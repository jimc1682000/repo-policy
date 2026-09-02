"""Behavioral tests for the shared consumer scaffold command."""

from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "scaffold_consumer.sh"


def test_scaffold_writes_pinned_automerge_and_adr_wrappers(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            "bash",
            str(SCRIPT),
            "demo",
            "--default-branch",
            "main",
            "--policy-ref",
            "v1.2",
            "--write",
            str(tmp_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "wrote wrapper" in result.stdout

    automerge = (tmp_path / ".github/workflows/pr-merge-automation.yml").read_text()
    assert "pr-automerge.yml@v1.2" in automerge
    assert "policy_ref: v1.2" in automerge

    adr = yaml.safe_load((tmp_path / ".github/workflows/adr-gate.yml").read_text())
    assert adr[True]["pull_request"]["branches"] == ["main"]
    assert adr["permissions"] == {"contents": "read", "pull-requests": "read"}
    assert adr["jobs"]["adr"]["uses"] == (
        "jimc1682000/repo-policy/.github/workflows/adr-gate.yml@v1.2"
    )
    assert adr["jobs"]["adr"]["with"]["policy_ref"] == "v1.2"
    assert "pull_request_target" not in adr[True]


def test_scaffold_does_not_overwrite_existing_adr_override(tmp_path: Path) -> None:
    override = tmp_path / ".github/policies/adr-gate.yml"
    override.parent.mkdir(parents=True)
    override.write_text("architecture_sensitive_paths:\n  - custom/**\n")

    result = subprocess.run(
        ["bash", str(SCRIPT), "demo", "--write", str(tmp_path)],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "wrote wrapper" in result.stdout
    workflow = (tmp_path / ".github/workflows/adr-gate.yml").read_text()
    assert "adr-gate.yml@v1.2" in workflow
    assert override.read_text() == "architecture_sensitive_paths:\n  - custom/**\n"
