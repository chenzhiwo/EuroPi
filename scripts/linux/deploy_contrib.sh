#!/usr/bin/env bash

# Deploy all EuroPi contrib Python modules to a connected Pico/Pico 2.
# Usage: ./scripts/linux/deploy_contrib.sh [device]

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd)"
SOURCE_DIR="$PROJECT_ROOT/software/contrib"
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

REMOTE_SETUP=$'import os\nfor path in ("/lib", "/lib/contrib"):\n    try:\n        os.stat(path)\n    except OSError:\n        os.mkdir(path)'

echo "Deploying ${#SOURCE_FILES[@]} contrib modules to ${DEVICE}..."
mpremote connect "$DEVICE" \
    + exec "$REMOTE_SETUP" \
    + \
    fs cp "${SOURCE_FILES[@]}" :/lib/contrib/

echo "Contrib deployment complete."
