#!/usr/bin/env bash

# Deploy all EuroPi configuration files to a connected Pico/Pico 2.
# Usage: ./scripts/linux/deploy_config.sh [device]
# Example: ./scripts/linux/deploy_config.sh /dev/ttyACM0

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
DEVICE="${1:-auto}"

if (( $# > 1 )); then
    echo "Usage: $0 [device]" >&2
    exit 2
fi

if ! command -v mpremote >/dev/null 2>&1; then
    echo "Error: mpremote is not installed. Run: python3 -m pip install mpremote" >&2
    exit 1
fi

REMOTE_SETUP=$'import os\ntry:\n    os.stat("/config")\nexcept OSError:\n    os.mkdir("/config")'

echo "Deploying configuration files to ${DEVICE}..."
mpremote connect "$DEVICE" \
    + exec "$REMOTE_SETUP" \
    + \
    fs cp \
        "$SCRIPT_DIR/EuroPiConfig.json" \
        "$SCRIPT_DIR/ExperimentalConfig.json" \
        "$SCRIPT_DIR/Diagnostic.json" \
        :/config/

echo "Configuration deployment complete."
echo "Verify with: mpremote connect '$DEVICE' fs ls :/config"
