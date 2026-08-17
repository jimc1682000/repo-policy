"""Unit tests for central poller consumer selection."""

from __future__ import annotations

import subprocess

import pytest

from scripts import poll_consumers
from scripts.poll_consumers import (
    DEFAULT_AUTHORS,
    DEFAULT_OVERRIDE_PATH,
    Consumer,
    ConsumerUnreadable,
    OverrideFetchError,
    fetch_override,
    open_pr_numbers,
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


def test_open_pr_numbers_lists_every_page(monkeypatch):
    def fake_run_gh(*args, check=True):
        assert "--paginate" in args  # 30 筆的預設上限會漏掉舊 PR
        return subprocess.CompletedProcess(args, 0, "12\n7\n99\n", "")

    monkeypatch.setattr(poll_consumers, "run_gh", fake_run_gh)
    assert open_pr_numbers("jimc1682000/my-nb") == [12, 7, 99]


def test_open_pr_numbers_empty_when_no_open_prs(monkeypatch):
    def fake_run_gh(*args, check=True):
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(poll_consumers, "run_gh", fake_run_gh)
    assert open_pr_numbers("jimc1682000/my-nb") == []


def test_unreadable_consumer_raises_instead_of_looking_empty(monkeypatch):
    # 過期 PAT 與「沒有 open PR」長得一樣;靜默跳過會讓那個 repo 永遠不再被評估。
    def fake_run_gh(*args, check=True):
        return subprocess.CompletedProcess(args, 1, "", "gh: Bad credentials (HTTP 401)")

    monkeypatch.setattr(poll_consumers, "run_gh", fake_run_gh)
    with pytest.raises(ConsumerUnreadable):
        open_pr_numbers("jimc1682000/my-nb")


def test_consumer_is_hashable_and_frozen():
    consumer = Consumer("o/r", "main", "p.yml", "a", "w")
    with pytest.raises(Exception):
        consumer.repo = "other"  # type: ignore[misc]
