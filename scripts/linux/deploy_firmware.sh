#!/usr/bin/env bash

# Deploy the core EuroPi firmware and tools to a connected Pico/Pico 2.
# Usage: ./scripts/linux/deploy_firmware.sh [device]

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd)"
SOURCE_DIR="$PROJECT_ROOT/software/firmware"
TOOLS_DIR="$SOURCE_DIR/tools"
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
FIRMWARE_FILES=("$SOURCE_DIR"/*.py)
TOOL_FILES=("$TOOLS_DIR"/*.py)
if (( ${#FIRMWARE_FILES[@]} == 0 || ${#TOOL_FILES[@]} == 0 )); then
    echo "Error: firmware or tool Python files are missing under $SOURCE_DIR" >&2
    exit 1
fi

REMOTE_SETUP=$'import os\nfor path in ("/lib", "/lib/tools"):\n    try:\n        os.stat(path)\n    except OSError:\n        os.mkdir(path)'

# Remove core modules misplaced in /lib/tools by the initial Linux deploy script.
for source_file in "${FIRMWARE_FILES[@]}"; do
    filename="${source_file##*/}"
    REMOTE_SETUP+=$'\ntry:\n    os.remove("/lib/tools/'"$filename"$'")\nexcept OSError:\n    pass'
done

echo "Deploying core firmware and tools to ${DEVICE}..."
mpremote connect "$DEVICE" \
    + exec "$REMOTE_SETUP" \
    + \
    fs cp "${FIRMWARE_FILES[@]}" :/lib/ \
    + \
    fs cp "${TOOL_FILES[@]}" :/lib/tools/

echo "Core firmware deployment complete."
