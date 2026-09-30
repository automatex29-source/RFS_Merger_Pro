#!/usr/bin/env bash
cd "$(dirname "$0")"
[ -d .venv ] || python3 -m venv .venv || { echo "Install Python 3.9+ first"; exit 1; }
if ! cmp -s requirements.txt .venv/installed.txt; then
  .venv/bin/python -m pip install --upgrade pip && .venv/bin/python -m pip install -r requirements.txt && cp requirements.txt .venv/installed.txt || exit 1
fi
.venv/bin/python rfs_merger_backend.py
