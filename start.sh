#!/usr/bin/env bash
# ============================================================================
# start.sh
# Arranca Jarvis Industrial dentro de WSL. Úsalo desde la propia WSL.
# ============================================================================
set -e
cd "$(dirname "$0")"

# Activar venv si existe
if [ -f "../.venv/bin/activate" ]; then
    # shellcheck disable=SC1091
    source ../.venv/bin/activate
elif [ -f "../../.venv/bin/activate" ]; then
    # shellcheck disable=SC1091
    source ../../.venv/bin/activate
else
    echo "AVISO: no encuentro .venv. Ejecuta setup.sh primero."
fi

exec python main.py
