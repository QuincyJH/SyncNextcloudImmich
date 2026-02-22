#!/usr/bin/env bash
set -euo pipefail

VERSION="latest"
OUTPUT_DIR="tools/immich-go"
REPO="simulot/immich-go"
ARCH=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --version)
      VERSION="$2"; shift 2 ;;
    --output-dir)
      OUTPUT_DIR="$2"; shift 2 ;;
    --repo)
      REPO="$2"; shift 2 ;;
    --arch)
      ARCH="$2"; shift 2 ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 1 ;;
  esac
done

resolve_arch() {
  if [[ -n "$ARCH" ]]; then
    echo "$ARCH"
    return
  fi

  if command -v docker >/dev/null 2>&1; then
    local docker_arch
    docker_arch="$(docker version --format '{{.Server.Arch}}' 2>/dev/null || true)"
    case "$docker_arch" in
      x86_64|amd64) echo "amd64"; return ;;
      arm64|aarch64) echo "arm64"; return ;;
    esac
  fi

  case "$(uname -m)" in
    x86_64|amd64) echo "amd64" ;;
    arm64|aarch64) echo "arm64" ;;
    *) echo "amd64" ;;
  esac
}

if ! command -v curl >/dev/null 2>&1; then
  echo "curl is required" >&2
  exit 1
fi
if ! command -v tar >/dev/null 2>&1; then
  echo "tar is required" >&2
  exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required" >&2
  exit 1
fi

RESOLVED_ARCH="$(resolve_arch)"
ARCH_ALT="$RESOLVED_ARCH"
case "$RESOLVED_ARCH" in
  amd64) ARCH_ALT="x86_64" ;;
  arm64) ARCH_ALT="aarch64" ;;
esac
API_URL="https://api.github.com/repos/$REPO/releases/latest"
if [[ "$VERSION" != "latest" ]]; then
  API_URL="https://api.github.com/repos/$REPO/releases/tags/$VERSION"
fi

echo "Fetching immich-go metadata from $API_URL ..."
RELEASE_JSON="$(curl -fsSL -H "User-Agent: SyncNextcloudImmich" -H "Accept: application/vnd.github+json" ${GITHUB_TOKEN:+-H "Authorization: Bearer $GITHUB_TOKEN"} "$API_URL")"

ASSET_URL="$(python3 -c 'import json,sys,re; data=json.loads(sys.stdin.read()); arch=sys.argv[1].lower(); alt=sys.argv[2].lower();
for a in data.get("assets", []):
    n=a.get("name","").lower();
  if "linux" in n and (arch in n or alt in n) and n.endswith(".tar.gz"):
        print(a.get("browser_download_url","")); break
' "$RESOLVED_ARCH" "$ARCH_ALT" <<< "$RELEASE_JSON")"

ASSET_NAME="$(python3 -c 'import json,sys; data=json.loads(sys.stdin.read()); arch=sys.argv[1].lower(); alt=sys.argv[2].lower();
for a in data.get("assets", []):
    n=a.get("name","").lower();
  if "linux" in n and (arch in n or alt in n) and n.endswith(".tar.gz"):
        print(a.get("name","")); break
' "$RESOLVED_ARCH" "$ARCH_ALT" <<< "$RELEASE_JSON")"

if [[ -z "$ASSET_URL" ]]; then
  echo "No linux/$RESOLVED_ARCH tar.gz asset found in release." >&2
  exit 1
fi

mkdir -p "$OUTPUT_DIR"
ARCHIVE_PATH="$OUTPUT_DIR/${ASSET_NAME:-immich-go.tar.gz}"
BINARY_PATH="$OUTPUT_DIR/immich-go"

echo "Downloading $ASSET_NAME ..."
curl -fL "$ASSET_URL" -o "$ARCHIVE_PATH"

echo "Extracting archive ..."
tar -xzf "$ARCHIVE_PATH" -C "$OUTPUT_DIR"

if [[ ! -f "$BINARY_PATH" ]]; then
  CANDIDATE="$(find "$OUTPUT_DIR" -type f -name immich-go | head -n 1 || true)"
  if [[ -n "$CANDIDATE" ]]; then
    cp "$CANDIDATE" "$BINARY_PATH"
  fi
fi

if [[ ! -f "$BINARY_PATH" ]]; then
  echo "Download succeeded but immich-go binary was not found after extraction." >&2
  exit 1
fi

chmod +x "$BINARY_PATH"
echo "immich-go ready at: $BINARY_PATH"
