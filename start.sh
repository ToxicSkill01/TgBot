#!/usr/bin/env sh
# Запуск бота на Linux/macOS: ./start.sh  (настройка заново: ./start.sh --setup)
set -e
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
    echo "[1/3] Готовлю окружение Python..."
    python3 -m venv .venv
fi
echo "[2/3] Проверяю библиотеки..."
.venv/bin/python -m pip install -q --disable-pip-version-check -r requirements.txt
echo "[3/3] Запускаю бота..."
exec .venv/bin/python bot.py "$@"
