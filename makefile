default: install

VERSION ?= latest
RELEASE_ARGS = $(if $(RELEASE_VERSION),-Version $(RELEASE_VERSION))
# PowerShell 7 if present, else Windows PowerShell 5.1 (release.ps1 supports both).
PWSH = $(shell command -v pwsh >/dev/null 2>&1 && echo pwsh || echo powershell)

ifeq ($(OS),Windows_NT)
IMMICH_GO_CMD = pwsh -NoProfile -ExecutionPolicy Bypass -File ./scripts/get-immich-go.ps1 -Version $(VERSION) -OutputDir tools/immich-go
else
IMMICH_GO_CMD = bash ./scripts/get-immich-go.sh --version $(VERSION) --output-dir tools/immich-go
endif

install:
	@bash ./scripts/install.sh

immich-go:
	@$(IMMICH_GO_CMD)

start:
	@bash ./scripts/start.sh

# Cut a release: tags HEAD and publishes a GitHub Release so CI builds the
# versioned ghcr.io image. Needs GITHUB_TOKEN. Override the auto version with
# `make release RELEASE_VERSION=2026.09.17.2`; preview with `make release-dry`.
release:
	@$(PWSH) -NoProfile -ExecutionPolicy Bypass -File ./scripts/release.ps1 $(RELEASE_ARGS)

release-dry:
	@$(PWSH) -NoProfile -ExecutionPolicy Bypass -File ./scripts/release.ps1 -DryRun $(RELEASE_ARGS)
