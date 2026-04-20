"""
Proposed refactor of `copy_nextcloud_tags_to_immich` and its helpers.

This file is for review, not yet wired into the app. It leaves
`sync_files_to_cloud` and the file-upload path in the original
`sync_service.py` untouched.

Key changes vs. the original:

  1. Build a single in-memory index of Immich assets per user, keyed by
     checksum / (filename, size) / filename. The per-file lookup now does
     ZERO HTTP calls instead of 1-2 calls each.

  2. Fetch all (tag_id, path, checksum, size) rows for a user in ONE SQL
     query using `systemtagid = ANY(%s)`, instead of one query per tag.

  3. Fetch the Immich album list ONCE per user (was: once per tag inside
     `get_or_create_album`), and reuse it for the stale-album cleanup
     pass at the end.

  4. Use a `requests.Session` with a larger connection pool so we stop
     paying TCP+TLS setup on every call.

  5. Parallelize per-tag work (lookup + PUT/DELETE) with a thread pool.
     Safe because each tag touches a different album.

Expected effect: for a library with ~10k tagged assets and ~50 tags,
wall-clock drops from "hours" to "a couple of minutes" — dominated by
the single index build instead of N*M round trips.

Tunables (env vars):
  IMMICH_PAGE_SIZE    default 1000   page size when indexing assets
  ALBUM_PARALLELISM   default 8      threads used for per-tag sync
  HTTP_POOL_SIZE      default 16     requests connection pool size
"""

from __future__ import annotations

import os
import json
import base64
import logging
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import unquote

import psycopg
import requests
from requests.adapters import HTTPAdapter

# Reuse existing config/logger/DB constants and the unchanged helper.
from app.services.sync_service import (
    CONFIG_FILE,
    DB_HOST, DB_PORT, DB_USER, DB_PASSWORD, DB_NAME,
    logger,
    get_system_tags_db,
)

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

IMMICH_PAGE_SIZE  = int(os.environ.get("IMMICH_PAGE_SIZE", "1000"))
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
# STEP 1: one-shot Immich asset index
# ---------------------------------------------------------------------------

def build_immich_asset_index(session: requests.Session, immich_url: str) -> dict:
    """
    Page through every asset and build three lookup tables:

        by_checksum:  {base64_checksum: asset_id}
        by_name_size: {(filename, size): [asset_id, ...]}
        by_name:      {filename: [asset_id, ...]}

    `POST /api/search/metadata` with an empty filter + `page`/`size` paginates
    over the whole library. If your Immich version exposes a cheaper "list
    all assets" endpoint you can swap it in here — the only contract is the
    three dicts above.
    """
    by_checksum: dict[str, str] = {}
    by_name_size: dict[tuple[str, int], list[str]] = defaultdict(list)
    by_name: dict[str, list[str]] = defaultdict(list)

    page = 1
    total = 0
    while True:
        r = session.post(
            f"{immich_url}/api/search/metadata",
            json={"page": page, "size": IMMICH_PAGE_SIZE},
            timeout=60,
        )
        r.raise_for_status()
        data = r.json()
        assets_block = data.get("assets", {}) or {}
        items = assets_block.get("items", []) or []
        if not items:
            break

        for a in items:
            aid = a.get("id")
            if not aid:
                continue

            checksum = a.get("checksum")
            if checksum:
                by_checksum.setdefault(checksum, aid)

            name = a.get("originalFileName")
            if name:
                by_name[name].append(aid)

                raw_size = (
                    a.get("fileSizeInByte")
                    or a.get("size")
                    or a.get("fileSize")
                    or (a.get("exifInfo") or {}).get("fileSizeInByte")
                )
                size: int | None = None
                if raw_size is not None:
                    try:
                        size = int(raw_size)
                    except Exception:
                        size = None
                if size is not None:
                    by_name_size[(name, size)].append(aid)

        total += len(items)
        next_page = assets_block.get("nextPage")
        if not next_page:
            break
        try:
            page = int(next_page)
        except Exception:
            page += 1

    logger.info(
        f"Indexed Immich library: {total} assets, "
        f"{len(by_checksum)} with checksum, "
        f"{len(by_name)} distinct filenames."
    )
    return {
        "by_checksum": by_checksum,
        "by_name_size": by_name_size,
        "by_name": by_name,
    }


def resolve_asset_id(index: dict, filename: str, nc_checksum, nc_filesize):
    """Pure in-memory resolution — no HTTP calls."""
    # 1. Checksum (unambiguous when present).
    if nc_checksum and ":" in nc_checksum:
        try:
            _, hex_value = nc_checksum.split(":", 1)
            b64 = base64.b64encode(bytes.fromhex(hex_value)).decode()
            hit = index["by_checksum"].get(b64)
            if hit:
                return hit
        except Exception:
            pass  # fall through to filename-based matching

    # 2. Filename + size.
    if nc_filesize is not None:
        candidates = index["by_name_size"].get((filename, int(nc_filesize)))
        if candidates and len(candidates) == 1:
            return candidates[0]

    # 3. Filename only, but only if unambiguous.
    by_name = index["by_name"].get(filename) or []
    if len(by_name) == 1:
        return by_name[0]

    return None


# ---------------------------------------------------------------------------
# STEP 2: one-shot DB pull for a user
# ---------------------------------------------------------------------------

def fetch_user_tag_files(username: str, tag_ids) -> dict:
    """
    Single SQL round-trip. Returns {tag_id: [(path, checksum, size), ...]}.
    """
    if not tag_ids:
        return {}

    sql = """
        SELECT m.systemtagid, f.path, f.checksum, f.size
        FROM oc_systemtag_object_mapping m
        JOIN oc_filecache f ON f.fileid = m.objectid::bigint
        JOIN oc_storages s  ON s.numeric_id = f.storage
        WHERE m.systemtagid = ANY(%s)
          AND m.objecttype = 'files'
          AND s.id = %s
          AND f.path LIKE 'files/%%';
    """
    params = ([int(t) for t in tag_ids], f"home::{username}")
    result: dict[int, list] = defaultdict(list)

    try:
        with psycopg.connect(
            host=DB_HOST, port=DB_PORT,
            user=DB_USER, password=DB_PASSWORD,
            dbname=DB_NAME, connect_timeout=10,
        ) as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            for tid, path, checksum, size in cur.fetchall():
                if not path:
                    continue
                try:
                    size_i = int(size) if size is not None else None
                except Exception:
                    size_i = None
                result[int(tid)].append((path, checksum or None, size_i))
    except Exception as e:
        logger.error(f"Bulk tag-file DB query failed for {username}: {e}")

    return result


# ---------------------------------------------------------------------------
# STEP 3: one-shot album list + small helpers
# ---------------------------------------------------------------------------

def fetch_album_map(session: requests.Session, immich_url: str) -> dict:
    """{album_name: {'id': ..., 'type': ...}} for every album on the server."""
    r = session.get(f"{immich_url}/api/albums", timeout=30)
    r.raise_for_status()
    return {
        a["albumName"]: {"id": a["id"], "type": a.get("albumType")}
        for a in r.json()
    }


def ensure_album(session, immich_url, albums: dict, name: str):
    """Look up an album by name; create it (and update the cache) if missing."""
    if name in albums:
        return albums[name]["id"]
    r = session.post(
        f"{immich_url}/api/albums",
        json={"albumName": name, "assetIds": []},
        timeout=30,
    )
    if r.status_code in (200, 201):
        aid = r.json().get("id")
        albums[name] = {"id": aid, "type": None}
        return aid
    logger.error(f"Album creation failed for '{name}': {r.status_code} {r.text}")
    return None


def current_album_assets(session, immich_url, album_id):
    r = session.get(f"{immich_url}/api/albums/{album_id}", timeout=60)
    if r.status_code != 200:
        logger.error(
            f"Failed to fetch album contents for {album_id}: "
            f"{r.status_code} {r.text}"
        )
        return None
    return {a["id"] for a in r.json().get("assets", [])}


# ---------------------------------------------------------------------------
# STEP 4: refactored entry point
# ---------------------------------------------------------------------------

def copy_nextcloud_tags_to_immich(dry_run=None):
    logger.info("Starting Nextcloud → Immich album sync (optimized)...")

    with open(CONFIG_FILE, "r") as f:
        users = json.load(f)

    global_tags = get_system_tags_db()
    logger.info(f"Retrieved {len(global_tags)} system tags from DB.")
    if not global_tags:
        logger.warning("No system tags found — nothing to sync.")
        return

    for user in users:
        username        = user["nextcloud_username"]
        immich_url      = user["immich_url"]
        immich_token    = user["immich_token"]
        effective_dry   = user.get("dry_run", False) if dry_run is None else dry_run
        whitelist       = set(user.get("whitelist_albums", []))

        logger.info(f"[{username}] Starting sync. Dry run={effective_dry}")
        session = _make_session(immich_token)

        # Fetch album list — also acts as token validation.
        try:
            albums = fetch_album_map(session, immich_url)
        except Exception as e:
            logger.error(f"[{username}] Can't reach Immich / invalid token: {e}")
            continue

        # Build the asset index once for the whole user.
        try:
            index = build_immich_asset_index(session, immich_url)
        except Exception as e:
            logger.error(f"[{username}] Asset indexing failed: {e}")
            continue

        # One SQL query for all tags this user has.
        tag_files = fetch_user_tag_files(username, list(global_tags.keys()))

        def sync_one_tag(item):
            tid, tag_name = item
            nc_files = tag_files.get(tid, [])
            if not nc_files:
                return (tag_name, "empty", 0, 0, 0)

            album_id = ensure_album(session, immich_url, albums, tag_name)
            if not album_id:
                return (tag_name, "no-album", 0, 0, 0)

            desired: set[str] = set()
            missing = 0
            for path, checksum, size in nc_files:
                filename = unquote(path.split("/")[-1])
                aid = resolve_asset_id(index, filename, checksum, size)
                if aid:
                    desired.add(aid)
                else:
                    missing += 1

            current = current_album_assets(session, immich_url, album_id)
            if current is None:
                return (tag_name, "fetch-failed", 0, 0, missing)

            to_add    = desired - current
            to_remove = current - desired

            if effective_dry:
                logger.info(
                    f"[{username}]-{tag_name} DRY: +{len(to_add)} / -{len(to_remove)} "
                    f"(missing Immich asset for {missing} NC file(s))"
                )
                return (tag_name, "dry", len(to_add), len(to_remove), missing)

            if to_add:
                session.put(
                    f"{immich_url}/api/albums/{album_id}/assets",
                    json={"ids": list(to_add)},
                    timeout=60,
                )
            if to_remove:
                session.delete(
                    f"{immich_url}/api/albums/{album_id}/assets",
                    json={"ids": list(to_remove)},
                    timeout=60,
                )
            return (tag_name, "ok", len(to_add), len(to_remove), missing)

        # Parallel over tags — each touches a different album, so no contention.
        with ThreadPoolExecutor(max_workers=ALBUM_PARALLELISM) as ex:
            futures = [ex.submit(sync_one_tag, item) for item in global_tags.items()]
            for fut in as_completed(futures):
                try:
                    name, status, added, removed, missing = fut.result()
                    if status == "ok":
                        logger.info(
                            f"[{username}]-{name} synced (+{added}/-{removed}, "
                            f"{missing} unresolved)"
                        )
                    elif status == "empty":
                        logger.debug(f"[{username}]-{name} no files; skipped.")
                except Exception as e:
                    logger.error(f"[{username}] tag task crashed: {e}")

        # ---- Stale-album cleanup: reuse the album list we already have ----
        valid = set(global_tags.values())
        for name, meta in list(albums.items()):
            if name in whitelist or name in valid or meta.get("type") == "favorite":
                continue
            logger.warning(f"[{username}] Stale album '{name}' ({meta['id']})")
            if effective_dry:
                continue
            r = session.delete(
                f"{immich_url}/api/albums/{meta['id']}",
                timeout=30,
            )
            logger.info(
                f"[{username}] Deleted album '{name}' (status {r.status_code})"
            )

    logger.info("Album sync run complete.")
