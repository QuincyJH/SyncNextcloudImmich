# SyncNextcloudImmich

Containerized FastAPI service that syncs files and tags between **Nextcloud** and **Immich**.

It exposes three jobs as HTTP endpoints, each meant to be triggered on a schedule (e.g. cron hitting the API):

| Job | Endpoint | What it does |
| --- | --- | --- |
| **File upload** | `POST /sync/` | Uploads each user's Nextcloud folder into Immich via the external `immich-go` binary. |
| **Tags → albums** | `POST /sync/copy-tags` | Reads Nextcloud **system tags** from the Nextcloud Postgres DB and mirrors them into Immich **albums** (one album per tag). |
| **Albums → tags** | `POST /immich/` | Applies nested Immich **tags** to album assets, driven by `mapping.json`. |

The two sync directions are inverse: Nextcloud tags become Immich albums; Immich albums become Immich hierarchical tags.

> **Note:** all jobs run **synchronously** — the HTTP request blocks until the job finishes and returns a status string. There is no background queue.

## Getting Started

### 1. Build the image

```bash
docker build -t immich-sync:local .
```

### 2. Download `immich-go` (required for file uploads)

`immich-go` is a separate Go binary (not a Python package). It must be downloaded and mounted into the container.

`make install` downloads it automatically to `tools/immich-go/immich-go` if missing. You can also fetch it explicitly:

```bash
make immich-go                    # latest
make immich-go VERSION=v0.108.0   # pin a release
```

Manually, per platform:

```powershell
pwsh scripts/get-immich-go.ps1 -Version latest -OutputDir tools/immich-go
```

```bash
bash scripts/get-immich-go.sh --version latest --output-dir tools/immich-go
```

If GitHub rate-limits the download, set a token first (`$env:GITHUB_TOKEN` / `export GITHUB_TOKEN=`).

The binary is mounted via `docker-compose.yml` to `/tools/immich-go` and selected with `IMMICH_GO_BIN=/tools/immich-go/immich-go`.

### 3. Prepare config

Copy the examples in `config/` and edit values:

```bash
cp config/user_config.example.json config/user_config.json
cp config/mapping.example.json config/mapping.json
```

- **`user_config.json`** — JSON **array of users**. Every job loops over all users, so one deployment can serve multiple accounts/servers. Per-user keys: `immich_url`, `immich_token`, `nextcloud_username`, `nextcloud_file_path`, `dry_run`, `whitelist_albums` (albums never deleted as "stale" during tag sync), and optional `leaf_only_tagging`.
- **`mapping.json`** — album-name → nested-tag hierarchy for the albums→tags job. Supports arbitrarily nested dicts and `[list]` leaves. Matching is case- and whitespace-insensitive.

Nextcloud DB credentials are supplied via environment variables (see below), **not** config files.

### 4. Run

```bash
docker compose up -d --build
```

The service listens on port `8000`. `make start` runs `docker compose up --build --watch`, which live-reloads on edits to `app/` and `config/`.

To run the API without Docker:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

## Triggering jobs

All jobs are `POST` requests. `dry_run=true` simulates without writing; if omitted, each user's `dry_run` config value applies.

```bash
# File upload
curl -X POST "http://localhost:8000/sync/?dry_run=true"

# Nextcloud tags → Immich albums
curl -X POST "http://localhost:8000/sync/copy-tags?dry_run=true"

# Immich albums → hierarchical tags
curl -X POST "http://localhost:8000/immich/"

# Delete ALL Immich tags (destructive)
curl -X POST "http://localhost:8000/immich/clear?dry_run=true"

# Health
curl "http://localhost:8000/health/"
curl "http://localhost:8000/health/dependencies"   # checks immich-go availability
```

A [Bruno](https://www.usebruno.com/) collection for exercising these endpoints lives in the `SyncNextcloudImmich/` directory.

## Editing config without SFTP

Instead of pulling `mapping.json` / `user_config.json` over SFTP to edit them, the service exposes a small token-protected editor.

1. Set a secret in your `.env`: `CONFIG_API_TOKEN=<a long random value>` (if unset, the config API is disabled and returns `503`).
2. Open **`http://<host>:8000/ui`** in a browser, paste the token, and edit either file in the textarea. **Load** fetches the current file, **Validate JSON** checks syntax client-side, **Save** writes it.

Saves are validated server-side (mapping must be nested objects / string-list leaves; user config must be an array with the required per-user keys) and written **atomically**, so a malformed or interrupted save can't corrupt the live file. The same operations are available directly:

```bash
curl -H "X-Config-Token: $CONFIG_API_TOKEN" http://localhost:8000/config/mapping
curl -X PUT -H "X-Config-Token: $CONFIG_API_TOKEN" -H "Content-Type: application/json" \
  -d @config/mapping.json http://localhost:8000/config/mapping
# also: /config/user-config
```

> The user-config view exposes Immich API tokens. Keep `CONFIG_API_TOKEN` secret and the port off the public internet.

## Scheduling

Point any scheduler at the running container's endpoints. Example crontab (UTC assumed inside the container; set `TZ` to change):

```cron
# Albums → hierarchical tags, hourly
0 * * * * curl -fsS -X POST http://localhost:8000/immich/
# Nextcloud tags → Immich albums, daily at 02:00
0 2 * * * curl -fsS -X POST http://localhost:8000/sync/copy-tags
# File uploads, daily at 03:00
0 3 * * * curl -fsS -X POST http://localhost:8000/sync/
```

## Environment & Config

- **Immich:** `IMMICH_SERVER=https://immich.example.com`
- **immich-go:** `IMMICH_GO_BIN` (path to the binary; default `immich-go` on `PATH`)
- **Nextcloud DB:** `NEXTCLOUD_DB_HOST`, `NEXTCLOUD_DB_PORT` (default 5432), `NEXTCLOUD_DB_NAME`, `NEXTCLOUD_DB_USER`, `NEXTCLOUD_DB_PASSWORD`
- **Tag hierarchy:** `LEAF_ONLY_TAGGING=true` (default) applies only leaf mapped tags; set `false` to also apply parent mapped tags
- **Config editor:** `CONFIG_API_TOKEN` — shared secret for the `/config` endpoints and `/ui` editor; if unset, that API is disabled
- **Logging:** `LOG_LEVEL` (default `INFO`); `LOG_TO_FILE=true` also writes to `/config/*.log`
- **Performance tunables:** `IMMICH_PAGE_SIZE` (1000), `ALBUM_PARALLELISM` (8), `HTTP_POOL_SIZE` (16)
- **Container paths:** config directory `/config`, optional cache `/cache`

Copy `.env.example` to `.env` for `docker-compose.yml` variable substitution.

## Healthcheck script

`app/healthcheck.py` is a standalone script (run directly, not via the API) that verifies Immich reachability for each configured user and, if DB env vars are set, Nextcloud DB connectivity:

```bash
python app/healthcheck.py
```

## TrueNAS SCALE

- **Recommended:** deploy via Apps as a long-running service, then create CronJobs (or external cron) that `curl` the endpoints on a schedule.
- **Alternative:** deploy the Compose stack above via the Docker service.

## CI / releases

`.github/workflows/publish_image.yml` builds and pushes a Docker image to **ghcr.io** on every push to `main` (tagged `latest`) and on published GitHub releases (tagged with the version, `v` prefix stripped).

## Notes

- `sync_service.copy_nextcloud_tags_to_immich` uses **direct Postgres access** to Nextcloud (no `docker exec`, no Nextcloud API). Ensure network connectivity and valid credentials, and that the schema matches Nextcloud's `oc_systemtag*` / `oc_filecache` / `oc_storages` tables.
- Asset matching maps Nextcloud files to Immich assets by checksum → (filename, size) → unique filename.
- Logs are written to stdout.

### Windows local dev

```
install git
install chocolatey
install pipenv
ensure C:\Program Files\Git\bin is on PATH
make install
make start
```
