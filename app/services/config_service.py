"""
Config service.

Read / validate / **atomically** write the JSON config files the sync jobs
consume, so they can be edited through the API + web editor instead of being
hand-edited over SFTP:

  * mapping.json      — album -> hierarchical tag mapping (see immich_service).
  * user_config.json  — array of per-user Immich/Nextcloud settings.

Writes go to a temp file in the same directory and are swapped in with
os.replace(), so a dropped connection mid-save can never leave a half-written
(corrupt) config behind. Validation runs before the write, so a structurally
bad payload is rejected without touching the live file.
"""

from __future__ import annotations

import os
import json
import tempfile
import logging

logger = logging.getLogger("ConfigService")

# Mirror the env-var defaults used by the sync services so the editor always
# targets the same files the jobs read.
MAPPING_FILE = os.environ.get("MAPPING_FILE", os.path.join("/config", "mapping.json"))
CONFIG_FILE = os.environ.get("CONFIG_FILE", os.path.join("/config", "user_config.json"))

REQUIRED_USER_KEYS = (
    "immich_url",
    "immich_token",
    "nextcloud_username",
    "nextcloud_file_path",
)


class ConfigValidationError(ValueError):
    """Raised when a submitted config fails structural validation."""


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

def _read_json(path, default):
    if not os.path.isfile(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def read_mapping():
    return _read_json(MAPPING_FILE, {})


def read_user_config():
    return _read_json(CONFIG_FILE, [])


# ---------------------------------------------------------------------------
# Validate
# ---------------------------------------------------------------------------

def validate_mapping(data):
    """
    Enforce the shape find_hierarchical_tag() expects: a top-level object whose
    values are either nested objects (same rule, recursively) or lists of
    string leaves. Anything else would silently map nothing, so reject it here.
    """
    if not isinstance(data, dict):
        raise ConfigValidationError("Mapping must be a JSON object at the top level.")
    _validate_mapping_subtree(data, path="")


def _validate_mapping_subtree(node, path):
    for key, value in node.items():
        here = f"{path}/{key}" if path else key
        if isinstance(value, dict):
            _validate_mapping_subtree(value, here)
        elif isinstance(value, list):
            for child in value:
                if not isinstance(child, str):
                    raise ConfigValidationError(
                        f"List entries under '{here}' must be strings."
                    )
        else:
            raise ConfigValidationError(
                f"Value at '{here}' must be an object or a list of strings, "
                f"got {type(value).__name__}."
            )


def validate_user_config(data):
    if not isinstance(data, list):
        raise ConfigValidationError("User config must be a JSON array of user objects.")
    for i, user in enumerate(data):
        if not isinstance(user, dict):
            raise ConfigValidationError(f"User entry #{i} must be an object.")
        missing = [k for k in REQUIRED_USER_KEYS if not user.get(k)]
        if missing:
            raise ConfigValidationError(
                f"User entry #{i} is missing required key(s): {', '.join(missing)}."
            )
        if "dry_run" in user and not isinstance(user["dry_run"], bool):
            raise ConfigValidationError(f"User entry #{i}: 'dry_run' must be true or false.")
        if "whitelist_albums" in user and not isinstance(user["whitelist_albums"], list):
            raise ConfigValidationError(f"User entry #{i}: 'whitelist_albums' must be a list.")


# ---------------------------------------------------------------------------
# Atomic write
# ---------------------------------------------------------------------------

def _atomic_write_json(path, data):
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-config-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, path)  # atomic on the same filesystem
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    logger.info(f"Wrote config file: {path}")


def write_mapping(data):
    validate_mapping(data)
    _atomic_write_json(MAPPING_FILE, data)


def write_user_config(data):
    validate_user_config(data)
    _atomic_write_json(CONFIG_FILE, data)
