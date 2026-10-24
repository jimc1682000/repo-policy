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
    WrapperDrift,
    check_wrapper_pin,
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


WRAPPER = """jobs:
  evaluate:
    uses: jimc1682000/repo-policy/.github/workflows/pr-automerge.yml@{uses}
    with:
      default_branch: main
      policy_ref: {ref}
"""


def _wrapper_gh(monkeypatch, body, returncode=0, stderr=""):
    calls = []

    def fake_run_gh(*args, check=True):
        calls.append(args)
        return subprocess.CompletedProcess(args, returncode, body, stderr)

    monkeypatch.setattr(poll_consumers, "run_gh", fake_run_gh)
    return calls


def test_wrapper_pinned_to_poller_ref_passes(monkeypatch):
    consumer = Consumer("o/r", "master", "p.yml", "a", "w")
    calls = _wrapper_gh(monkeypatch, WRAPPER.format(uses="v1.5", ref="v1.5"))
    check_wrapper_pin(consumer, "v1.5")
    assert calls[0][-1] == (
        "repos/o/r/contents/.github/workflows/pr-merge-automation.yml?ref=master"
    )


@pytest.mark.parametrize(
    ("uses", "ref"),
    [("v1.4", "v1.4"), ("v1.5", "v1.4"), ("v1.4", "v1.5"), ("v1.5/hotfix", "v1.5/hotfix")],
)
def test_wrapper_drift_raises(monkeypatch, uses, ref):
    # uses 與 policy_ref 是分開傳的,任一個沒跟上都會讓兩版 policy 同時分類同一個 PR
    _wrapper_gh(monkeypatch, WRAPPER.format(uses=uses, ref=ref))
    with pytest.raises(WrapperDrift, match="poller expects v1.5"):
        check_wrapper_pin(Consumer("o/r", "main", "p.yml", "a", "w"), "v1.5")


@pytest.mark.parametrize("quote", ["", '"', "'"])
def test_slash_ref_compared_whole(monkeypatch, quote):
    _wrapper_gh(
        monkeypatch,
        WRAPPER.format(uses="v1.5/hotfix", ref=f"{quote}v1.5/hotfix{quote}  # pinned"),
    )
    check_wrapper_pin(Consumer("o/r", "main", "p.yml", "a", "w"), "v1.5/hotfix")


def test_commented_out_old_pin_is_ignored(monkeypatch):
    body = WRAPPER.format(uses="v1.5", ref="v1.5").replace(
        "    uses:", "    # uses: jimc1682000/repo-policy/.github/workflows/pr-automerge.yml@v1.4\n    uses:"
    ) + "      # policy_ref: v1.4\n"
    _wrapper_gh(monkeypatch, body)
    check_wrapper_pin(Consumer("o/r", "main", "p.yml", "a", "w"), "v1.5")


def test_unparseable_wrapper_counts_as_drift(monkeypatch):
    _wrapper_gh(monkeypatch, "jobs: [unclosed")
    with pytest.raises(WrapperDrift, match="cannot parse"):
        check_wrapper_pin(Consumer("o/r", "main", "p.yml", "a", "w"), "v1.5")


def test_missing_wrapper_counts_as_drift(monkeypatch):
    _wrapper_gh(monkeypatch, "", returncode=1, stderr="gh: Not Found (HTTP 404)")
    with pytest.raises(WrapperDrift, match="cannot read"):
        check_wrapper_pin(Consumer("o/r", "main", "p.yml", "a", "w"), "v1.5")


def test_wrapper_path_override_is_read(monkeypatch):
    data = {
        "owner": "o",
        "consumers": [{"repo": "r", "adopt": True, "wrapper_path": "x/w.yml"}],
    }
    assert selected_consumers(data)[0].wrapper_path == "x/w.yml"


def test_main_skips_drifted_repo_and_fails_run(monkeypatch, tmp_path):
    inventory = tmp_path / "consumers.yml"
    inventory.write_text(
        "owner: o\npolicy_ref: v1.5\nconsumers:\n"
        "  - {repo: old, adopt: true}\n  - {repo: new, adopt: true}\n"
    )

    def fake_check(consumer, ref):
        if consumer.repo == "o/old":
            raise WrapperDrift("o/old: wrapper pins v1.4")

    listed = []
    monkeypatch.setattr(poll_consumers, "check_wrapper_pin", fake_check)
    monkeypatch.setattr(
        poll_consumers, "open_pr_numbers", lambda repo: listed.append(repo) or []
    )
    code = poll_consumers.main(
        ["--consumers", str(inventory), "--work-dir", str(tmp_path / "w")]
    )
    assert code == 1
    assert listed == ["o/new"]  # 漂移的 repo 不拿錯的 policy 評估
