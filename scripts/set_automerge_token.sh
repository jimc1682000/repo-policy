#!/usr/bin/env bash
# Set AUTOMERGE_TOKEN on repos the PAT is allowed to access.
# Targets come from the token itself (fine-grained "Only select repositories"),
# not a hardcoded inventory. consumers.yml is only used with --from-consumers.
#
# Usage:
#   export AUTOMERGE_TOKEN='…'   # required; never pass as CLI arg / never echo
#   ./scripts/set_automerge_token.sh              # default: all repos this PAT can access
#   ./scripts/set_automerge_token.sh --dry-run
#   ./scripts/set_automerge_token.sh film-brain   # explicit subset (must still be PAT-allowed)
#   ./scripts/set_automerge_token.sh --from-consumers   # optional: consumers.yml secret_managed
#
# Auth split:
#   - List targets: GH_TOKEN=AUTOMERGE_TOKEN (what the PAT is allowed to see)
#   - gh secret set: your logged-in `gh` user (needs admin on each repo)
#
# Requires: gh, python3

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONSUMERS_FILE="${CONSUMERS_FILE:-$ROOT/consumers.yml}"
SECRET_NAME="${SECRET_NAME:-AUTOMERGE_TOKEN}"
DRY_RUN=0
FROM_CONSUMERS=0
INCLUDE_FORKS=0
INCLUDE_ARCHIVED=0
REPOS=()

usage() {
  cat <<'EOF'
Set AUTOMERGE_TOKEN secret on repos (token value never printed).

  export AUTOMERGE_TOKEN='…'
  ./scripts/set_automerge_token.sh              # repos this PAT can access
  ./scripts/set_automerge_token.sh --dry-run
  ./scripts/set_automerge_token.sh repo1 repo2  # explicit list
  ./scripts/set_automerge_token.sh --from-consumers

Flags:
  --dry-run           list targets only
  --from-consumers    use consumers.yml (secret_managed) instead of PAT access list
  --include-forks     keep forks when discovering via PAT
  --include-archived  keep archived when discovering via PAT
  --consumers FILE    path to consumers.yml (with --from-consumers)
EOF
  exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help) usage 0 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --from-consumers) FROM_CONSUMERS=1; shift ;;
    --include-forks) INCLUDE_FORKS=1; shift ;;
    --include-archived) INCLUDE_ARCHIVED=1; shift ;;
    --consumers)
      CONSUMERS_FILE="$2"
      shift 2
      ;;
    -*)
      echo "unknown flag: $1" >&2
      usage 1
      ;;
    *)
      REPOS+=("$1")
      shift
      ;;
  esac
done

if [[ -z "${AUTOMERGE_TOKEN:-}" ]]; then
  echo "error: AUTOMERGE_TOKEN is unset." >&2
  echo "Set it from your password manager, then re-run. Do not pass the token as a CLI argument." >&2
  exit 1
fi

list_repos_from_pat() {
  # Use the PAT only for discovery. Do not print token. Paginate full_name list.
  # Fine-grained PATs: GitHub returns only repositories the token may access.
  local include_forks="$1"
  local include_archived="$2"
  GH_TOKEN="$AUTOMERGE_TOKEN" gh api user/repos --paginate \
    -f per_page=100 \
    -f affiliation=owner,collaborator,organization_member \
    --jq '.[] | [.full_name, (.fork|tostring), (.archived|tostring)] | @tsv' \
    | while IFS=$'\t' read -r full fork archived; do
        if [[ "$include_forks" -eq 0 && "$fork" == "true" ]]; then
          continue
        fi
        if [[ "$include_archived" -eq 0 && "$archived" == "true" ]]; then
          continue
        fi
        printf '%s\n' "$full"
      done
}

list_repos_from_consumers() {
  python3 - "$CONSUMERS_FILE" <<'PY'
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("error: PyYAML required for --from-consumers (pip install pyyaml)", file=sys.stderr)
    sys.exit(1)

data = yaml.safe_load(Path(sys.argv[1]).read_text(encoding="utf-8")) or {}
owner = data.get("owner") or "jimc1682000"
for c in data.get("consumers") or []:
    if not isinstance(c, dict):
        continue
    name = c.get("repo")
    if not name or name == "*":
        continue
    if c.get("secret_managed") is True and c.get("adopt") is not False:
        print(f"{owner}/{name}" if "/" not in str(name) else name)
PY
}

if [[ ${#REPOS[@]} -eq 0 ]]; then
  if [[ "$FROM_CONSUMERS" -eq 1 ]]; then
    if [[ ! -f "$CONSUMERS_FILE" ]]; then
      echo "error: consumers file missing: $CONSUMERS_FILE" >&2
      exit 1
    fi
    echo "source: consumers.yml ($CONSUMERS_FILE)"
    mapfile -t REPOS < <(list_repos_from_consumers)
  else
    echo "source: repositories accessible to AUTOMERGE_TOKEN (fine-grained allow-list)"
    mapfile -t REPOS < <(list_repos_from_pat "$INCLUDE_FORKS" "$INCLUDE_ARCHIVED")
  fi
else
  echo "source: CLI arguments"
fi

if [[ ${#REPOS[@]} -eq 0 ]]; then
  echo "error: no target repositories." >&2
  echo "If using a fine-grained PAT, edit the token → Repository access → select repos, then retry." >&2
  exit 1
fi

# Normalize to owner/name
NORMALIZED=()
for r in "${REPOS[@]}"; do
  r="${r//$'\r'/}"
  [[ -n "$r" ]] || continue
  if [[ "$r" == */* ]]; then
    NORMALIZED+=("$r")
  else
    NORMALIZED+=("jimc1682000/$r")
  fi
done

# De-dupe while preserving order
mapfile -t NORMALIZED < <(printf '%s\n' "${NORMALIZED[@]}" | awk 'NF && !seen[$0]++')

echo "secret name: $SECRET_NAME"
echo "targets (${#NORMALIZED[@]}):"
for r in "${NORMALIZED[@]}"; do
  echo "  - $r"
done

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "dry-run: no secrets written"
  exit 0
fi

# secret set uses your interactive gh login (admin), not the PAT
ok=0
fail=0
for r in "${NORMALIZED[@]}"; do
  if printf '%s' "$AUTOMERGE_TOKEN" | gh secret set "$SECRET_NAME" --repo "$r"; then
    echo "ok  $r"
    ok=$((ok + 1))
  else
    echo "FAIL $r" >&2
    fail=$((fail + 1))
  fi
done

echo "done: ok=$ok fail=$fail"
if [[ "$fail" -gt 0 ]]; then
  exit 1
fi
exit 0
