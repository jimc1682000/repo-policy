#!/usr/bin/env bash
# Set AUTOMERGE_TOKEN on repos the PAT is allowed to access.
#
# Fine-grained PATs cannot call GET /user/repos (HTTP 403). Discovery is:
#   1) candidate list = your logged-in `gh` identity (owned/collab repos), or CLI args,
#      or --from-consumers
#   2) filter = probe each repo with AUTOMERGE_TOKEN (Metadata/Contents access)
#
# Usage:
#   export AUTOMERGE_TOKEN='…'   # required; never pass as CLI arg / never echo
#   ./scripts/set_automerge_token.sh --dry-run
#   ./scripts/set_automerge_token.sh
#   ./scripts/set_automerge_token.sh film-brain other-repo
#   ./scripts/set_automerge_token.sh --from-consumers
#
# Auth split:
#   - Candidate list + gh secret set: logged-in `gh` user (admin for secret write)
#   - Access filter: AUTOMERGE_TOKEN
#
# Requires: gh, python3 (only for --from-consumers)

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONSUMERS_FILE="${CONSUMERS_FILE:-$ROOT/consumers.yml}"
SECRET_NAME="${SECRET_NAME:-AUTOMERGE_TOKEN}"
OWNER_FILTER="${OWNER_FILTER:-}" # empty = no filter; e.g. jimc1682000
DRY_RUN=0
FROM_CONSUMERS=0
INCLUDE_FORKS=0
INCLUDE_ARCHIVED=0
SKIP_PROBE=0
REPOS=()

usage() {
  cat <<'EOF'
Set AUTOMERGE_TOKEN secret on repos the PAT can access (token never printed).

  export AUTOMERGE_TOKEN='…'
  ./scripts/set_automerge_token.sh --dry-run
  ./scripts/set_automerge_token.sh
  ./scripts/set_automerge_token.sh film-brain
  ./scripts/set_automerge_token.sh --from-consumers

Discovery (fine-grained safe):
  candidates ← gh login (owner/collab repos) | CLI | --from-consumers
  targets    ← candidates where PAT has push|maintain|admin (curl Bearer probe)

Flags:
  --dry-run            list targets only
  --from-consumers     candidates from consumers.yml (secret_managed)
  --owner LOGIN        only candidates under this owner (default: autodetect gh user)
  --include-forks
  --include-archived
  --skip-probe         do not filter with PAT (write to all candidates; not recommended)
  --consumers FILE
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
    --skip-probe) SKIP_PROBE=1; shift ;;
    --owner)
      OWNER_FILTER="$2"
      shift 2
      ;;
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

if [[ -z "$OWNER_FILTER" ]]; then
  OWNER_FILTER="$(gh api user --jq .login)"
fi

list_candidates_from_gh_login() {
  # Use interactive/user gh auth — fine-grained PAT is NOT used here.
  local owner="$1"
  local include_forks="$2"
  local include_archived="$3"
  # gh repo list is reliable for personal accounts; paginate via --limit high
  gh repo list "$owner" --limit 1000 --json nameWithOwner,isFork,isArchived \
    --jq '.[] | [.nameWithOwner, (.isFork|tostring), (.isArchived|tostring)] | @tsv' \
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

list_candidates_from_consumers() {
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

pat_can_access() {
  # Probe with curl + Bearer ONLY. Do not use `gh api` here: even with
  # GH_TOKEN=…, gh may still use keyring credentials (owner → admin on every
  # owned repo), which falsely marks all public/private owned repos as allowed.
  #
  # Public metadata is world-readable; require push|maintain|admin from the
  # *PAT's* permission object (fine-grained Contents: write on selected repos).
  local full="$1"
  local body code push
  body="$(mktemp)"
  code="$(
    curl -sS -o "$body" -w '%{http_code}' \
      -H "Authorization: Bearer ${AUTOMERGE_TOKEN}" \
      -H "Accept: application/vnd.github+json" \
      -H "X-GitHub-Api-Version: 2022-11-28" \
      "https://api.github.com/repos/${full}"
  )" || code="000"
  if [[ "$code" != "200" ]]; then
    rm -f "$body"
    return 1
  fi
  push="$(
    python3 - "$body" <<'PY'
import json, sys
from pathlib import Path
data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
p = data.get("permissions") or {}
print("true" if (p.get("admin") or p.get("maintain") or p.get("push")) else "false")
PY
  )" || push="false"
  rm -f "$body"
  [[ "$push" == "true" ]]
}

if [[ ${#REPOS[@]} -eq 0 ]]; then
  if [[ "$FROM_CONSUMERS" -eq 1 ]]; then
    if [[ ! -f "$CONSUMERS_FILE" ]]; then
      echo "error: consumers file missing: $CONSUMERS_FILE" >&2
      exit 1
    fi
    echo "candidates: consumers.yml ($CONSUMERS_FILE)"
    mapfile -t REPOS < <(list_candidates_from_consumers)
  else
    echo "candidates: repos under owner '$OWNER_FILTER' via gh login (not the PAT)"
    mapfile -t REPOS < <(list_candidates_from_gh_login "$OWNER_FILTER" "$INCLUDE_FORKS" "$INCLUDE_ARCHIVED")
  fi
else
  echo "candidates: CLI arguments"
fi

if [[ ${#REPOS[@]} -eq 0 ]]; then
  echo "error: no candidate repositories" >&2
  exit 1
fi

# Normalize
CANDIDATES=()
for r in "${REPOS[@]}"; do
  r="${r//$'\r'/}"
  [[ -n "$r" ]] || continue
  if [[ "$r" == */* ]]; then
    CANDIDATES+=("$r")
  else
    CANDIDATES+=("${OWNER_FILTER}/$r")
  fi
done
mapfile -t CANDIDATES < <(printf '%s\n' "${CANDIDATES[@]}" | awk 'NF && !seen[$0]++')

echo "probing PAT access on ${#CANDIDATES[@]} candidate(s)…"

NORMALIZED=()
SKIPPED=()
if [[ "$SKIP_PROBE" -eq 1 ]]; then
  echo "warning: --skip-probe set; not filtering by PAT access" >&2
  NORMALIZED=("${CANDIDATES[@]}")
else
  for r in "${CANDIDATES[@]}"; do
    if pat_can_access "$r"; then
      NORMALIZED+=("$r")
    else
      SKIPPED+=("$r")
    fi
  done
fi

if [[ ${#NORMALIZED[@]} -eq 0 ]]; then
  echo "error: PAT cannot access any candidate repo." >&2
  echo "Edit the fine-grained PAT → Repository access → select the repos you want, then retry." >&2
  if [[ ${#SKIPPED[@]} -gt 0 ]]; then
    echo "probed (no access): ${#SKIPPED[@]} repos (e.g. ${SKIPPED[0]})" >&2
  fi
  exit 1
fi

echo "secret name: $SECRET_NAME"
echo "targets (${#NORMALIZED[@]} PAT-allowed):"
for r in "${NORMALIZED[@]}"; do
  echo "  - $r"
done
if [[ ${#SKIPPED[@]} -gt 0 ]]; then
  echo "skipped (${#SKIPPED[@]} not in PAT allow-list / no access):"
  # show at most 20 to keep output short
  i=0
  for r in "${SKIPPED[@]}"; do
    echo "  - $r"
    i=$((i + 1))
    if [[ "$i" -ge 20 ]]; then
      echo "  … and $((${#SKIPPED[@]} - 20)) more"
      break
    fi
  done
fi

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "dry-run: no secrets written"
  exit 0
fi

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
