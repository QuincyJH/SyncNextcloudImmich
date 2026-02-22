#!/bin/bash

PIPENV_CMD=()

function main() {
    ensure_immich_go || handle_exception
    ensure_pipenv || handle_exception
    "${PIPENV_CMD[@]}" install || handle_exception
}

function ensure_pipenv() {
    if command -v pipenv >/dev/null 2>&1; then
        PIPENV_CMD=(pipenv)
        return 0
    fi

    if command -v py >/dev/null 2>&1; then
        echo "pipenv not found; installing via py -m pip ..."
        py -m pip install --user pipenv || return 1
        PIPENV_CMD=(py -m pipenv)
        return 0
    fi

    if command -v python3 >/dev/null 2>&1; then
        echo "pipenv not found; installing via python3 -m pip ..."
        python3 -m pip install --user pipenv || return 1
        PIPENV_CMD=(python3 -m pipenv)
        return 0
    fi

    if command -v python >/dev/null 2>&1; then
        echo "pipenv not found; installing via python -m pip ..."
        python -m pip install --user pipenv || return 1
        PIPENV_CMD=(python -m pipenv)
        return 0
    fi

    echo "pipenv is unavailable and no Python interpreter was found to bootstrap it."
    return 1
}

function ensure_immich_go() {
    local IMMICH_GO_PATH="tools/immich-go/immich-go"
    if [ -f "$IMMICH_GO_PATH" ]; then
        echo "immich-go binary already present at $IMMICH_GO_PATH"
        return 0
    fi

    if command -v pwsh >/dev/null 2>&1; then
        echo "immich-go binary not found; downloading automatically..."
        pwsh -NoProfile -ExecutionPolicy Bypass -File ./scripts/get-immich-go.ps1 -Version latest -OutputDir tools/immich-go
        return $?
    fi

    if command -v powershell.exe >/dev/null 2>&1; then
        echo "immich-go binary not found; downloading automatically via powershell.exe..."
        powershell.exe -NoProfile -ExecutionPolicy Bypass -File ./scripts/get-immich-go.ps1 -Version latest -OutputDir tools/immich-go
        return $?
    fi

    if [ -f "./scripts/get-immich-go.sh" ]; then
        echo "immich-go binary not found; downloading automatically..."
        bash ./scripts/get-immich-go.sh --version latest --output-dir tools/immich-go
        return $?
    fi

    echo "immich-go binary not found at $IMMICH_GO_PATH and no downloader is available (pwsh or get-immich-go.sh)."
    return 1
}

function handle_exception() {
    echo "An error occurred while installing. Exiting..."
    exit 1
}

main