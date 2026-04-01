#!/usr/bin/env bash
# Local runner — installs deps + runs monitor once.
# Usage: bash run.sh
set -e

echo "→ Installing Python deps..."
pip install -r requirements.txt

echo "→ Installing Playwright Chromium..."
playwright install chromium
playwright install-deps chromium 2>/dev/null || true

echo "→ Running monitor..."
python respondent_monitor.py
