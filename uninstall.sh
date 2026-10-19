#!/usr/bin/env bash
# Remove pc-watchdog. Run: sudo ./uninstall.sh
set -euo pipefail
[ "$EUID" -eq 0 ] || { echo "Run with sudo"; exit 1; }
exec "$(dirname "$(readlink -f "$0")")/pcw-helper" uninstall
