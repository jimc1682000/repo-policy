"""Contract tests for the reusable ADR gate workflow."""

from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "adr-gate.yml"


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


def test_reusable_workflow_is_read_only() -> None:
    workflow = _workflow()
    assert "workflow_call" in workflow[True]
    assert workflow["permissions"] == {"contents": "read", "pull-requests": "read"}


def test_reusable_workflow_checks_policy_at_a_fixed_ref() -> None:
    workflow = _workflow()
    inputs = workflow[True]["workflow_call"]["inputs"]
    assert inputs["policy_ref"]["default"] == "main"
    assert inputs["policy_repository"]["default"] == "jimc1682000/repo-policy"

    steps = workflow["jobs"]["check"]["steps"]
    checkout = next(step for step in steps if step.get("name") == "Checkout policy repo")
    assert checkout["uses"] == "actions/checkout@v5"
    assert checkout["with"]["ref"] == "${{ inputs.policy_ref }}"
    assert checkout["with"]["repository"] == "${{ inputs.policy_repository }}"


def test_reusable_workflow_does_not_execute_caller_pr_code() -> None:
    workflow = _workflow()
    steps = workflow["jobs"]["check"]["steps"]
    run_commands = "\n".join(step.get("run", "") for step in steps)
    assert "check_adr_required.py" in run_commands
    assert "actions/checkout@v5" in "\n".join(step.get("uses", "") for step in steps)
    assert "github.event.pull_request.head" not in run_commands

