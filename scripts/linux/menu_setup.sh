#!/usr/bin/env bash

# Install the EuroPi menu as /main.py on a connected Pico/Pico 2.
# Usage: ./scripts/linux/menu_setup.sh [device]

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd)"
MENU_FILE="$PROJECT_ROOT/software/contrib/menu.py"
DEVICE="${1:-auto}"

if (( $# > 1 )); then
    echo "Usage: $0 [device]" >&2
    exit 2
fi

if ! command -v mpremote >/dev/null 2>&1; then
    echo "Error: mpremote is not installed. Run: python3 -m pip install mpremote" >&2
    exit 1
fi

echo "Installing the EuroPi menu on ${DEVICE}..."
mpremote connect "$DEVICE" \
    + fs cp "$MENU_FILE" :/main.py \
    + soft-reset

echo "Menu installation complete."
