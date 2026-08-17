"""Unit tests for central poller consumer selection."""

from __future__ import annotations

import subprocess

import pytest

from scripts import poll_consumers
from scripts.poll_consumers import (
    DEFAULT_AUTHORS,
    DEFAULT_OVERRIDE_PATH,
    Consumer,
    OverrideFetchError,
    fetch_override,
    open_pr_count,
    selected_consumers,
)

INVENTORY = {
    "owner": "jimc1682000",
    "consumers": [
        {"repo": "film-brain", "adopt": True, "default_branch": "master"},
        {"repo": "my-nb", "adopt": True},
        {"repo": "my-social-network-backup", "adopt": False},
        {
            "repo": "dotfiles",
            "adopt": True,
            "automation_comment_authors": "github-actions[bot]",
            "policy_override_path": "",
        },
    ],
}


def test_only_adopted_consumers_are_selected():
    repos = [c.repo for c in selected_consumers(INVENTORY)]
    assert repos == [
        "jimc1682000/film-brain",
        "jimc1682000/my-nb",
        "jimc1682000/dotfiles",
    ]


def test_defaults_are_applied_per_consumer():
    by_repo = {c.repo: c for c in selected_consumers(INVENTORY)}
    my_nb = by_repo["jimc1682000/my-nb"]
    assert my_nb.default_branch == "main"
    assert my_nb.override_path == DEFAULT_OVERRIDE_PATH
    assert my_nb.authors == DEFAULT_AUTHORS
    assert by_repo["jimc1682000/film-brain"].default_branch == "master"


def test_per_consumer_overrides_win():
    dotfiles = next(
        c for c in selected_consumers(INVENTORY) if c.repo == "jimc1682000/dotfiles"
    )
    assert dotfiles.authors == "github-actions[bot]"
    assert dotfiles.override_path == ""


def test_empty_inventory_selects_nothing():
    assert selected_consumers({"owner": "jimc1682000"}) == []


@pytest.mark.parametrize(
    ("stdout", "returncode", "expected"),
    [
        ("3\n", 0, 3),
        ("0\n", 0, 0),
        ("", 0, 0),
        ("", 1, 0),  # unreadable repo must not abort the sweep
    ],
)
def test_open_pr_count(monkeypatch, stdout, returncode, expected):
    def fake_run_gh(*args, check=True):
        return subprocess.CompletedProcess(args, returncode, stdout, "boom")

    monkeypatch.setattr(poll_consumers, "run_gh", fake_run_gh)
    assert open_pr_count("jimc1682000/my-nb") == expected


def _fake_gh(returncode: int, stdout: str = "", stderr: str = ""):
    def fake(*args, check=True):
        return subprocess.CompletedProcess(args, returncode, stdout, stderr)

    return fake


CONSUMER = Consumer(
    "jimc1682000/my-nb", "main", ".github/policies/pr-automerge.yml", "a", "w"
)


def test_missing_override_is_a_confirmed_404(monkeypatch, tmp_path):
    monkeypatch.setattr(
        poll_consumers, "run_gh", _fake_gh(1, stderr="gh: Not Found (HTTP 404)")
    )
    assert fetch_override(CONSUMER, tmp_path) is None


def test_override_fetch_failure_is_not_treated_as_missing(monkeypatch, tmp_path):
    # 401/rate limit must not silently downgrade to the default policy.
    monkeypatch.setattr(
        poll_consumers, "run_gh", _fake_gh(1, stderr="gh: Bad credentials (HTTP 401)")
    )
    with pytest.raises(OverrideFetchError):
        fetch_override(CONSUMER, tmp_path)


def test_override_is_written_when_present(monkeypatch, tmp_path):
    monkeypatch.setattr(
        poll_consumers, "run_gh", _fake_gh(0, stdout="trusted_comment_authors: [x]\n")
    )
    dest = fetch_override(CONSUMER, tmp_path)
    assert dest is not None
    assert dest.read_text(encoding="utf-8") == "trusted_comment_authors: [x]\n"


def test_no_override_path_skips_the_fetch(tmp_path):
    consumer = Consumer("jimc1682000/dotfiles", "main", "", "a", "w")
    assert fetch_override(consumer, tmp_path) is None


def test_consumer_is_hashable_and_frozen():
    consumer = Consumer("o/r", "main", "p.yml", "a", "w")
    with pytest.raises(Exception):
        consumer.repo = "other"  # type: ignore[misc]
