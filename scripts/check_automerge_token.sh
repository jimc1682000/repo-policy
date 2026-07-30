#!/usr/bin/env bash
# Check which repos have AUTOMERGE_TOKEN set (name only; never values).
# Same discovery as set_automerge_token.sh (gh login candidates → PAT probe).
#
# Usage:
#   export AUTOMERGE_TOKEN='…'
#   ./scripts/check_automerge_token.sh
#   ./scripts/check_automerge_token.sh film-brain
#   ./scripts/check_automerge_token.sh --from-consumers

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONSUMERS_FILE="${CONSUMERS_FILE:-$ROOT/consumers.yml}"
SECRET_NAME="${SECRET_NAME:-AUTOMERGE_TOKEN}"
OWNER_FILTER="${OWNER_FILTER:-}"
FROM_CONSUMERS=0
INCLUDE_FORKS=0
INCLUDE_ARCHIVED=0
SKIP_PROBE=0
REPOS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --from-consumers) FROM_CONSUMERS=1; shift ;;
    --include-forks) INCLUDE_FORKS=1; shift ;;
    --include-archived) INCLUDE_ARCHIVED=1; shift ;;
    --skip-probe) SKIP_PROBE=1; shift ;;
    --owner) OWNER_FILTER="$2"; shift 2 ;;
    --consumers) CONSUMERS_FILE="$2"; shift 2 ;;
    -h|--help)
      sed -n '2,12p' "$0" | sed 's/^# \?//'
      exit 0
      ;;
    -*) echo "unknown: $1" >&2; exit 1 ;;
    *) REPOS+=("$1"); shift ;;
  esac
done

if [[ -z "${AUTOMERGE_TOKEN:-}" && "$SKIP_PROBE" -eq 0 && ${#REPOS[@]} -eq 0 ]]; then
  echo "error: AUTOMERGE_TOKEN unset (needed to filter PAT-allowed repos)." >&2
  echo "Or pass repo names / --skip-probe / --from-consumers with explicit checks." >&2
  exit 1
fi

if [[ -z "$OWNER_FILTER" ]]; then
  OWNER_FILTER="$(gh api user --jq .login)"
fi

pat_can_access() {
  local full="$1"
  GH_TOKEN="$AUTOMERGE_TOKEN" gh api "repos/$full" --jq .full_name >/dev/null 2>&1
}

if [[ ${#REPOS[@]} -eq 0 ]]; then
  if [[ "$FROM_CONSUMERS" -eq 1 ]]; then
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
  else
    mapfile -t REPOS < <(
      gh repo list "$OWNER_FILTER" --limit 1000 --json nameWithOwner,isFork,isArchived \
        --jq '.[] | [.nameWithOwner, (.isFork|tostring), (.isArchived|tostring)] | @tsv' \
        | while IFS=$'\t' read -r full fork archived; do
            if [[ "$INCLUDE_FORKS" -eq 0 && "$fork" == "true" ]]; then continue; fi
            if [[ "$INCLUDE_ARCHIVED" -eq 0 && "$archived" == "true" ]]; then continue; fi
            printf '%s\n' "$full"
          done
    )
  fi
fi

TARGETS=()
for r in "${REPOS[@]}"; do
  r="${r//$'\r'/}"
  [[ -n "$r" ]] || continue
  [[ "$r" == */* ]] || r="${OWNER_FILTER}/$r"
  if [[ "$SKIP_PROBE" -eq 1 || -z "${AUTOMERGE_TOKEN:-}" ]]; then
    TARGETS+=("$r")
  elif pat_can_access "$r"; then
    TARGETS+=("$r")
  fi
done

if [[ ${#TARGETS[@]} -eq 0 ]]; then
  echo "error: no repos to check (PAT allow-list empty or no candidates)" >&2
  exit 1
fi

printf '%-40s %s\n' "REPO" "AUTOMERGE_TOKEN"
printf '%-40s %s\n' "----------------------------------------" "---------------"
for r in "${TARGETS[@]}"; do
  if gh secret list --repo "$r" 2>/dev/null | awk '{print $1}' | grep -qx "$SECRET_NAME"; then
    printf '%-40s %s\n' "$r" "present"
  else
    printf '%-40s %s\n' "$r" "MISSING"
  fi
done
