#!/usr/bin/env bash

# Deploy all top-level EuroPi experimental modules to a connected Pico/Pico 2.
# Usage: ./scripts/linux/deploy_experimental.sh [device]

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd)"
SOURCE_DIR="$PROJECT_ROOT/software/firmware/experimental"
DEVICE="${1:-auto}"

if (( $# > 1 )); then
    echo "Usage: $0 [device]" >&2
    exit 2
fi

if ! command -v mpremote >/dev/null 2>&1; then
    echo "Error: mpremote is not installed. Run: python3 -m pip install mpremote" >&2
    exit 1
fi

shopt -s nullglob
SOURCE_FILES=("$SOURCE_DIR"/*.py)
if (( ${#SOURCE_FILES[@]} == 0 )); then
    echo "Error: no Python files found in $SOURCE_DIR" >&2
    exit 1
fi

REMOTE_SETUP=$'import os\nfor path in ("/lib", "/lib/experimental"):\n    try:\n        os.stat(path)\n    except OSError:\n        os.mkdir(path)'

echo "Deploying ${#SOURCE_FILES[@]} experimental modules to ${DEVICE}..."
mpremote connect "$DEVICE" \
    + exec "$REMOTE_SETUP" \
    + \
    fs cp "${SOURCE_FILES[@]}" :/lib/experimental/

echo "Experimental firmware deployment complete."
