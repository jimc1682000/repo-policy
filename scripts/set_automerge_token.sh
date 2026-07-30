#!/usr/bin/env bash
# Set AUTOMERGE_TOKEN on one or more repos without printing the token.
#
# Usage:
#   export AUTOMERGE_TOKEN='…'   # required; never pass as CLI arg
#   ./scripts/set_automerge_token.sh              # consumers.yml secret_managed
#   ./scripts/set_automerge_token.sh film-brain
#   ./scripts/set_automerge_token.sh --dry-run
#
# Requires: gh, python3, PyYAML (or python3 with yaml stdlib alternative — uses PyYAML if present)

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONSUMERS_FILE="${CONSUMERS_FILE:-$ROOT/consumers.yml}"
SECRET_NAME="${SECRET_NAME:-AUTOMERGE_TOKEN}"
DRY_RUN=0
REPOS=()

usage() {
  sed -n '2,12p' "$0" | sed 's/^# \?//'
  exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help) usage 0 ;;
    --dry-run) DRY_RUN=1; shift ;;
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

if [[ -z "${AUTOMERGE_TOKEN:-}" && "$DRY_RUN" -eq 0 ]]; then
  echo "error: AUTOMERGE_TOKEN is unset." >&2
  echo "Set it from your password manager, then re-run. Do not pass the token as a CLI argument." >&2
  exit 1
fi

if [[ ${#REPOS[@]} -eq 0 ]]; then
  if [[ ! -f "$CONSUMERS_FILE" ]]; then
    echo "error: no repos given and consumers file missing: $CONSUMERS_FILE" >&2
    exit 1
  fi
  mapfile -t REPOS < <(
    python3 - "$CONSUMERS_FILE" <<'PY'
import sys
from pathlib import Path

path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")
try:
    import yaml
except ImportError:
    # Minimal fallback: only lines under consumers with secret_managed: true nearby — require PyYAML.
    print("error: PyYAML required (pip install pyyaml)", file=sys.stderr)
    sys.exit(1)

data = yaml.safe_load(text) or {}
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
  )
fi

if [[ ${#REPOS[@]} -eq 0 ]]; then
  echo "error: no target repositories" >&2
  exit 1
fi

# Normalize to owner/name
NORMALIZED=()
for r in "${REPOS[@]}"; do
  if [[ "$r" == */* ]]; then
    NORMALIZED+=("$r")
  else
    NORMALIZED+=("jimc1682000/$r")
  fi
done

echo "secret name: $SECRET_NAME"
echo "targets (${#NORMALIZED[@]}):"
for r in "${NORMALIZED[@]}"; do
  echo "  - $r"
done

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "dry-run: no secrets written"
  exit 0
fi

# Read token once from env; pipe to gh without embedding in argv of ps listings more than needed
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
exit "$fail"
