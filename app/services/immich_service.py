#!/usr/bin/env python3
import requests
import logging
from logging.handlers import RotatingFileHandler
import json
import os
import sys
import argparse

# -----------------------------
# CONFIG
# -----------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Allow overriding config paths via env; default to /config inside container
CONFIG_FILE = os.environ.get("CONFIG_FILE", os.path.join("/config", "user_config.json"))
MAPPING_FILE = os.environ.get("MAPPING_FILE", os.path.join("/config", "mapping.json"))
LOGFILE = os.environ.get("LOGFILE", os.path.join("/config", "immich_album_tag_sync.log"))
LEAF_ONLY_TAGGING_DEFAULT = os.environ.get("LEAF_ONLY_TAGGING", "true").lower() in ("1", "true", "yes", "on")

logger = logging.getLogger("ImmichAlbumTagSync")
logger.setLevel(getattr(logging, os.environ.get("LOG_LEVEL", "INFO")))
formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

# Console logging for container use
console_handler = logging.StreamHandler()
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)

# Optional file logging
if os.environ.get("LOG_TO_FILE", "false").lower() in ("1", "true", "yes"):
    file_handler = RotatingFileHandler(LOGFILE, maxBytes=5*1024*1024, backupCount=3)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)


# -----------------------------
# IMMICH API HELPERS
# -----------------------------

def immich_headers(api_key):
    return {"x-api-key": api_key, "Content-Type": "application/json"}


def get_albums(immich_url, api_key):
    headers = immich_headers(api_key)
    r = requests.get(f"{immich_url}/api/albums", headers=headers)
    r.raise_for_status()
    return r.json()


def get_album_assets(immich_url, api_key, album_id):
    headers = immich_headers(api_key)
    r = requests.get(f"{immich_url}/api/albums/{album_id}", headers=headers)
    r.raise_for_status()
    album = r.json()
    return album.get("assets", [])


def get_all_tags(immich_url, api_key):
    headers = immich_headers(api_key)
    r = requests.get(f"{immich_url}/api/tags", headers=headers)
    r.raise_for_status()
    return r.json()

def _find_existing_tag_id(immich_url, api_key, tag_name, parent_id=None):
    headers = immich_headers(api_key)
    r = requests.get(f"{immich_url}/api/tags", headers=headers, timeout=30)
    if r.status_code != 200:
        return None

    for t in r.json() or []:
        name = t.get("name")
        pid = t.get("parentId", t.get("parentTagId"))
        if name == tag_name and (pid == parent_id or (pid is None and parent_id is None)):
            return t.get("id")
    return None


def create_tag(immich_url, api_key, tag_name, parent_id=None, dry_run=False):
    if dry_run:
        logger.info(f"[DRY-RUN] Would create tag '{tag_name}' (parent={parent_id})")
        return None

    headers = immich_headers(api_key)
    payload = {"name": tag_name}
    if parent_id:
        payload["parentId"] = parent_id

    r = requests.post(f"{immich_url}/api/tags", headers=headers, json=payload, timeout=30)

    if r.status_code in (200, 201):
        return r.json().get("id")

    if r.status_code == 400 and "already exists" in (r.text or "").lower():
        existing_id = _find_existing_tag_id(immich_url, api_key, tag_name, parent_id)
        if existing_id:
            logger.info(f"Tag '{tag_name}' already exists; using existing id {existing_id}")
            return existing_id

    raise requests.HTTPError(
        f"Failed to create tag '{tag_name}': {r.text}",
        response=r,
    )

    r.raise_for_status()
    return r.json()["id"]
    

def add_tag_to_assets(immich_url, api_key, tag_id, asset_ids, dry_run=False):
    headers = immich_headers(api_key)
    data = {"ids": asset_ids}
    if dry_run:
        logger.info(f"DRY RUN: Would tag {len(asset_ids)} assets with tag {tag_id}")
        return 200, "DRY RUN"
    r = requests.put(f"{immich_url}/api/tags/{tag_id}/assets", headers=headers, json=data)
    if r.status_code not in (200, 201):
        logger.error(f"Failed to tag assets with tag {tag_id}: {r.status_code} {r.text}")
    return r.status_code, r.text


def delete_tag(immich_url, api_key, tag_id, dry_run=False):
    headers = immich_headers(api_key)
    if dry_run:
        logger.info(f"DRY RUN: Would delete tag {tag_id}")
        return 200, "DRY RUN"
    r = requests.delete(f"{immich_url}/api/tags/{tag_id}", headers=headers)
    if r.status_code not in (200, 204):
        logger.warning(f"Failed to delete tag {tag_id}: {r.status_code} {r.text}")
    return r.status_code, r.text


def get_tag_id_by_name(immich_url, api_key, tag_name):
    headers = immich_headers(api_key)
    r = requests.get(f"{immich_url}/api/tags", headers=headers)
    if r.status_code != 200:
        return None

    for tag in r.json():
        if tag.get("value") == tag_name:
            return tag.get("id")

    return None


def _tag_name(tag):
    return tag.get("value") or tag.get("name")


def _tag_parent_id(tag):
    return tag.get("parentId") or tag.get("parentTagId")


def _build_tag_maps(tags):
    """
    Build helpful maps:
      - id_index: {id: tag}
      - path_map: {"Parent/Child": id}
      - name_index: {value: [tag, ...]}
    """
    id_index = {t["id"]: t for t in tags}
    name_index = {}
    for t in tags:
        tag_name = _tag_name(t)
        if tag_name:
            name_index.setdefault(tag_name, []).append(t)

    def _path_for(tag):
        parts = []
        cur = tag
        while cur:
            parts.append(_tag_name(cur))
            pid = _tag_parent_id(cur)
            cur = id_index.get(pid)
        return "/".join(reversed(parts))

    path_map = {_path_for(t): t["id"] for t in tags}
    return id_index, path_map, name_index


# -----------------------------
# HIERARCHY MAPPING LOGIC
# -----------------------------

def load_mapping():
    with open(MAPPING_FILE, "r") as f:
        return json.load(f)


def _as_bool(value, default=False):
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _normalize_label(value):
    if value is None:
        return ""
    return " ".join(str(value).strip().split()).casefold()


def find_hierarchical_tag(album_name, mapping):
    """
    Given a flat album name, return the hierarchical tag path.
    Supports unlimited nested dicts and {} leaf nodes.
    """
    normalized_album_name = _normalize_label(album_name)

    for parent, children in mapping.items():
        # Case 0: direct top-level match
        if normalized_album_name == _normalize_label(parent):
            return parent

        # Case 1: children is a list
        if isinstance(children, list):
            for child in children:
                if normalized_album_name == _normalize_label(child):
                    return f"{parent}/{child}"

        # Case 2: children is a nested dict
        if isinstance(children, dict):
            result = search_nested_mapping(album_name, parent, children)
            if result:
                return result

    return album_name  # fallback: no hierarchy


def search_nested_mapping(target, parent_path, subtree):
    """
    Recursively search nested mapping structures.
    Supports:
      - parent: [list]
      - parent: { child: {}, child: { ... } }
      - unlimited depth
    Returns a full path for both intermediate nodes and leaves.
    """
    normalized_target = _normalize_label(target)

    for key, value in subtree.items():

        # Case 1: match node itself (works for both intermediate nodes and leaf nodes)
        if _normalize_label(key) == normalized_target:
            return f"{parent_path}/{key}"

        # Case 2: nested dict
        if isinstance(value, dict):
            deeper = search_nested_mapping(target, f"{parent_path}/{key}", value)
            if deeper:
                return deeper

        # Case 3: list of children
        if isinstance(value, list):
            for child in value:
                if _normalize_label(child) == normalized_target:
                    return f"{parent_path}/{key}/{child}"

    return None


def ensure_parent_tags_exist(immich_url, api_key, path_map, hierarchical_tag, dry_run=False):
    parts = hierarchical_tag.split("/")
    current_parent_id = None
    current_path = ""

    for part in parts:
        current_path = part if current_path == "" else f"{current_path}/{part}"
        if current_path in path_map:
            current_parent_id = path_map[current_path]
            tag_id = current_parent_id
        else:
            # Create child under the current parentId
            tag_id = create_tag(immich_url, api_key, part, parent_id=current_parent_id, dry_run=dry_run)
            path_map[current_path] = tag_id
            current_parent_id = tag_id

    return tag_id


def remove_tag_from_assets(immich_url, api_key, tag_id, asset_ids, dry_run=False):
    """Remove a tag from given assets (used to drop standalone leaf tags)."""
    headers = immich_headers(api_key)
    if dry_run:
        logger.info(f"DRY RUN: Would remove tag {tag_id} from {len(asset_ids)} assets")
        return
    for aid in asset_ids:
        r = requests.delete(f"{immich_url}/api/tags/{tag_id}/assets/{aid}", headers=headers)
        if r.status_code not in (200, 204):
            logger.warning(f"Failed to remove tag {tag_id} from asset {aid}: {r.status_code} {r.text}")


def clear_all_tags(dry_run: bool = False):
    """Delete all tags from each configured Immich account."""
    with open(CONFIG_FILE, "r") as f:
        configs = json.load(f)

    for config in configs:
        immich_url = config["immich_url"]
        api_key = config["immich_token"]

        tags = get_all_tags(immich_url, api_key)
        if not tags:
            logger.info("No tags found to delete.")
            continue

        id_index = {t.get("id"): t for t in tags if t.get("id")}

        def _depth(tag):
            depth = 0
            cur = tag
            seen = set()
            while cur:
                tid = cur.get("id")
                if tid in seen:
                    break
                seen.add(tid)
                pid = _tag_parent_id(cur)
                cur = id_index.get(pid)
                if cur:
                    depth += 1
            return depth

        # Delete deepest children first, then parents.
        ordered_tags = sorted(tags, key=_depth, reverse=True)
        logger.info(f"Found {len(ordered_tags)} tags. Deleting child-first order.")

        deleted = 0
        for tag in ordered_tags:
            tag_id = tag.get("id")
            if not tag_id:
                continue
            status, _ = delete_tag(immich_url, api_key, tag_id, dry_run=dry_run)
            if status in (200, 204):
                deleted += 1

        logger.info(f"Deleted {deleted}/{len(ordered_tags)} tags.")


# -----------------------------
# MAIN SYNC LOGIC
# -----------------------------

def convert_album_to_tag(dry_run: bool | None = None):
    parser = argparse.ArgumentParser(description="Immich album → hierarchical tag sync")
    parser.add_argument("--dry-run", action="store_true", help="Print intended changes without modifying Immich")

    if dry_run is not None:
        args = parser.parse_args([])
        args.dry_run = dry_run
    else:
        args = parser.parse_args()
        dry_run = args.dry_run

    # Load config
    with open(CONFIG_FILE, "r") as f:
        configs = json.load(f)
        
    for config in configs:

        immich_url = config["immich_url"]
        api_key = config["immich_token"]
        dry_run = config.get("dry_run", dry_run)
        leaf_only_tagging = _as_bool(
            config.get("leaf_only_tagging", LEAF_ONLY_TAGGING_DEFAULT),
            LEAF_ONLY_TAGGING_DEFAULT,
        )

        # Load mapping file
        mapping = load_mapping()
        logger.info(f"Loaded mapping file with {len(mapping)} parent categories.")

        # Load all tags once and build maps
        existing_tags = get_all_tags(immich_url, api_key)
        id_index, path_map, name_index = _build_tag_maps(existing_tags)

        logger.info(f"Loaded {len(path_map)} existing tags from Immich.")

        # Load all albums
        albums = get_albums(immich_url, api_key)
        logger.info(f"Found {len(albums)} albums.")

        # Precompute mapped hierarchy path per album and keep only leaf paths.
        mapped_by_album = {}
        mapped_paths = []
        for album in albums:
            mapped = find_hierarchical_tag(album["albumName"], mapping)
            if mapped == album["albumName"]:
                logger.debug(
                    f"No hierarchy mapping found for album '{album['albumName']}'. Using fallback tag '{mapped}'."
                )
            mapped_by_album[album["id"]] = mapped
            mapped_paths.append(mapped)

        non_leaf_paths = set()
        if leaf_only_tagging:
            for path in mapped_paths:
                prefix = f"{path}/"
                if any(other != path and other.startswith(prefix) for other in mapped_paths):
                    non_leaf_paths.add(path)

            if non_leaf_paths:
                logger.info(
                    f"Skipping {len(non_leaf_paths)} non-leaf mapped tags to avoid parent-tag duplication."
                )
        else:
            logger.info("Leaf-only tagging disabled; parent mapped tags will also be applied.")

        for album in albums:
            album_id = album["id"]
            album_name = album["albumName"]

            logger.info(f"Processing album '{album_name}' ({album_id})")

            # Convert album name → hierarchical tag
            hierarchical_tag = mapped_by_album.get(album_id, album_name)

            if leaf_only_tagging and hierarchical_tag in non_leaf_paths:
                logger.info(
                    f"Skipping album '{album_name}' because mapped tag '{hierarchical_tag}' has child paths."
                )
                continue

            logger.info(f"Mapped album '{album_name}' → tag '{hierarchical_tag}'")

            # Ensure full hierarchy exists and get deepest tag id
            tag_id = ensure_parent_tags_exist(immich_url, api_key, path_map, hierarchical_tag,dry_run=dry_run)

            # Get assets in album
            assets = get_album_assets(immich_url, api_key, album_id)
            asset_ids = [a["id"] for a in assets]

            if not asset_ids:
                logger.info(f"No assets found in album '{album_name}'. Skipping.")
                continue

            # Bulk apply hierarchical tag
            status, _ = add_tag_to_assets(immich_url, api_key, tag_id, asset_ids, dry_run=dry_run)
            logger.info(f"Applied tag '{hierarchical_tag}' to {len(asset_ids)} assets. Status: {status}")

            # Remove standalone leaf tag (same name, no parent) to avoid duplicates
            leaf = hierarchical_tag.split("/")[-1]
            standalone_leaf_ids = [
                t["id"]
                for t in name_index.get(leaf, [])
                if not _tag_parent_id(t) and t.get("id") != tag_id
            ]
            if standalone_leaf_ids:
                logger.info(
                    f"Removing {len(standalone_leaf_ids)} standalone leaf tag(s) named '{leaf}' from assets."
                )
                for standalone_leaf_id in standalone_leaf_ids:
                    remove_tag_from_assets(immich_url, api_key, standalone_leaf_id, asset_ids, dry_run=dry_run)

    logger.info("Album → hierarchical tag sync run complete.")
