#!/usr/bin/env bash
# Check which consumers have AUTOMERGE_TOKEN set (name only; never values).
#
# Usage:
#   ./scripts/check_automerge_token.sh
#   ./scripts/check_automerge_token.sh film-brain

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONSUMERS_FILE="${CONSUMERS_FILE:-$ROOT/consumers.yml}"
SECRET_NAME="${SECRET_NAME:-AUTOMERGE_TOKEN}"
REPOS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --consumers) CONSUMERS_FILE="$2"; shift 2 ;;
    -*) echo "unknown: $1" >&2; exit 1 ;;
    *) REPOS+=("$1"); shift ;;
  esac
done

if [[ ${#REPOS[@]} -eq 0 ]]; then
  mapfile -t REPOS < <(
    python3 - "$CONSUMERS_FILE" <<'PY'
import sys
from pathlib import Path
import yaml
data = yaml.safe_load(Path(sys.argv[1]).read_text(encoding="utf-8")) or {}
owner = data.get("owner") or "jimc1682000"
for c in data.get("consumers") or []:
    if not isinstance(c, dict):
        continue
    name = c.get("repo")
    if not name or name == "*":
        continue
    if c.get("adopt") is True:
        print(f"{owner}/{name}")
PY
  )
fi

printf '%-40s %s\n' "REPO" "AUTOMERGE_TOKEN"
printf '%-40s %s\n' "----------------------------------------" "---------------"
for r in "${REPOS[@]}"; do
  [[ "$r" == */* ]] || r="jimc1682000/$r"
  if gh secret list --repo "$r" 2>/dev/null | awk '{print $1}' | grep -qx "$SECRET_NAME"; then
    printf '%-40s %s\n' "$r" "present"
  else
    printf '%-40s %s\n' "$r" "MISSING"
  fi
done
