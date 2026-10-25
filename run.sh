#!/usr/bin/env bash
# Те саме, що run_windows.bat, для macOS/Linux
set -e
cd "$(dirname "$0")"
[ -d .venv ] || python3 -m venv .venv
source .venv/bin/activate
pip install -q -r requirements.txt
[ -f .env ] || cp .env.example .env
python -m collector run
python -m collector backfill
python -m collector run
python -m collector stats
python -m collector export --format jsonl
python -m collector export --format csv
python -m collector dashboard
python -m collector report
