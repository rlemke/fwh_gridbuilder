#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
[ -f "${REPO_ROOT}/scripts/_env.sh" ] && source "${REPO_ROOT}/scripts/_env.sh"
[ -f "${REPO_ROOT}/.venv/bin/activate" ] && source "${REPO_ROOT}/.venv/bin/activate"
exec python3 "${SCRIPT_DIR}/retrieve_osm.py" "$@"
