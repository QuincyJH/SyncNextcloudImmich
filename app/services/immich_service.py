"""
Proposed refactor of `convert_album_to_tag` (the album → hierarchical tag
sync). Side-by-side with `immich_service.py`; nothing is wired up yet.

Key changes vs. the original:

  1. Single `requests.Session` per user with connection pooling — no more
     fresh TCP+TLS handshake on every API call.

  2. Tag hierarchy is ensured up-front in ONE pre-pass over the set of
     distinct hierarchical paths across all albums. After that, the
     per-album loop only has to look up tag IDs — it never mutates
     `path_map` — which makes it safe to parallelize.

  3. Per-album work (fetch assets → apply tag → drop standalone leaf)
     runs in a thread pool (`ALBUM_PARALLELISM`, default 8).

  4. Bulk removal of the standalone leaf tag from assets via
     `DELETE /api/tags/{tag_id}/assets` with `{"ids": [...]}` body —
     one request per standalone tag per album instead of one per asset.

  5. `create_tag`'s collision fallback no longer refetches the whole tag
     list on every conflict. It refreshes once per run and reuses.

Tunables (env vars):
  ALBUM_PARALLELISM   default 8    threads used for per-album sync
  HTTP_POOL_SIZE      default 16   requests connection pool size

Expected effect: for a library with lots of albums (especially many small
ones), wall-clock should drop roughly proportional to ALBUM_PARALLELISM,
plus a one-time saving from collapsing the per-asset DELETE loop into one
bulk call per album.
"""

from __future__ import annotations

import os
import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

import requests
from requests.adapters import HTTPAdapter

# Reuse everything that's already correct in the original module.
from app.services.immich_service import (
    CONFIG_FILE,
    MAPPING_FILE,
    LEAF_ONLY_TAGGING_DEFAULT,
    logger,
    load_mapping,
    find_hierarchical_tag,
    _build_tag_maps,
    _tag_parent_id,
    _as_bool,
)

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

ALBUM_PARALLELISM = int(os.environ.get("ALBUM_PARALLELISM", "8"))
HTTP_POOL_SIZE    = int(os.environ.get("HTTP_POOL_SIZE", "16"))


# ---------------------------------------------------------------------------
# HTTP session with connection pooling
# ---------------------------------------------------------------------------

def _make_session(api_key: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({"x-api-key": api_key, "Content-Type": "application/json"})
    adapter = HTTPAdapter(
        pool_connections=HTTP_POOL_SIZE,
        pool_maxsize=HTTP_POOL_SIZE,
    )
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    return s


# ---------------------------------------------------------------------------
# Small session-aware replacements for the original HTTP helpers
# ---------------------------------------------------------------------------

def _get_albums(session, immich_url):
    r = session.get(f"{immich_url}/api/albums", timeout=30)
    r.raise_for_status()
    return r.json()


def _get_album_assets(session, immich_url, album_id):
    r = session.get(f"{immich_url}/api/albums/{album_id}", timeout=60)
    r.raise_for_status()
    return r.json().get("assets", []) or []


def _get_all_tags(session, immich_url):
    r = session.get(f"{immich_url}/api/tags", timeout=30)
    r.raise_for_status()
    return r.json() or []


def _create_tag(session, immich_url, tag_name, parent_id, dry_run, refresh_path_map):
    """
    Create a tag. On a 400 'already exists' collision, trigger ONE cached
    refresh of the tag list and use that to find the id — no per-call
    `GET /api/tags` storm.
    """
    if dry_run:
        logger.info(f"[DRY-RUN] Would create tag '{tag_name}' (parent={parent_id})")
        return None

    payload = {"name": tag_name}
    if parent_id:
        payload["parentId"] = parent_id

    r = session.post(f"{immich_url}/api/tags", json=payload, timeout=30)
    if r.status_code in (200, 201):
        return r.json().get("id")

    if r.status_code == 400 and "already exists" in (r.text or "").lower():
        existing = refresh_path_map().get((tag_name, parent_id))
        if existing:
            logger.info(
                f"Tag '{tag_name}' already exists; using existing id {existing}"
            )
            return existing

    raise requests.HTTPError(
        f"Failed to create tag '{tag_name}': {r.status_code} {r.text}",
        response=r,
    )


def _ensure_path(session, immich_url, path_map, hierarchical_tag,
                 dry_run, refresh_path_map):
    """
    Walk down a 'Parent/Child/Leaf' path and make sure every segment exists.
    Updates path_map in place and returns the deepest tag id.
    """
    parts = hierarchical_tag.split("/")
    current_parent_id = None
    current_path = ""
    tag_id = None

    for part in parts:
        current_path = part if not current_path else f"{current_path}/{part}"
        if current_path in path_map:
            tag_id = path_map[current_path]
        else:
            tag_id = _create_tag(
                session, immich_url, part,
                parent_id=current_parent_id,
                dry_run=dry_run,
                refresh_path_map=refresh_path_map,
            )
            path_map[current_path] = tag_id
        current_parent_id = tag_id

    return tag_id


def _add_tag_to_assets(session, immich_url, tag_id, asset_ids, dry_run):
    if dry_run:
        logger.info(f"DRY RUN: Would tag {len(asset_ids)} assets with tag {tag_id}")
        return 200
    r = session.put(
        f"{immich_url}/api/tags/{tag_id}/assets",
        json={"ids": asset_ids},
        timeout=60,
    )
    if r.status_code not in (200, 201):
        logger.error(f"Failed to tag assets with tag {tag_id}: {r.status_code} {r.text}")
    return r.status_code


def _bulk_remove_tag_from_assets(session, immich_url, tag_id, asset_ids, dry_run):
    """
    Remove a tag from many assets in ONE request instead of N.
    Uses `DELETE /api/tags/{tag_id}/assets` with `{"ids": [...]}`.
    """
    if not asset_ids:
        return 200
    if dry_run:
        logger.info(
            f"DRY RUN: Would bulk-remove tag {tag_id} from {len(asset_ids)} assets"
        )
        return 200
    r = session.delete(
        f"{immich_url}/api/tags/{tag_id}/assets",
        json={"ids": asset_ids},
        timeout=60,
    )
    if r.status_code not in (200, 204):
        logger.warning(
            f"Bulk remove failed for tag {tag_id}: {r.status_code} {r.text}"
        )
    return r.status_code


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def convert_album_to_tag(dry_run: bool | None = None):
    with open(CONFIG_FILE, "r") as f:
        configs = json.load(f)

    for config in configs:
        immich_url = config["immich_url"]
        api_key    = config["immich_token"]
        effective_dry = config.get("dry_run", dry_run or False)
        leaf_only = _as_bool(
            config.get("leaf_only_tagging", LEAF_ONLY_TAGGING_DEFAULT),
            LEAF_ONLY_TAGGING_DEFAULT,
        )

        session = _make_session(api_key)

        mapping = load_mapping()
        logger.info(f"Loaded mapping file with {len(mapping)} parent categories.")

        # ---- Tag + album state, fetched once per user ----
        existing_tags = _get_all_tags(session, immich_url)
        _, path_map, name_index = _build_tag_maps(existing_tags)
        logger.info(f"Loaded {len(path_map)} existing tags from Immich.")

        albums = _get_albums(session, immich_url)
        logger.info(f"Found {len(albums)} albums.")

        # ---- Resolve all hierarchy paths up front ----
        mapped_by_album = {}
        mapped_paths = []
        for album in albums:
            mapped = find_hierarchical_tag(album["albumName"], mapping)
            mapped_by_album[album["id"]] = mapped
            mapped_paths.append(mapped)

        non_leaf_paths: set[str] = set()
        if leaf_only:
            for path in mapped_paths:
                prefix = f"{path}/"
                if any(other != path and other.startswith(prefix) for other in mapped_paths):
                    non_leaf_paths.add(path)
            if non_leaf_paths:
                logger.info(
                    f"Skipping {len(non_leaf_paths)} non-leaf mapped tags to "
                    f"avoid parent-tag duplication."
                )
        else:
            logger.info("Leaf-only tagging disabled; parent mapped tags will also be applied.")

        # ---- Pre-ensure every distinct hierarchical path in ONE serial pass ----
        # Done single-threaded because path_map is shared mutable state; the
        # slow part (per-album work) is parallelized below.
        refresh_lock = Lock()
        cached_key_index: dict = {}

        def refresh_path_map():
            """Only call on an 'already exists' collision. Cached within a run."""
            with refresh_lock:
                if cached_key_index:
                    return cached_key_index
                fresh = _get_all_tags(session, immich_url)
                for t in fresh:
                    cached_key_index[(t.get("name") or t.get("value"), _tag_parent_id(t))] = t.get("id")
                return cached_key_index

        tag_id_by_path: dict[str, str] = {}
        for album in albums:
            path = mapped_by_album[album["id"]]
            if leaf_only and path in non_leaf_paths:
                continue
            if path in tag_id_by_path:
                continue
            tag_id_by_path[path] = _ensure_path(
                session, immich_url, path_map, path,
                dry_run=effective_dry,
                refresh_path_map=refresh_path_map,
            )

        logger.info(
            f"Pre-ensured {len(tag_id_by_path)} distinct hierarchical tag path(s)."
        )

        # ---- Process albums in parallel ----
        def sync_one_album(album):
            album_id   = album["id"]
            album_name = album["albumName"]
            path       = mapped_by_album[album_id]

            if leaf_only and path in non_leaf_paths:
                return (album_name, "skipped-non-leaf", 0)

            tag_id = tag_id_by_path.get(path)
            if not tag_id and not effective_dry:
                # Should only happen if ensure failed above.
                logger.error(f"No tag id resolved for '{path}' (album '{album_name}')")
                return (album_name, "no-tag", 0)

            assets = _get_album_assets(session, immich_url, album_id)
            asset_ids = [a["id"] for a in assets]
            if not asset_ids:
                return (album_name, "empty", 0)

            _add_tag_to_assets(session, immich_url, tag_id, asset_ids,
                               dry_run=effective_dry)

            # Bulk-drop any standalone leaf tag (same leaf name, no parent)
            # that isn't the hierarchical one we just applied.
            leaf = path.split("/")[-1]
            standalone_ids = [
                t["id"] for t in name_index.get(leaf, [])
                if not _tag_parent_id(t) and t.get("id") != tag_id
            ]
            for sid in standalone_ids:
                _bulk_remove_tag_from_assets(
                    session, immich_url, sid, asset_ids,
                    dry_run=effective_dry,
                )

            return (album_name, "ok", len(asset_ids))

        with ThreadPoolExecutor(max_workers=ALBUM_PARALLELISM) as ex:
            futures = [ex.submit(sync_one_album, a) for a in albums]
            for fut in as_completed(futures):
                try:
                    name, status, n = fut.result()
                    if status == "ok":
                        logger.info(f"Applied hierarchical tag to '{name}' ({n} assets)")
                    elif status == "skipped-non-leaf":
                        logger.debug(f"Skipped non-leaf album '{name}'")
                    elif status == "empty":
                        logger.debug(f"Album '{name}' had no assets")
                except Exception as e:
                    logger.error(f"Album task crashed: {e}")

    logger.info("Album → hierarchical tag sync run complete.")
