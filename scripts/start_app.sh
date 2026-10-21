#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ ! -f .env ]]; then echo 'Missing .env; copy .env.example first' >&2; exit 1; fi
set -a
# shellcheck disable=SC1091
source .env
set +a
exec .venv/bin/uvicorn app:app --host 127.0.0.1 --port 9088
