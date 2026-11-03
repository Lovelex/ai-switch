#!/bin/sh
# Instala o ai-switch a partir desta pasta. Opção: --dry-run (mostra sem gravar).
exec python3 "$(dirname "$0")/installer.py" install "$@"
