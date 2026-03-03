# SyncNextcloudImmich

Containerized tools to sync Nextcloud files and tags with Immich.

## Contents
- `nextcloud_immich_album_sync.py`: Sync Immich albums to mirror Nextcloud system tags (uses Postgres).
- `immich_album_tag_sync.py`: Apply hierarchical Immich tags to assets based on album names.
- `nextcloud_immich_file_sync.sh`: Upload files from Nextcloud datasets to Immich via `immich-go`.

## Quick Start (Docker)

1) Build the image

```bash
docker build -t immich-sync:local .
```
1.5) Download immich-go (Linux) and mount it (required for file uploads)

`make install` now downloads and extracts `immich-go` automatically to `tools/immich-go/immich-go` if it is missing.

You can also download/update it explicitly with:

```bash
make immich-go
```

Pin a specific release if `latest` has issues:

```bash
make immich-go VERSION=v0.108.0
```

On Windows PowerShell, you can also fetch the latest Linux binary manually:

```powershell
pwsh scripts/get-immich-go.ps1 -Version latest -OutputDir tools/immich-go
```

On Linux/macOS:

```bash
bash scripts/get-immich-go.sh --version latest --output-dir tools/immich-go
```

Then enable the optional volume in `docker-compose.yml`:

```
	volumes:
		- ./config:/config:rw
		- ./tools/immich-go/immich-go:/usr/local/bin/immich-go:ro
```

Alternatively, pass the mount to `docker run`:

```powershell
docker run --rm \
	-e IMMICH_SERVER=https://immich.example.com \
	-v ${PWD}/tools/immich-go/immich-go:/usr/local/bin/immich-go:ro \
	-v ${PWD}/config:/config \
	immich-sync:local file-sync
```


2) Prepare config
- Copy examples and edit values:

```bash
cp config/user_config.example.json config/user_config.json
cp config/mapping.example.json config/mapping.json
cp config/upload_map.example.txt config/upload_map.txt
cp config/cron.example config/cron
```

3) Run one-shot jobs

```bash
# Album → hierarchical tag sync
- `immich_file_sync.py`: Upload files from Nextcloud datasets to Immich via `immich-go`.
	-e IMMICH_SERVER=https://immich.example.com \
	-e LOG_LEVEL=INFO \
	-e LEAF_ONLY_TAGGING=true \
	-v $(pwd)/config:/config \
	immich-sync:local album-sync

# Nextcloud → Immich album sync (requires DB access)
docker run --rm \
	-e NEXTCLOUD_DB_HOST=nextcloud-db.local \
	-e NEXTCLOUD_DB_PORT=5432 \
	-e NEXTCLOUD_DB_NAME=nextcloud \
	-e NEXTCLOUD_DB_USER=nextcloud \
	-e NEXTCLOUD_DB_PASSWORD=secret \
	-v $(pwd)/config:/config \
	immich-sync:local tag-sync

# File uploads via immich-go (mount Nextcloud datasets read-only)
docker run --rm \
	-e IMMICH_SERVER=https://immich.example.com \
	-v /path/to/cloud/data/User1/files:/data/nextcloud/User1:ro \
	-v /path/to/cloud/data/User2/files:/data/nextcloud/User2:ro \
	-v $(pwd)/config:/config \
	immich-sync:local file-sync

# Healthcheck
docker run --rm \
	-e IMMICH_SERVER=https://immich.example.com \
	-e NEXTCLOUD_DB_HOST=nextcloud-db.example.local \
	-e NEXTCLOUD_DB_NAME=nextcloud \
	-e NEXTCLOUD_DB_USER=nextcloud \
	-e NEXTCLOUD_DB_PASSWORD=secret \
	-v $(pwd)/config:/config \
	immich-sync:local healthcheck
```

## Compose Scheduler

Edit `docker-compose.yml` to mount datasets and `/config`, then start:

```bash
docker compose up -d --build
```

The service runs `supercronic` using `/config/cron` to schedule the three commands.

Tip: If GitHub API rate limits you while downloading `immich-go`, set a token:

```powershell
$env:GITHUB_TOKEN = "ghp_..."
pwsh scripts/get-immich-go.ps1 -Version latest -OutputDir tools/immich-go
```

```bash
export GITHUB_TOKEN="ghp_..."
bash scripts/get-immich-go.sh --version latest --output-dir tools/immich-go
```

## Environment & Config

- Immich server: `IMMICH_SERVER=https://immich.example.com`
- Hierarchy behavior: `LEAF_ONLY_TAGGING=true` (default) to apply only leaf mapped tags; set `false` to also apply parent mapped tags
- Nextcloud DB: `NEXTCLOUD_DB_HOST`, `NEXTCLOUD_DB_PORT`, `NEXTCLOUD_DB_NAME`, `NEXTCLOUD_DB_USER`, `NEXTCLOUD_DB_PASSWORD`
- Paths in container:
	- Config directory: `/config`
	- Optional cache: `/cache`
- Files in `/config`:
	- `user_config.json`: per-user Immich credentials, whitelist, dry-run
	- `mapping.json`: album → hierarchical tag mapping
	- `upload_map.txt`: lines `NEXTCLOUD_PATH IMMICH_API_KEY`
	- `cron`: supercronic schedule file

# Optional path rewrite if your mapping uses host paths
docker run --rm \
  -e IMMICH_SERVER=https://immich.example.com \
  -e PATH_REWRITE_FROM=/path/to/cloud/data \
  -e PATH_REWRITE_TO=/data/nextcloud \
  -v /path/to/cloud/data/User1/files:/data/nextcloud/User1:ro \
  -v /path/to/cloud/data/User2/files:/data/nextcloud/User2:ro \
  -v $(pwd)/config:/config \
  immich-sync:local file-sync

## TrueNAS SCALE

- Recommended: deploy via Apps and create three CronJobs (album-sync, tag-sync, file-sync) with the same image and required volumes/envs.
- Alternative: deploy the Compose stack above via the Docker service.

## Notes
- Logs are written to stdout; set `LOG_TO_FILE=true` to also write to `/config/*.log`.
- `nextcloud_immich_album_sync.py` uses direct Postgres access (no `docker exec`). Ensure network connectivity and credentials are valid.

for window:
	install git
	install chocolatey
	install pipenv
	ensure  C:\Program Files\Git\bin is added to PATH
run make install
run make start
