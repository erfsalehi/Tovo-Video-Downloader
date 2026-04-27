#!/usr/bin/env bash
# Tovo Video Downloader - Linux/macOS launcher.
set -e

cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
    echo "[ERROR] python3 is not installed or not on PATH."
    echo "Please install Python 3.8+ from https://www.python.org/downloads/"
    exit 1
fi

if [ ! -d "venv" ]; then
    echo "[1/3] Creating Python virtual environment..."
    python3 -m venv venv
fi

# shellcheck disable=SC1091
source venv/bin/activate

echo "[2/3] Installing dependencies..."
python -m pip install --upgrade pip >/dev/null
python -m pip install -r requirements.txt

echo "[3/3] Starting application..."
python app.py
