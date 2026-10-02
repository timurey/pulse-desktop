#!/usr/bin/env bash
# Pulse Scan (новое окно, Qt + VTK) — macOS / Linux.
# Окружение .venv312 общее со старым окном (run_gui.*); недостающее ставится при первом запуске.
set -e
cd "$(dirname "$0")"
VENV=.venv312
if [ ! -x "$VENV/bin/python" ]; then
  PY=""
  for c in python3.12 python3.11 python3.10 /opt/homebrew/bin/python3.12 /usr/local/bin/python3.12; do
    if command -v "$c" >/dev/null 2>&1; then PY="$c"; break; fi
  done
  if [ -z "$PY" ]; then
    echo "Нужен Python 3.10–3.12 (для Open3D 0.19). macOS: brew install python@3.12; Linux: пакет python3.12."
    read -r -p "Enter — закрыть" _ || true
    exit 1
  fi
  echo "Создаю окружение ($PY) — один раз, несколько минут..."
  "$PY" -m venv "$VENV"
  "$VENV/bin/python" -m pip install --upgrade pip
fi
if ! "$VENV/bin/python" -c "import PySide6, vtk, qtawesome, open3d" >/dev/null 2>&1; then
  echo "Ставлю зависимости окна (PySide6, VTK) — один раз..."
  "$VENV/bin/python" -m pip install -r requirements-qt.txt
fi
exec "$VENV/bin/python" scan_qt.py "$@"
