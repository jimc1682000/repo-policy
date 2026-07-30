#!/usr/bin/env bash
# Check which repos have AUTOMERGE_TOKEN set (name only; never values).
#
# Default targets: repositories the AUTOMERGE_TOKEN PAT can access
# (same discovery as set_automerge_token.sh). Fallback: CLI args or --from-consumers.
#
# Usage:
#   export AUTOMERGE_TOKEN='…'    # for PAT-based discovery
#   ./scripts/check_automerge_token.sh
#   ./scripts/check_automerge_token.sh film-brain
#   ./scripts/check_automerge_token.sh --from-consumers

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONSUMERS_FILE="${CONSUMERS_FILE:-$ROOT/consumers.yml}"
SECRET_NAME="${SECRET_NAME:-AUTOMERGE_TOKEN}"
FROM_CONSUMERS=0
INCLUDE_FORKS=0
INCLUDE_ARCHIVED=0
REPOS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --from-consumers) FROM_CONSUMERS=1; shift ;;
    --include-forks) INCLUDE_FORKS=1; shift ;;
    --include-archived) INCLUDE_ARCHIVED=1; shift ;;
    --consumers) CONSUMERS_FILE="$2"; shift 2 ;;
    -h|--help)
      sed -n '2,14p' "$0" | sed 's/^# \?//'
      exit 0
      ;;
    -*) echo "unknown: $1" >&2; exit 1 ;;
    *) REPOS+=("$1"); shift ;;
  esac
done

list_repos_from_pat() {
  if [[ -z "${AUTOMERGE_TOKEN:-}" ]]; then
    echo "error: AUTOMERGE_TOKEN unset (needed to list PAT-allowed repos)." >&2
    echo "Or pass repo names, or use --from-consumers." >&2
    exit 1
  fi
  GH_TOKEN="$AUTOMERGE_TOKEN" gh api user/repos --paginate \
    -f per_page=100 \
    -f affiliation=owner,collaborator,organization_member \
    --jq '.[] | [.full_name, (.fork|tostring), (.archived|tostring)] | @tsv' \
    | while IFS=$'\t' read -r full fork archived; do
        if [[ "$INCLUDE_FORKS" -eq 0 && "$fork" == "true" ]]; then
          continue
        fi
        if [[ "$INCLUDE_ARCHIVED" -eq 0 && "$archived" == "true" ]]; then
          continue
        fi
        printf '%s\n' "$full"
      done
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
    mapfile -t REPOS < <(list_repos_from_pat)
  fi
fi

printf '%-40s %s\n' "REPO" "AUTOMERGE_TOKEN"
printf '%-40s %s\n' "----------------------------------------" "---------------"
for r in "${REPOS[@]}"; do
  r="${r//$'\r'/}"
  [[ -n "$r" ]] || continue
  [[ "$r" == */* ]] || r="jimc1682000/$r"
  if gh secret list --repo "$r" 2>/dev/null | awk '{print $1}' | grep -qx "$SECRET_NAME"; then
    printf '%-40s %s\n' "$r" "present"
  else
    printf '%-40s %s\n' "$r" "MISSING"
  fi
done
