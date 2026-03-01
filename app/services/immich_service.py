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


def create_tag(immich_url, api_key, name, parent_id=None, dry_run=False):
    """Create a tag by name, optionally under a parentId (hierarchical)."""
    headers = immich_headers(api_key)
    payload = {"name": name}
    if parent_id:
        payload["parentId"] = parent_id

    if dry_run:
        logger.info(f"DRY RUN: Would create tag '{name}' parentId={parent_id}")
        # Return a synthetic id to allow downstream logic to proceed
        return f"dryrun:{parent_id or 'root'}:{name}"

    r = requests.post(f"{immich_url}/api/tags", headers=headers, json=payload)

    # If tag already exists, resolve it by name + parentId
    if r.status_code == 400:
        existing = get_all_tags(immich_url, api_key)
        for t in existing:
            if t.get("value") == name and t.get("parentId") == parent_id:
                return t["id"]
        raise requests.HTTPError(
            f"Failed to create tag '{name}': {r.text}", response=r
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


def get_tag_id_by_name(immich_url, api_key, tag_name):
    headers = immich_headers(api_key)
    r = requests.get(f"{immich_url}/api/tags", headers=headers)
    if r.status_code != 200:
        return None

    for tag in r.json():
        if tag.get("value") == tag_name:
            return tag.get("id")

    return None


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
        name_index.setdefault(t.get("value"), []).append(t)

    def _path_for(tag):
        parts = []
        cur = tag
        while cur:
            parts.append(cur.get("value"))
            pid = cur.get("parentId")
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


def find_hierarchical_tag(album_name, mapping):
    """
    Given a flat album name, return the hierarchical tag path.
    Supports unlimited nested dicts and {} leaf nodes.
    """
    for parent, children in mapping.items():

        # Case 1: children is a list
        if isinstance(children, list):
            if album_name in children:
                return f"{parent}/{album_name}"

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
    """
    for key, value in subtree.items():

        # Case 1: leaf node (empty dict)
        if key == target and isinstance(value, dict) and len(value) == 0:
            return f"{parent_path}/{key}"

        # Case 2: nested dict
        if isinstance(value, dict):
            deeper = search_nested_mapping(target, f"{parent_path}/{key}", value)
            if deeper:
                return deeper

        # Case 3: list of children
        if isinstance(value, list) and target in value:
            return f"{parent_path}/{key}/{target}"

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


# -----------------------------
# MAIN SYNC LOGIC
# -----------------------------

def convert_album_to_tag():
    parser = argparse.ArgumentParser(description="Immich album → hierarchical tag sync")
    parser.add_argument("--dry-run", action="store_true", help="Print intended changes without modifying Immich")

    if dry_run is not None:
        args = parser.parse_args([])
        args.dry_run = dry_run
    else:
        args = parser.parse_args()

    # Load config
    with open(CONFIG_FILE, "r") as f:
        configs = json.load(f)
        
    for config in configs:

        immich_url = config["immich_url"]
        api_key = config["immich_token"]
        dry_run = config.get("dry_run", False)

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

        for album in albums:
            album_id = album["id"]
            album_name = album["albumName"]

            logger.info(f"Processing album '{album_name}' ({album_id})")

            # Convert album name → hierarchical tag
            hierarchical_tag = find_hierarchical_tag(album_name, mapping)

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
            standalone_leaf_id = None
            for t in name_index.get(leaf, []):
                if not t.get("parentId"):
                    standalone_leaf_id = t["id"]
                    break
            if standalone_leaf_id and standalone_leaf_id != tag_id:
                logger.info(f"Removing standalone leaf tag '{leaf}' from assets.")
                remove_tag_from_assets(immich_url, api_key, standalone_leaf_id, asset_ids, dry_run=dry_run)

    logger.info("Album → hierarchical tag sync run complete.")
