#!/usr/bin/env bash
# Print (or write) a thin PR-merge wrapper for a consumer repo.
#
# Usage:
#   ./scripts/scaffold_consumer.sh film-brain
#   ./scripts/scaffold_consumer.sh film-brain --default-branch master --write /path/to/clone
#
# Does not push. Does not set secrets.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REPO=""
DEFAULT_BRANCH="main"
POLICY_REF="v1"
AUTHORS="github-actions[bot],jimc1682000"
WRITE_DIR=""
WITH_OVERRIDE=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --default-branch) DEFAULT_BRANCH="$2"; shift 2 ;;
    --policy-ref) POLICY_REF="$2"; shift 2 ;;
    --authors) AUTHORS="$2"; shift 2 ;;
    --write) WRITE_DIR="$2"; shift 2 ;;
    --with-override) WITH_OVERRIDE=1; shift ;;
    -h|--help)
      sed -n '2,12p' "$0" | sed 's/^# \?//'
      exit 0
      ;;
    -*)
      echo "unknown: $1" >&2
      exit 1
      ;;
    *)
      REPO="$1"
      shift
      ;;
  esac
done

if [[ -z "$REPO" ]]; then
  echo "usage: $0 <repo-name> [--default-branch main] [--write DIR]" >&2
  exit 1
fi

WORKFLOW=$(cat <<EOF
name: PR merge automation

on:
  pull_request_target:
    types: [opened, reopened, synchronize, ready_for_review]
    branches: [${DEFAULT_BRANCH}]
  workflow_run:
    workflows: ["CI"]
    types: [completed]
  schedule:
    - cron: "17,47 * * * *"
  workflow_dispatch:
    inputs:
      pr_number:
        description: Optional PR number (empty = all open PRs)
        required: false
        default: ""
      dry_run:
        description: Classify only; do not label/comment/merge
        type: boolean
        required: false
        default: true

permissions:
  contents: write
  pull-requests: write
  issues: write
  checks: read
  actions: read
  statuses: read

jobs:
  evaluate:
    uses: jimc1682000/repo-policy/.github/workflows/pr-automerge.yml@${POLICY_REF}
    with:
      default_branch: ${DEFAULT_BRANCH}
      policy_ref: ${POLICY_REF}
      pr_number: \${{ inputs.pr_number || github.event.pull_request.number || '' }}
      policy_override_path: .github/policies/pr-automerge.yml
      automation_comment_authors: ${AUTHORS}
      automation_workflow_name: PR merge automation
      dry_run: \${{ inputs.dry_run || false }}
    secrets:
      token: \${{ secrets.AUTOMERGE_TOKEN }}
EOF
)

OVERRIDE=$(cat <<'EOF'
# Repo-specific overrides (unioned with jimc1682000/repo-policy defaults).
# Add high_risk_dependencies / high_risk_file_patterns as needed.

trusted_comment_authors:
  - jimc1682000

allowed_merge_actors:
  - jimc1682000
EOF
)

RENOVATE=$(cat <<'EOF'
{
  "$schema": "https://docs.renovatebot.com/renovate-schema.json",
  "extends": ["github>jimc1682000/repo-policy//renovate/default"]
}
EOF
)

if [[ -n "$WRITE_DIR" ]]; then
  mkdir -p "$WRITE_DIR/.github/workflows" "$WRITE_DIR/.github/policies"
  printf '%s\n' "$WORKFLOW" >"$WRITE_DIR/.github/workflows/pr-merge-automation.yml"
  if [[ "$WITH_OVERRIDE" -eq 1 ]] || [[ ! -f "$WRITE_DIR/.github/policies/pr-automerge.yml" ]]; then
    printf '%s\n' "$OVERRIDE" >"$WRITE_DIR/.github/policies/pr-automerge.yml"
  fi
  if [[ ! -f "$WRITE_DIR/renovate.json" ]]; then
    printf '%s\n' "$RENOVATE" >"$WRITE_DIR/renovate.json"
  fi
  echo "wrote wrapper under $WRITE_DIR"
  echo "next: commit, open PR; set AUTOMERGE_TOKEN via scripts/set_automerge_token.sh"
else
  echo "=== .github/workflows/pr-merge-automation.yml ==="
  printf '%s\n' "$WORKFLOW"
  echo
  echo "=== .github/policies/pr-automerge.yml (optional) ==="
  printf '%s\n' "$OVERRIDE"
  echo
  echo "=== renovate.json (optional) ==="
  printf '%s\n' "$RENOVATE"
fi
