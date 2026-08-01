#!/usr/bin/env bash

# Deploy the complete EuroPi software stack to a connected Pico/Pico 2.
# Usage: ./scripts/linux/deploy_all.sh [device]
# Example: ./scripts/linux/deploy_all.sh /dev/ttyACM0

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
DEVICE="${1:-auto}"

if (( $# > 1 )); then
    echo "Usage: $0 [device]" >&2
    exit 2
fi

STEPS=(
    deploy_ssd1306.sh
    deploy_firmware.sh
    deploy_experimental.sh
    deploy_contrib.sh
    deploy_config.sh
    menu_setup.sh
)

echo "Starting complete EuroPi deployment to ${DEVICE}..."

for index in "${!STEPS[@]}"; do
    step="${STEPS[$index]}"
    echo
    echo "[$((index + 1))/${#STEPS[@]}] Running ${step}"
    "$SCRIPT_DIR/$step" "$DEVICE"
done

echo
echo "Complete EuroPi deployment finished successfully."
