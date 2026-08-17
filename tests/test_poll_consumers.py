"""Unit tests for central poller consumer selection."""

from __future__ import annotations

import subprocess

import pytest

from scripts import poll_consumers
from scripts.poll_consumers import (
    DEFAULT_AUTHORS,
    DEFAULT_OVERRIDE_PATH,
    Consumer,
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


def test_consumer_is_hashable_and_frozen():
    consumer = Consumer("o/r", "main", "p.yml", "a", "w")
    with pytest.raises(Exception):
        consumer.repo = "other"  # type: ignore[misc]
