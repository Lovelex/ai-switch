#!/bin/sh
# Remove o ai-switch. Opções: --dry-run, --purge (apaga também o estado em ~/.local/state/ai-switch).
exec python3 "$(dirname "$0")/installer.py" uninstall "$@"
