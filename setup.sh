#!/usr/bin/env bash
# ---------------------------------------------------------
# setup.sh — Bootstrap para Jarvis Industrial
# Resuelve el error PEP 668 (externally-managed-environment)
# creando un entorno virtual e instalando las dependencias.
# ---------------------------------------------------------
set -e

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

VENV_DIR="${PROJECT_DIR}/.venv"
PYTHON_BIN="${PYTHON:-python3}"

echo "==> Jarvis Industrial · Setup"
echo "    Proyecto : $PROJECT_DIR"
echo "    Python   : $($PYTHON_BIN --version 2>&1)"
echo "    Venv     : $VENV_DIR"

# 1) Crear venv si no existe
if [ ! -d "$VENV_DIR" ]; then
  echo "==> Creando entorno virtual..."
  $PYTHON_BIN -m venv "$VENV_DIR"
fi

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

# 2) Actualizar pip
echo "==> Actualizando pip..."
python -m pip install --upgrade pip --quiet

# 3) Instalar dependencias
echo "==> Instalando requirements.txt..."
python -m pip install -r requirements.txt

# 4) Comprobación rápida
echo "==> Verificando instalación..."
python - <<'PY'
import importlib, sys
needed = ["fastapi", "uvicorn", "pydantic", "pymodbus", "requests", "httpx"]
faltan = []
for m in needed:
    try:
        importlib.import_module(m if m != "pymodbus" else "pymodbus")
    except Exception as e:
        faltan.append((m, str(e)))
if faltan:
    print("Módulos con problemas:")
    for m, e in faltan:
        print(f"  - {m}: {e}")
    sys.exit(1)
print("OK · Todas las dependencias cargan correctamente.")
PY

cat <<EOF

✅ Entorno listo.

Para arrancar el servidor:
    source "$VENV_DIR/bin/activate"
    python main.py

Y abre http://localhost:8000
EOF
