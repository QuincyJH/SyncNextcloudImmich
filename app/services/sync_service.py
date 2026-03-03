import os
import sys
import subprocess
import shutil
import logging
import json
import requests
from logging.handlers import RotatingFileHandler
from datetime import datetime
from urllib.parse import unquote
import base64
import psycopg

CONFIG_FILE = os.environ.get("CONFIG_FILE", "/config/user_config.json")
LOGFILE = os.environ.get("LOGFILE", "/config/immich_file_sync.log")
LOG_TO_FILE = os.environ.get("LOG_TO_FILE", "false").lower() in ("1", "true", "yes")
LOG_LEVEL = getattr(logging, os.environ.get("LOG_LEVEL", "INFO"))

# -----------------------------
# DATABASE SETTINGS
# -----------------------------

DB_HOST = os.environ.get("NEXTCLOUD_DB_HOST", "nextcloud-db")
DB_PORT = int(os.environ.get("NEXTCLOUD_DB_PORT", "5432"))
DB_USER = os.environ.get("NEXTCLOUD_DB_USER", "nextcloud")
DB_PASSWORD = os.environ.get("NEXTCLOUD_DB_PASSWORD", "")
DB_NAME = os.environ.get("NEXTCLOUD_DB_NAME", "nextcloud")

logger = logging.getLogger("ImmichFileSync")
logger.setLevel(LOG_LEVEL)
fmt = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
console = logging.StreamHandler(sys.stdout)
console.setFormatter(fmt)
logger.addHandler(console)
if LOG_TO_FILE:
    file_handler = RotatingFileHandler(LOGFILE, maxBytes=5*1024*1024, backupCount=3)
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

def sync_files_to_cloud(dry_run=None):
    with open(CONFIG_FILE, "r") as f:
        users = json.load(f)

    for user in users:
        nextcloud_file_path = user['nextcloud_file_path']
        
        nextcloud_username = user['nextcloud_username']
        immich_url = user['immich_url']
        immich_token = user['immich_token']
        effective_dry_run = user.get("dry_run", False) if dry_run is None else dry_run

        if effective_dry_run:
            logger.info(f"[{nextcloud_username}] DRY RUN: simulating upload from {nextcloud_file_path} → Immich")
        else:
            logger.info(f"[{nextcloud_username}] Uploading files from {nextcloud_file_path} → Immich")
        start = datetime.utcnow()
        rc = run_immich_go_upload(immich_url, immich_token, nextcloud_file_path, dry_run=effective_dry_run)
        end = datetime.utcnow()
        if rc == 0:
            if effective_dry_run:
                log = f"[{nextcloud_username}] Dry run complete. Elapsed: {end - start}"
            else:
                log = f"[{nextcloud_username}] Finished upload. Elapsed: {end - start}"
            logger.info(log)
        else:
            log = f"[{nextcloud_username}] Upload failed with code {rc}"
            logger.error(log)

def run_immich_go_upload(server, api_key, folder, dry_run=False):
    immich_go_bin = os.environ.get("IMMICH_GO_BIN", "immich-go")
    if not dry_run and shutil.which(immich_go_bin) is None:
        logger.error(
            "immich-go binary not found. Set IMMICH_GO_BIN or mount the binary to /usr/local/bin/immich-go in the container."
        )
        return 127

    cmd = [
        immich_go_bin, "upload", "from-folder",
        "--server", server,
        "--api-key", api_key,
        "--no-ui=true",
        folder,
    ]
    if dry_run:
        logger.info(f"DRY RUN: Would execute: {' '.join(cmd)}")
        return 0
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        logger.error(
            f"immich-go executable '{immich_go_bin}' was not found at runtime."
        )
        return 127
    if result.returncode != 0:
        logger.error(f"immich-go failed (rc={result.returncode}): {result.stderr.strip()}")
    else:
        if result.stdout:
            logger.info(result.stdout.strip())
    return result.returncode

# -----------------------------
# IMMICH HELPERS
# -----------------------------

def immich_headers(token):
    return {"x-api-key": token, "Content-Type": "application/json"}


def test_immich_token(immich_url, immich_token):
    # A lightweight check: list albums (requires a valid user token)
    headers = immich_headers(immich_token)
    r = requests.get(f"{immich_url}/api/albums", headers=headers)
    if r.status_code == 200:
        logger.info("Immich token validated: can list albums.")
        return True
    logger.error(f"Immich token validation failed: {r.status_code} {r.text}")
    return False

def lookup_asset_id_in_immich(immich_url, immich_token, filename, nc_checksum=None, nc_filesize=None):
    """
    Return the Immich asset ID for a file with an EXACT filename match,
    preferably filtered by file size and optionally by checksum.

    nc_checksum should be the raw Nextcloud checksum string, e.g.:
        "SHA1:713a45f244961366129248c8c08c1bcb3d311b8d"
    """

    headers = immich_headers(immich_token)

    # Step 1: Query Immich for all assets containing the filename
    response = requests.post(
        f"{immich_url}/api/search/metadata",
        headers=headers,
        json={"originalFileName": filename}
    )

    if response.status_code != 200:
        logger.error(f"Immich lookup failed for {filename}: {response.status_code} {response.text}")
        return None

    data = response.json()
    items = data.get("assets", {}).get("items", [])
    if not items:
        logger.warning(f"No Immich asset found for {filename}")
        return None

    # Step 2: Filter to exact filename matches only
    exact = [a for a in items if a.get("originalFileName") == filename]

    if not exact:
        logger.warning(
            f"No exact filename match in Immich for {filename}. "
            f"Candidates were: {[a.get('originalFileName') for a in items]}"
        )
        return None

    # Step 3: If Nextcloud filesize is available, prefer exact filename+size match
    if nc_filesize is not None:
        def _asset_size(asset):
            candidates = (
                asset.get("fileSizeInByte"),
                asset.get("size"),
                asset.get("fileSize"),
                (asset.get("exifInfo") or {}).get("fileSizeInByte"),
            )
            for value in candidates:
                if value is None:
                    continue
                try:
                    return int(value)
                except Exception:
                    continue
            return None

        def _load_asset_detail_size(asset):
            asset_id = asset.get("id")
            if not asset_id:
                return None
            r = requests.get(f"{immich_url}/api/assets/{asset_id}", headers=headers)
            if r.status_code != 200:
                logger.warning(
                    f"Failed to load Immich asset details for {asset_id}: {r.status_code} {r.text}"
                )
                return None
            return _asset_size(r.json())

        size_matches = [a for a in exact if _asset_size(a) == nc_filesize]

        # /api/search/metadata often omits file size; fetch details for unresolved candidates.
        if not size_matches and len(exact) > 1:
            detailed_size_matches = []
            for asset in exact:
                asset_size = _asset_size(asset)
                if asset_size is None:
                    asset_size = _load_asset_detail_size(asset)
                if asset_size == nc_filesize:
                    detailed_size_matches.append(asset)
            size_matches = detailed_size_matches

        if len(size_matches) == 1:
            return size_matches[0]["id"]

        if len(size_matches) > 1:
            logger.warning(
                f"Multiple Immich assets match filename and filesize for {filename}. "
                f"Candidates: {[a['id'] for a in size_matches]}"
            )
            exact = size_matches

    # Step 4: If Nextcloud checksum is available, convert and filter
    if nc_checksum:
        try:
            algo, hex_value = nc_checksum.split(":", 1)
            immich_checksum = base64.b64encode(bytes.fromhex(hex_value)).decode()

            checksum_matches = [
                a for a in exact
                if a.get("checksum") == immich_checksum
            ]

            if len(checksum_matches) == 1:
                return checksum_matches[0]["id"]

            if len(checksum_matches) > 1:
                logger.warning(
                    f"Multiple Immich assets match filename and checksum for {filename}. "
                    f"Candidates: {[a['id'] for a in checksum_matches]}"
                )
                return None

        except Exception as e:
            logger.error(f"Checksum parsing failed for {filename}: {e}")

    # Step 5: If only one exact filename match, return it
    if len(exact) == 1:
        return exact[0]["id"]

    # Step 6: Multiple exact matches remain → ambiguous
    logger.warning(
        f"Multiple exact filename matches for {filename}. "
        f"Candidates: {[a['id'] for a in exact]}. Skipping."
    )
    return None


def get_or_create_album(immich_url, immich_token, album_name):
    headers = immich_headers(immich_token)
    # Check existing albums
    r = requests.get(f"{immich_url}/api/albums", headers=headers)
    if r.status_code == 200:
        albums = r.json()
        for album in albums:
            if album.get("albumName") == album_name:
                return album.get("id")
    # Create new album
    data = {"albumName": album_name, "assetIds": []}
    r = requests.post(f"{immich_url}/api/albums", headers=headers, json=data)
    if r.status_code in (200, 201):
        return r.json().get("id")
    logger.error(f"Album creation failed: {r.status_code} {r.text}")
    return None


def add_assets_to_album(immich_url, immich_token, album_id, asset_ids):
    if not asset_ids:
        return 200, "No assets to add"
    headers = immich_headers(immich_token)
    # Batch add
    data = {"ids": asset_ids}
    r = requests.put(f"{immich_url}/api/albums/{album_id}/assets", headers=headers, json=data)
    return r.status_code, r.text


def remove_assets_from_album(immich_url, immich_token, album_id, asset_ids):
    if not asset_ids:
        return 200, "No assets to remove"
    headers = immich_headers(immich_token)
    data = {"ids": asset_ids}
    r = requests.delete(f"{immich_url}/api/albums/{album_id}/assets", headers=headers, json=data)
    return r.status_code, r.text


def delete_album(immich_url, immich_token, album_id):
    headers = immich_headers(immich_token)
    r = requests.delete(f"{immich_url}/api/albums/{album_id}", headers=headers)
    return r.status_code, r.text
    
# -----------------------------
# DB HELPERS (VIA docker exec)
# -----------------------------

def run_db_query(sql, params=None):
    """
    Execute a SQL query against Nextcloud Postgres and return rows.
    Uses psycopg with env-provided connection settings.
    """
    try:
        with psycopg.connect(
            host=DB_HOST,
            port=DB_PORT,
            user=DB_USER,
            password=DB_PASSWORD,
            dbname=DB_NAME,
            connect_timeout=10,
        ) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params or ())
                return cur.fetchall()
    except Exception as e:
        logger.error(f"DB query failed: {e}")
        return []


def get_system_tags_db():
    """
    Return dict {tag_id: tag_name} from oc_systemtag.
    """
    sql = "SELECT id, name FROM oc_systemtag;"
    rows = run_db_query(sql)
    tags = {}

    if not rows:
        logger.warning("No system tags returned from DB.")
        return tags

    for tid, name in rows:
        try:
            tid_int = int(tid)
        except Exception:
            continue
        name = name or f"Tag-{tid_int}"
        tags[tid_int] = name

    return tags


def get_files_for_tag_db(username, tagid):
    """
        Return a list of (path, checksum, filesize) tuples for a given tag and Nextcloud username.
    Only includes the user's home storage and 'files/' entries.
    """

    sql = (
        """
                SELECT f.path, f.checksum, f.size
        FROM oc_systemtag_object_mapping m
        JOIN oc_filecache f ON f.fileid = m.objectid::bigint
        JOIN oc_storages s ON s.numeric_id = f.storage
        WHERE m.systemtagid = %s
          AND m.objecttype = 'files'
          AND s.id = %s
                    AND f.path LIKE 'files/%%';
        """
    )
    params = (int(tagid), f"home::{username}")

    rows = run_db_query(sql, params)
    results = []

    if not rows:
        return results

    for path, checksum, filesize in rows:
        if not path:
            continue
        normalized_filesize = None
        if filesize is not None:
            try:
                normalized_filesize = int(filesize)
            except Exception:
                normalized_filesize = None
        results.append((path, checksum or None, normalized_filesize))

    return results

def copy_nextcloud_tags_to_immich(dry_run=None):
    logger.info("Starting Nextcloud → Immich album sync run (DB-verified tags)...")

    with open(CONFIG_FILE, "r") as f:
        users = json.load(f)

    # System tags are global, so we can fetch once
    global_tags = get_system_tags_db()
    logger.info(f"Retrieved {len(global_tags)} system tags from DB.")

    for user in users:
        username = user["nextcloud_username"]
        immich_url = user["immich_url"]
        immich_token = user["immich_token"]
        effective_dry_run = user.get("dry_run", False) if dry_run is None else dry_run
        whitelist = set(user.get("whitelist_albums", []))

        logger.info(f"[{username}] Starting sync for user.")
        logger.info(f"[{username}] Dry-run mode: {effective_dry_run}")
        logger.info(f"[{username}] Whitelist: {', '.join(whitelist) if whitelist else 'None'}")

        # Validate Immich token early
        if not test_immich_token(immich_url, immich_token):
            logger.error(f"[{username}] Aborting: invalid Immich token or URL.")
            continue

        # -----------------------------
        # CLEAN 1:1 SYNC FOR EACH TAG
        # -----------------------------

        for tid, tag_name in global_tags.items():
            logger.info(f"[{username}]-{tag_name} Syncing tag '{tag_name}' ({tid})")

            # 1. Get files for this tag and user from DB
            nc_files = get_files_for_tag_db(username, tid)
            if not nc_files:
                logger.info(f"[{username}]-{tag_name} No files for tag '{tag_name}'. Skipping.")
                continue

            # 2. Resolve Immich album
            album_id = get_or_create_album(immich_url, immich_token, tag_name)
            if not album_id:
                logger.error(f"[{username}]-{tag_name} Cannot sync tag '{tag_name}': album unavailable.")
                continue

            # 3. Convert NC files → Immich asset IDs (checksum-aware)
            desired_assets = set()

            for path, nc_checksum, nc_filesize in nc_files:
                filename = unquote(path.split("/")[-1])

                # Pass checksum and size into lookup function
                asset_id = lookup_asset_id_in_immich(
                    immich_url,
                    immich_token,
                    filename,
                    nc_checksum=nc_checksum,
                    nc_filesize=nc_filesize,
                )

                if asset_id:
                    desired_assets.add(asset_id)
                else:
                    logger.warning(
                        f"[{username}]-{tag_name} No Immich asset found for {filename} "
                        f"(size={nc_filesize}, checksum={nc_checksum})"
                    )

            headers = immich_headers(immich_token)

            # 4. Get current Immich album contents
            r = requests.get(f"{immich_url}/api/albums/{album_id}", headers=headers)
            if r.status_code != 200:
                logger.error(f"[{username}]-{tag_name} Failed to fetch album contents for '{tag_name}': {r.status_code} {r.text}")
                continue

            album_data = r.json()
            current_assets = {a["id"] for a in album_data.get("assets", [])}

            # 5. Compute differences
            to_add = desired_assets - current_assets
            to_remove = current_assets - desired_assets

            logger.info(
                f"[{username}]-{tag_name} Album '{tag_name}': "
                f"{len(desired_assets)} desired, "
                f"{len(current_assets)} current, "
                f"{len(to_add)} to add, "
                f"{len(to_remove)} to remove."
            )

            # 6. Apply additions
            if to_add:
                if effective_dry_run:
                    logger.info(f"[{username}]-{tag_name} DRY RUN: Would add {len(to_add)} assets to '{tag_name}'")
                else:
                    status, resp = add_assets_to_album(
                        immich_url, immich_token, album_id, list(to_add)
                    )
                    logger.info(f"[{username}]-{tag_name} Added {len(to_add)} assets → '{tag_name}' (status {status})")

            # 7. Apply removals
            if to_remove:
                if effective_dry_run:
                    logger.info(f"[{username}]-{tag_name} DRY RUN: Would remove {len(to_remove)} assets from '{tag_name}'")
                else:
                    status, resp = remove_assets_from_album(
                        immich_url, immich_token, album_id, list(to_remove)
                    )
                    if status in (200, 204):
                        logger.info(f"[{username}]-{tag_name} Removed {len(to_remove)} assets from '{tag_name}'")
                    else:
                        logger.error(
                            f"[{username}]-{tag_name} Failed to remove {len(to_remove)} assets from '{tag_name}': "
                            f"{status} {resp}"
                        )

        # -----------------------------
        # DELETE ALBUMS NOT IN NEXTCLOUD TAG LIST
        # -----------------------------

        logger.info(f"[{username}] Checking for stale Immich albums to delete...")

        # Build a set of valid album names from Nextcloud tags
        valid_album_names = set(global_tags.values())

        # Fetch all Immich albums for this user
        headers = immich_headers(immich_token)
        r = requests.get(f"{immich_url}/api/albums", headers=headers)
        if r.status_code != 200:
            logger.error(f"[{username}] Failed to list Immich albums for cleanup: {r.status_code} {r.text}")
            continue

        immich_albums = r.json()

        for album in immich_albums:
            album_name = album.get("albumName")
            album_id = album.get("id")

            # Skip whitelisted albums
            if album_name in whitelist:
                logger.info(f"[{username}] Skipping whitelisted album '{album_name}'.")
                continue

            # Skip albums that match Nextcloud tags
            if album_name in valid_album_names:
                continue

            # Skip Immich special albums
            if album.get("albumType") == "favorite":
                logger.info(f"[{username}] Skipping Immich special album '{album_name}'.")
                continue

            # Album is stale
            logger.warning(f"[{username}] Stale album detected: '{album_name}' ({album_id}) — no matching Nextcloud tag.")

            if effective_dry_run:
                logger.warning(f"[{username}] DRY RUN: Would delete album '{album_name}' ({album_id})")
                continue

            # Perform deletion
            status, resp = delete_album(immich_url, immich_token, album_id)
            logger.info(f"[{username}] Deleted album '{album_name}'. Status: {status}")

    logger.info("Album sync run complete (DB-verified).")
