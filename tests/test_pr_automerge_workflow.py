"""Contract tests for the reusable PR automerge workflow."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "pr-automerge.yml"


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


def test_runner_defaults_to_github_hosted_ubuntu() -> None:
    workflow = _workflow()
    runner = workflow[True]["workflow_call"]["inputs"]["runner"]
    assert runner["type"] == "string"
    # Existing wrappers pass no runner and must keep running where they did.
    assert json.loads(runner["default"]) == "ubuntu-latest"
    # An empty string (an unset caller variable) also falls back to hosted Ubuntu.
    assert (
        workflow["jobs"]["evaluate"]["runs-on"]
        == "${{ fromJSON(inputs.runner || '\"ubuntu-latest\"') }}"
    )

