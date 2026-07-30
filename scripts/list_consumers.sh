#!/usr/bin/env bash
# List consumer inventory and adoption status from consumers.yml + GitHub.
#
# Usage: ./scripts/list_consumers.sh

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONSUMERS_FILE="${CONSUMERS_FILE:-$ROOT/consumers.yml}"

python3 - "$CONSUMERS_FILE" <<'PY'
import subprocess
import sys
from pathlib import Path

import yaml

data = yaml.safe_load(Path(sys.argv[1]).read_text(encoding="utf-8")) or {}
owner = data.get("owner") or "jimc1682000"
rows = []
for c in data.get("consumers") or []:
    if not isinstance(c, dict):
        continue
    name = c.get("repo")
    if not name or name == "*":
        continue
    full = f"{owner}/{name}"
    adopt = bool(c.get("adopt"))
    secret = bool(c.get("secret_managed"))
    branch = c.get("default_branch") or "?"
    notes = (c.get("notes") or "")[:60]
    # workflow present?
    wf = "n/a"
    try:
        r = subprocess.run(
            [
                "gh",
                "api",
                f"repos/{full}/contents/.github/workflows/pr-merge-automation.yml",
                "--jq",
                ".name",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        wf = "yes" if r.returncode == 0 else "no"
    except FileNotFoundError:
        wf = "?"
    rows.append((name, adopt, secret, branch, wf, notes))

print(f"{'repo':<32} {'adopt':<6} {'secret':<7} {'branch':<8} {'wrapper':<8} notes")
print("-" * 100)
for name, adopt, secret, branch, wf, notes in rows:
    print(
        f"{name:<32} {str(adopt):<6} {str(secret):<7} {branch:<8} {wf:<8} {notes}"
    )
PY
