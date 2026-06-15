# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A containerized FastAPI service that synchronizes data between **Nextcloud** and **Immich** (self-hosted photo server). It does three distinct jobs, each exposed as an HTTP endpoint and meant to be triggered on a schedule (cron hitting the API):

1. **File upload** (`/sync/`) — uploads each user's Nextcloud folder into Immich by shelling out to the external `immich-go` binary.
2. **Tag → album sync** (`/sync/copy-tags`) — reads Nextcloud **system tags** directly from the Nextcloud Postgres DB and mirrors them into Immich **albums** (one album per tag).
3. **Album → hierarchical tag sync** (`/immich/`) — reads Immich **albums** and applies nested Immich **tags** to their assets, driven by `mapping.json`.

Note the two sync directions are inverse and use different primitives: Nextcloud tags become Immich albums; Immich albums become Immich hierarchical tags.

## Important: README vs. actual architecture

The `README.md` still documents an older design — one-shot `docker run immich-sync:local <album-sync|tag-sync|file-sync>` subcommands, standalone scripts like `immich_file_sync.py`, and a `supercronic`/`/config/cron` scheduler. **That model no longer matches the code.** The app is now a long-running FastAPI server (`app/main.py`) and all jobs are triggered via HTTP POST. When the README and the code disagree, trust the code. The env vars and `/config` file formats the README documents are still accurate.

## Architecture

Request flow is a thin three-layer stack:

- `app/main.py` — entrypoint; wires CORS and mounts three routers under `/health`, `/sync`, `/immich`.
- `app/server.py` — constructs the `FastAPI` app instance and OpenAPI tag metadata.
- `app/routers/{health,sync,immich}.py` — thin HTTP handlers that immediately delegate to a service function. **Endpoints fire jobs synchronously and return a string status** (e.g. `"sync started"`); there is no background-task queue, so a long sync blocks the request.
- `app/services/sync_service.py` — jobs 1 and 2 (file upload + Nextcloud-tag → Immich-album).
- `app/services/immich_service.py` — job 3 (Immich-album → hierarchical-tag) plus `clear_all_tags`.
- `app/healthcheck.py` — standalone script (run directly, not via the API) that verifies Immich reachability per user and optional Nextcloud DB connectivity.

### Cross-cutting patterns (both service modules)

These conventions repeat across both service files; follow them when adding code:

- **Config is read fresh from disk on every job** from `/config/user_config.json` (path overridable via `CONFIG_FILE`). It's a JSON **array of user objects**; every job loops over all users. Each user carries its own `immich_url` + `immich_token`, so one deployment serves multiple accounts/servers.
- **`dry_run` resolution** is layered: an explicit query-param wins; otherwise the per-user `dry_run` key in config applies. Always thread a `dry_run` path through new mutating operations and log what *would* happen.
- **Performance model** — each service builds state once per user, then parallelizes per-album/per-tag work with a `ThreadPoolExecutor`. The pattern is deliberate: serial pre-pass for anything that mutates shared maps (tag hierarchy, album list), then parallel fan-out for independent per-item HTTP work. Read the module docstrings before changing concurrency — they explain why each step is serial vs. parallel.
- **Immich HTTP** goes through `_make_session()` (a pooled `requests.Session` with the `x-api-key` header). Reuse it; don't make bare `requests.get` calls.
- **Asset matching** (`sync_service.resolve_asset_id`) matches Nextcloud files to Immich assets by checksum → (filename, size) → unique filename, in that order, all in memory against a one-shot asset index. Nextcloud stores checksums as `TYPE:hexvalue`; Immich uses base64.

### External dependencies

- **`immich-go`** — a separate Go binary (not pip-installable) required only for file uploads. Resolved via `IMMICH_GO_BIN` (default `immich-go`); if absent, upload returns rc 127. Downloaded by `scripts/get-immich-go.{sh,ps1}` into `tools/immich-go/` and mounted into the container.
- **Nextcloud Postgres** — `sync_service` connects directly with `psycopg` (no Nextcloud API). The tag queries depend on Nextcloud's schema: `oc_systemtag`, `oc_systemtag_object_mapping`, `oc_filecache`, `oc_storages`, with storage scoped to `home::<username>` and paths under `files/`.

## Commands

```bash
# Local dev setup (Windows: needs git, chocolatey, pipenv on PATH, plus Git's bin dir)
make install          # downloads immich-go + installs Python deps via pipenv
make start            # docker compose up --build --watch (live-reload on app/ + config/ edits)

# Run the API directly (without Docker)
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

# immich-go binary management
make immich-go                      # download/update latest
make immich-go VERSION=v0.108.0     # pin a release

# Build/run container manually
docker build -t immich-sync:local .
docker compose up -d --build

# Run the standalone healthcheck script
python app/healthcheck.py
```

Trigger jobs against a running server (all POST):

```bash
curl -X POST "http://localhost:8000/sync/?dry_run=true"        # file upload (dry run)
curl -X POST "http://localhost:8000/sync/copy-tags?dry_run=true"  # Nextcloud tags → Immich albums
curl -X POST "http://localhost:8000/immich/"                   # Immich albums → hierarchical tags
curl -X POST "http://localhost:8000/immich/clear"              # delete ALL Immich tags (destructive)
curl "http://localhost:8000/health/dependencies"               # check immich-go availability
```

The `SyncNextcloudImmich/` directory (note: same name as repo root) is a **Bruno** API collection — open it in Bruno to exercise the endpoints interactively. There is no automated test suite.

## Config files (`/config`, gitignored — copy from `*.example`)

- `user_config.json` — array of users: `immich_url`, `immich_token`, `nextcloud_username`, `nextcloud_file_path`, `dry_run`, `whitelist_albums` (albums never deleted as "stale"), optional `leaf_only_tagging`.
- `mapping.json` — album-name → nested-tag hierarchy. Supports arbitrarily nested dicts and `[list]` leaves (see `mapping.example.json`). Matching is case/whitespace-insensitive (`_normalize_label`).
- DB credentials come from env vars (`NEXTCLOUD_DB_*`), not config files.

## Key env vars

`IMMICH_SERVER`, `IMMICH_GO_BIN`, `NEXTCLOUD_DB_{HOST,PORT,NAME,USER,PASSWORD}`, `LEAF_ONLY_TAGGING` (default true — apply only leaf mapped tags, skip parent duplication), `LOG_LEVEL`, `LOG_TO_FILE` (+ `LOGFILE`), and tunables `IMMICH_PAGE_SIZE`, `ALBUM_PARALLELISM`, `HTTP_POOL_SIZE`.

## CI / release

`.github/workflows/publish_image.yml` builds and pushes a Docker image to ghcr.io on every push to `main` (tagged `latest`) and on published GitHub releases (tagged with the version, `v` prefix stripped). There is no test/lint CI gate.
