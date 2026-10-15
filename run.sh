#!/usr/bin/env bash
# Установка зависимостей и запуск выгрузки сайта.
# Запуск:   bash run.sh
# Своя папка для результата:   OUT=/root/site-backup bash run.sh
set -euo pipefail
cd "$(dirname "$0")"

SUDO=""
if [ "$(id -u)" -ne 0 ]; then SUDO="sudo"; fi

if ! command -v python3 >/dev/null 2>&1; then
  $SUDO apt-get update -q && $SUDO apt-get install -y -q python3
fi
if ! python3 -c "import ensurepip" >/dev/null 2>&1; then
  $SUDO apt-get update -q && $SUDO apt-get install -y -q python3-venv
fi

if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q requests beautifulsoup4 lxml pillow

OUT="${OUT:-$HOME/dumskaya-export}"
echo "Результат будет в: $OUT"
.venv/bin/python weblium_export.py --out "$OUT" "$@"
