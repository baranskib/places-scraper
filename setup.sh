#!/usr/bin/env bash
# Create .venv, install dependencies, and download Playwright Chromium.
# Run from the repo root:  bash setup.sh   or   chmod +x setup.sh && ./setup.sh

set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

if command -v python3 &>/dev/null; then
  PY=python3
elif command -v python &>/dev/null; then
  PY=python
else
  echo "Error: need python3 or python on PATH." >&2
  exit 1
fi

echo "Using: $($PY --version)"

"$PY" -m venv .venv
# shellcheck source=/dev/null
source .venv/bin/activate

python -m pip install --upgrade pip
pip install -r requirements.txt
playwright install chromium

echo ""
echo "Setup complete."
echo "  Activate:  source .venv/bin/activate"
echo "  Run:       python scraper.py"
echo "  Debug UI:  python scraper.py --debug"
