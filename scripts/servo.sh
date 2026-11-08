#!/usr/bin/env bash
set -euo pipefail
bundle_path="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd -- "$bundle_path"
export PYTHONUTF8=1 PYTHONDONTWRITEBYTECODE=1
[[ -x .venv/bin/python ]] || { echo 'Run bash scripts/setup_pi.sh first' >&2; exit 2; }
exec .venv/bin/python -m task4.servo_cli "$@"
