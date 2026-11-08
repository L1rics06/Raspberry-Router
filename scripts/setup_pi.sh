#!/usr/bin/env bash
set -euo pipefail
bundle_path="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd -- "$bundle_path"
export PYTHONUTF8=1 PYTHONDONTWRITEBYTECODE=1
if [[ "$(uname -s)" != Linux ]]; then
  echo 'This setup script requires Linux' >&2
  exit 2
fi
python3 -c 'import sys; assert sys.version_info >= (3,11), "Python 3.11+ required"'
if [[ ! -x .venv/bin/python ]]; then
  python3 -m venv .venv || {
    echo 'Install python3-venv first: sudo apt install python3-venv python3-pip' >&2
    exit 2
  }
fi
.venv/bin/python -m pip install -r requirements.txt
echo 'Ready: bash scripts/run.sh preflight --profile profiles/hardware.local.toml'
