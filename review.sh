#!/usr/bin/env bash
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"
exec python3 "$PROJECT_DIR/lib/workflow.py" review "$@"
