#!/usr/bin/env bash

# Install the ssd1306 OLED driver on a connected Pico/Pico 2.
# Usage: ./scripts/linux/deploy_ssd1306.sh [device]

set -euo pipefail

DEVICE="${1:-auto}"

if (( $# > 1 )); then
    echo "Usage: $0 [device]" >&2
    exit 2
fi

if ! command -v mpremote >/dev/null 2>&1; then
    echo "Error: mpremote is not installed. Run: python3 -m pip install mpremote" >&2
    exit 1
fi

echo "Installing ssd1306 on ${DEVICE}..."
mpremote connect "$DEVICE" mip install ssd1306

echo "ssd1306 installation complete."
