#!/usr/bin/env bash
# .env is a trusted, local shell configuration. Do not source untrusted files.
set -euo pipefail
umask 077
PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -f "$PROJECT_DIR/.env" ]]; then
  set -a
  source "$PROJECT_DIR/.env"
  set +a
fi
command -v python3 >/dev/null || { echo 'Python 3.10 or newer is required.' >&2; exit 1; }
