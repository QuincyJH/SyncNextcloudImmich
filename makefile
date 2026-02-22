default: install

VERSION ?= latest

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
	