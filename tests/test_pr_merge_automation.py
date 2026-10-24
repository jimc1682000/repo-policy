"""Unit tests for PR risk classification and merge guards."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from scripts import pr_merge_automation
from scripts.pr_merge_automation import (
    Decision,
    classify,
    count_unresolved_from_pages,
    deep_merge_policy,
    find_marker_comment,
    has_green_checks,
    load_policy,
    load_yaml_file,
    policy_from_dict,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY_PATH = ROOT / "policies" / "pr-automerge.yml"


@pytest.fixture
def policy():
    data = load_yaml_file(DEFAULT_POLICY_PATH)
    # film-brain-like override: torch + search-sensitive paths
    override = {
        "high_risk_dependencies": ["torch"],
        "high_risk_file_patterns": [
            "backend/db.py",
            "backend/interfaces.py",
            "backend/llm_client.py",
            "backend/models.py",
            "backend/routers/search.py",
            "backend/services/search/*",
            "docs/adr/*",
        ],
        "dependabot_structural_file_patterns": [
            "backend/db.py",
            "backend/interfaces.py",
            "backend/llm_client.py",
            "backend/models.py",
            "backend/routers/search.py",
            "backend/services/search/*",
            "docs/adr/*",
        ],
        "trusted_comment_authors": ["github-actions[bot]", "jimc1682000"],
        "allowed_merge_actors": ["github-actions[bot]", "jimc1682000"],
    }
    return policy_from_dict(deep_merge_policy(data, override))


def _pr(
    *,
    title: str,
    author: str = "dependabot[bot]",
    files: list[str] | None = None,
    body: str = "",
    is_draft: bool = False,
    base: str = "master",
    additions: int = 10,
    deletions: int = 2,
    head_oid: str = "abc123",
    labels: list[str] | None = None,
    commits: list[dict] | None = None,
    checks: list[dict] | None = None,
    mergeable: str = "MERGEABLE",
) -> dict:
    return {
        "number": 1,
        "title": title,
        "body": body,
        "author": {"login": author},
        "baseRefName": base,
        "headRefName": "deps/x",
        "headRefOid": head_oid,
        "isDraft": is_draft,
        "labels": [{"name": n} for n in (labels or [])],
        "additions": additions,
        "deletions": deletions,
        "changedFiles": len(files or []),
        "mergeable": mergeable,
        "files": [{"path": p} for p in (files or [])],
        "commits": commits or [],
        "statusCheckRollup": checks
        or [
            {
                "__typename": "CheckRun",
                "name": "test",
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
                "workflowName": "CI",
            }
        ],
        "url": "https://example.test/pr/1",
    }


class TestClassifyDependabot:
    def test_patch_minor_lockfile_is_low(self, policy):
        pr = _pr(
            title="chore(deps): bump pillow from 12.2.0 to 12.3.0",
            body="Updates pillow\n\ndependency-name: pillow\nversion-update:semver-minor",
            files=["uv.lock", "requirements.txt"],
        )
        d = classify(pr, "master", policy)
        assert d == Decision(
            "risk:low",
            True,
            False,
            "low-risk dependency patch/minor lockfile or workflow update",
        )

    def test_major_grouped_update_is_high(self, policy):
        pr = _pr(
            title="chore(deps): bump the python-deps group with 3 updates",
            body="Bumps the python-deps group\n\nUpdates foo from 1.0.0 to 2.0.0",
            files=["uv.lock", "requirements.txt"],
        )
        d = classify(pr, "master", policy)
        assert d.risk == "risk:high"
        assert d.automerge is False

    def test_unparseable_grouped_update_fail_closed_high(self, policy):
        pr = _pr(
            title="chore(deps): bump the python-deps group with 3 updates",
            body="Grouped update without clear from/to pairs",
            files=["uv.lock"],
        )
        d = classify(pr, "master", policy)
        assert d.risk == "risk:high"
        assert "grouped" in d.reason

    def test_torch_update_is_high(self, policy):
        pr = _pr(
            title="chore(deps): bump torch from 2.12.0 to 2.13.0",
            body="dependency-name: torch\nversion-update:semver-minor",
            files=["uv.lock", "requirements.txt"],
        )
        d = classify(pr, "master", policy)
        assert d.risk == "risk:high"
        assert d.automerge is False
        assert "high-risk" in d.reason

    def test_workflow_minor_bump_is_low(self, policy):
        pr = _pr(
            title="chore(deps): bump actions/checkout from 4.1.0 to 4.2.0",
            body="dependency-name: actions/checkout\nversion-update:semver-minor",
            files=[".github/workflows/ci.yml"],
        )
        d = classify(pr, "master", policy)
        assert d.risk == "risk:low"
        assert d.automerge is True

    def test_actions_major_is_high(self, policy):
        pr = _pr(
            title="chore(deps): bump actions/setup-python from 5 to 6",
            body="dependency-name: actions/setup-python\nversion-update:semver-major",
            files=[".github/workflows/ci.yml"],
        )
        d = classify(pr, "master", policy)
        assert d.risk == "risk:high"

    def test_dependency_outside_allowlist_is_medium(self, policy):
        pr = _pr(
            title="chore(deps): bump pillow from 12.2.0 to 12.3.0",
            body="dependency-name: pillow\nversion-update:semver-minor",
            files=["uv.lock", "backend/main.py"],
        )
        d = classify(pr, "master", policy)
        assert d.risk == "risk:medium"
        assert d.automerge is False

    def test_missing_dependency_metadata_fail_closed(self, policy):
        pr = _pr(
            title="chore: mysterious dependency PR",
            body="",
            files=["backend/main.py", "uv.lock"],
        )
        d = classify(pr, "master", policy)
        assert d.risk == "risk:manual-only"
        assert "ambiguous" in d.reason or "missing" in d.reason


class TestClassifyHuman:
    def test_docs_only_small_is_low(self, policy):
        pr = _pr(
            title="docs: fix typo in README",
            author="jimc1682000",
            files=["README.md", "docs/guide.md"],
            additions=20,
            deletions=5,
        )
        d = classify(pr, "master", policy)
        assert d.risk == "risk:low"
        assert d.automerge is True

    def test_app_refactor_is_high(self, policy):
        pr = _pr(
            title="refactor: restructure search service",
            author="jimc1682000",
            files=["backend/services/search/engine.py", "backend/routers/search.py"],
            additions=200,
            deletions=150,
        )
        d = classify(pr, "master", policy)
        assert d.risk == "risk:high"
        assert d.automerge is False

    def test_small_non_docs_is_medium(self, policy):
        pr = _pr(
            title="fix: tweak log format",
            author="jimc1682000",
            files=["backend/observability.py"],
            additions=10,
            deletions=5,
        )
        d = classify(pr, "master", policy)
        assert d.risk == "risk:medium"

    def test_draft_is_manual_only(self, policy):
        pr = _pr(
            title="chore(deps): bump pillow from 1.0.0 to 1.0.1",
            files=["uv.lock"],
            is_draft=True,
        )
        d = classify(pr, "master", policy)
        assert d.risk == "risk:manual-only"
        assert d.request_codex_review is False

    def test_non_default_base_is_manual_only(self, policy):
        pr = _pr(
            title="chore(deps): bump pillow from 1.0.0 to 1.0.1",
            files=["uv.lock"],
            base="develop",
        )
        d = classify(pr, "master", policy)
        assert d.risk == "risk:manual-only"

    def test_stacked_keyword_is_manual_only(self, policy):
        pr = _pr(
            title="feat: part 2 of stacked PR series",
            author="jimc1682000",
            files=["backend/foo.py"],
        )
        d = classify(pr, "master", policy)
        assert d.risk == "risk:manual-only"

    def test_high_risk_path_override(self, policy):
        pr = _pr(
            title="fix: adjust llm timeout",
            author="jimc1682000",
            files=["backend/llm_client.py"],
            additions=5,
            deletions=2,
        )
        d = classify(pr, "master", policy)
        assert d.risk == "risk:high"


class TestReviewThreadsPagination:
    def test_counts_across_pages_over_100(self):
        def page(unresolved: int, resolved: int, has_next: bool, cursor: str | None):
            nodes = [{"isResolved": False}] * unresolved + [{"isResolved": True}] * resolved
            return {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "reviewThreads": {
                                "nodes": nodes,
                                "pageInfo": {
                                    "hasNextPage": has_next,
                                    "endCursor": cursor,
                                },
                            }
                        }
                    }
                }
            }

        pages = [
            page(80, 20, True, "c1"),
            page(30, 70, True, "c2"),
            page(5, 10, False, None),
        ]
        # 80 + 30 + 5 unresolved
        assert count_unresolved_from_pages(pages) == 115


class TestMarkerTrust:
    def test_spoofed_marker_from_untrusted_author_ignored(self, policy):
        comments = [
            {
                "user": {"login": "evil-user"},
                "body": "Looks Good\n\n<!-- repo-policy-pr-automation:looks-good -->",
            }
        ]
        found = find_marker_comment(
            comments,
            policy.looks_good_marker,
            set(policy.trusted_comment_authors),
        )
        assert found is None

    def test_trusted_author_marker_accepted(self, policy):
        comments = [
            {
                "user": {"login": "github-actions[bot]"},
                "body": "Looks Good\n\n<!-- repo-policy-pr-automation:looks-good -->",
            }
        ]
        found = find_marker_comment(
            comments,
            policy.looks_good_marker,
            set(policy.trusted_comment_authors),
        )
        assert found is not None

    def test_wrong_marker_not_matched(self, policy):
        comments = [
            {
                "user": {"login": "github-actions[bot]"},
                "body": "Looks Good\n\n<!-- other-marker -->",
            }
        ]
        found = find_marker_comment(
            comments,
            policy.looks_good_marker,
            set(policy.trusted_comment_authors),
        )
        assert found is None


class TestChecks:
    def test_excludes_self_workflow(self, policy):
        pr = _pr(
            title="x",
            author="jimc1682000",
            files=["README.md"],
            checks=[
                {
                    "__typename": "CheckRun",
                    "name": "evaluate",
                    "status": "IN_PROGRESS",
                    "conclusion": None,
                    "workflowName": "PR merge automation",
                },
                {
                    "__typename": "CheckRun",
                    "name": "test",
                    "status": "COMPLETED",
                    "conclusion": "SUCCESS",
                    "workflowName": "CI",
                },
            ],
        )
        ok, reason = has_green_checks(pr, policy)
        assert ok is True
        assert reason == "all checks green"


class TestPolicyLoad:
    def test_default_policy_loads(self):
        policy = load_policy(DEFAULT_POLICY_PATH, override_path="")
        assert "risk:low" in policy.risk_labels
        assert policy.docs_only_max_lines == 100

    def test_list_union_merge(self):
        base = {"high_risk_dependencies": ["a"], "thresholds": {"docs_only_max_lines": 100}}
        over = {"high_risk_dependencies": ["b", "a"]}
        merged = deep_merge_policy(base, over)
        assert merged["high_risk_dependencies"] == ["a", "b"]

class TestUpstreamChangelogDoesNotDecideRisk:
    """Regression: a grouped patch/minor PR must not inherit "major" from the changelog
    excerpts Dependabot embeds in the body (jimc1682000.github.io#50)."""

    # Shape copied from a real Dependabot grouped PR: three patch/minor updates, plus a
    # release-notes block whose commit list mentions an unrelated 71.1.0 → 72.0.0 bump.
    GROUPED_BODY = """Bumps the minor-and-patch group with 3 updates.

Updates `astro` from 7.1.3 to 7.1.5
Updates `html-validate` from 11.5.6 to 11.6.0
Updates `markdownlint-cli2` from 0.23.1 to 0.23.2

<details>
<summary>Changelog</summary>
<code>6e3cc93</code> bump eslint-plugin-import from 71.1.0 to 72.0.0
<code>85bb5e7</code> bump semver from 5.2.1 to 5.2.2
</details>
"""

    TRAILER = """updated-dependencies:
- dependency-name: astro
  update-type: version-update:semver-patch
- dependency-name: html-validate
  update-type: version-update:semver-minor
- dependency-name: markdownlint-cli2
  update-type: version-update:semver-patch
"""

    def test_grouped_patch_minor_with_changelog_major_is_low(self, policy):
        pr = _pr(
            title="build(deps): bump the minor-and-patch group with 3 updates",
            body=self.GROUPED_BODY,
            files=["package.json", "package-lock.json"],
            commits=[{"messageBody": self.TRAILER}],
        )
        d = classify(pr, "master", policy)
        assert d.risk == "risk:low"
        assert d.automerge is True

    def test_declared_trailer_beats_body_prose(self, policy):
        """Even without clean `Updates` lines, the trailer alone is enough."""
        pr = _pr(
            title="build(deps): bump the minor-and-patch group with 3 updates",
            body="Bumps the group. See <code>abc</code> bump foo from 1.0.0 to 9.0.0 upstream.",
            files=["package-lock.json"],
            commits=[{"messageBody": self.TRAILER}],
        )
        d = classify(pr, "master", policy)
        assert d.risk == "risk:low"

    def test_grouped_without_trailer_uses_declared_lines_only(self, policy):
        pr = _pr(
            title="build(deps): bump the minor-and-patch group with 3 updates",
            body=self.GROUPED_BODY,
            files=["package.json", "package-lock.json"],
        )
        d = classify(pr, "master", policy)
        assert d.risk == "risk:low"

    def test_declared_major_trailer_is_still_high(self, policy):
        pr = _pr(
            title="build(deps): bump the minor-and-patch group with 3 updates",
            body="Updates `typescript` from 6.0.3 to 7.0.2",
            files=["package.json", "package-lock.json"],
            commits=[
                {
                    "messageBody": (
                        "updated-dependencies:\n- dependency-name: typescript\n"
                        "  update-type: version-update:semver-major\n"
                    )
                }
            ],
        )
        d = classify(pr, "master", policy)
        assert d.risk == "risk:high"
        assert d.automerge is False

    def test_title_only_major_still_detected(self, policy):
        """Single-dependency PRs may carry no body at all — fall back to the title."""
        pr = _pr(
            title="ci: bump actions/download-artifact from 4 to 8",
            body="",
            files=[".github/workflows/deploy.yml"],
        )
        d = classify(pr, "master", policy)
        assert d.risk == "risk:high"

    def test_changelog_major_cannot_promote_single_dep_minor(self, policy):
        pr = _pr(
            title="build(deps): bump astro from 7.1.3 to 7.1.5",
            body="Updates `astro` from 7.1.3 to 7.1.5\n\n<code>x</code> bump y from 3.0.0 to 4.0.0",
            files=["package.json", "package-lock.json"],
        )
        d = classify(pr, "master", policy)
        assert d.risk == "risk:low"


class TestRootLevelLockfilesAreInAllowlist:
    """Regression: "**/package-lock.json" must also cover the repo-root lockfile."""

    def test_root_lockfiles_are_low_risk(self, policy):
        pr = _pr(
            title="build(deps): bump the minor-and-patch group with 2 updates",
            body="Updates `astro` from 7.1.3 to 7.1.5\nUpdates `sharp` from 0.34.1 to 0.34.2",
            files=["package.json", "package-lock.json"],
        )
        d = classify(pr, "master", policy)
        assert d.risk == "risk:low"

    def test_nested_lockfile_still_matches(self, policy):
        pr = _pr(
            title="build(deps): bump astro from 7.1.3 to 7.1.5",
            body="Updates `astro` from 7.1.3 to 7.1.5",
            files=["frontend/package.json", "frontend/package-lock.json"],
        )
        d = classify(pr, "master", policy)
        assert d.risk == "risk:low"

    def test_source_file_outside_allowlist_still_medium(self, policy):
        pr = _pr(
            title="build(deps): bump astro from 7.1.3 to 7.1.5",
            body="Updates `astro` from 7.1.3 to 7.1.5",
            files=["package.json", "src/pages/index.astro"],
        )
        d = classify(pr, "master", policy)
        assert d.risk == "risk:medium"


class TestCheckRollupToken:
    """The check rollup is read with its own token; everything else keeps the PAT.

    A fine-grained PAT has no Checks permission to grant, so on a private repo
    `statusCheckRollup` is unreadable and used to fail the whole `gh pr view`.
    """

    def test_checks_token_defaults_to_none(self, monkeypatch):
        monkeypatch.delenv("GH_CHECKS_TOKEN", raising=False)
        assert pr_merge_automation.checks_token() is None

    def test_blank_checks_token_is_none(self, monkeypatch):
        monkeypatch.setenv("GH_CHECKS_TOKEN", "")
        assert pr_merge_automation.checks_token() is None

    def test_rollup_is_a_separate_call_with_its_own_token(self, monkeypatch):
        monkeypatch.setenv("GH_CHECKS_TOKEN", "job-token")
        calls: list[tuple[tuple[str, ...], str | None]] = []
        rollup = [
            {
                "__typename": "CheckRun",
                "name": "test",
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
                "workflowName": "CI",
            }
        ]

        def fake_gh_json(*args: str, token: str | None = None):
            calls.append((args, token))
            if "statusCheckRollup" in args[-1]:
                return {"headRefOid": "aaa111", "statusCheckRollup": rollup}
            return {"number": 1, "title": "x", "headRefOid": "aaa111"}

        monkeypatch.setattr(pr_merge_automation, "gh_json", fake_gh_json)
        pr = pr_merge_automation.load_pr("owner/repo", 1)

        assert pr["statusCheckRollup"] == rollup
        assert len(calls) == 2
        fields_args, fields_token = calls[0]
        rollup_args, rollup_token = calls[1]
        # The PAT (gh's ambient GH_TOKEN) stays the default for the field read.
        assert fields_token is None
        assert "statusCheckRollup" not in fields_args[-1]
        assert rollup_token == "job-token"
        assert rollup_args[-1] == "headRefOid,statusCheckRollup"

    def test_rollup_falls_back_to_default_token_when_unset(self, monkeypatch):
        """The central poller is cross-repo: no job token to hand over."""
        monkeypatch.delenv("GH_CHECKS_TOKEN", raising=False)
        tokens: list[str | None] = []

        def fake_gh_json(*args: str, token: str | None = None):
            tokens.append(token)
            if "statusCheckRollup" in args[-1]:
                return {"headRefOid": "aaa111", "statusCheckRollup": []}
            return {"number": 1, "headRefOid": "aaa111"}

        monkeypatch.setattr(pr_merge_automation, "gh_json", fake_gh_json)
        pr_merge_automation.load_pr("owner/repo", 1)
        assert tokens == [None, None]

    def test_missing_rollup_field_becomes_empty_list(self, monkeypatch):
        """has_green_checks must see [] (blocks merge), never None (crashes)."""
        monkeypatch.delenv("GH_CHECKS_TOKEN", raising=False)
        monkeypatch.setattr(
            pr_merge_automation,
            "gh_json",
            lambda *args, token=None: {"headRefOid": "aaa111"}
            if "statusCheckRollup" in args[-1]
            else {"headRefOid": "aaa111"},
        )
        assert pr_merge_automation.load_pr("owner/repo", 1)["statusCheckRollup"] == []

    def test_run_gh_injects_the_token_into_the_child_env(self, monkeypatch):
        captured: dict = {}

        class Result:
            returncode = 0
            stdout = "{}"
            stderr = ""

        def fake_run(cmd, **kwargs):
            captured["env"] = kwargs.get("env")
            return Result()

        monkeypatch.setattr(pr_merge_automation.subprocess, "run", fake_run)
        monkeypatch.setattr(pr_merge_automation, "GH_BIN", "/usr/bin/gh")

        pr_merge_automation.run_gh("pr", "view", token="job-token")
        assert captured["env"]["GH_TOKEN"] == "job-token"
        assert captured["env"]["GITHUB_TOKEN"] == "job-token"

        pr_merge_automation.run_gh("pr", "view")
        # None means "inherit": gh keeps using the ambient PAT.
        assert captured["env"] is None

    def test_head_moving_between_the_two_calls_aborts(self, monkeypatch):
        """A force-push mid-read must not attach commit B's checks to commit A."""
        monkeypatch.delenv("GH_CHECKS_TOKEN", raising=False)

        def fake_gh_json(*args: str, token: str | None = None):
            if "statusCheckRollup" in args[-1]:
                return {"headRefOid": "bbb222", "statusCheckRollup": []}
            return {"number": 1, "headRefOid": "aaa111"}

        monkeypatch.setattr(pr_merge_automation, "gh_json", fake_gh_json)
        with pytest.raises(SystemExit):
            pr_merge_automation.load_pr("owner/repo", 1)


class TestTitleKeywords:
    def test_keyword_inside_a_word_does_not_match(self, policy):
        pr = _pr(
            title="docs(skills): follow skill authoring best practices",
            author="jimc1682000",
            files=["skills/a/SKILL.md"],
        )
        assert classify(pr, "master", policy).risk != "risk:manual-only"

    def test_whole_word_keyword_still_matches(self, policy):
        pr = _pr(title="fix(auth): rotate session keys", author="jimc1682000")
        assert classify(pr, "master", policy).risk == "risk:manual-only"


HEAD = "e540571fae0123456789abcdef0123456789abcd"
CODEX = "chatgpt-codex-connector[bot]"


@pytest.fixture
def approval_policy(policy):
    data = deep_merge_policy(
        load_yaml_file(DEFAULT_POLICY_PATH),
        {"approval_actors": ["jimc1682000"], "merge_method": "rebase"},
    )
    return policy_from_dict(data)


def _codex_receipt(sha: str = HEAD[:10], author: str = CODEX) -> dict:
    body = f"Codex Review: Didn't find any major issues.\n\n**Reviewed commit:** `{sha}`"
    return {"user": {"login": author}, "body": body}


def _codex_state(sha: str = HEAD, author: str = CODEX, **overrides) -> dict:
    code_review = {"commit_sha": sha, "has_any_findings": False,
                   "has_unresolved_findings": False, "status": "completed", **overrides}
    state = json.dumps({"code_review": code_review, "security_review": {"commit_sha": None}})
    body = f"<!-- codex-pull-request-review-summary -->\n<!-- codex-review-state:v1 {state} -->\nsummary"
    return {"user": {"login": author}, "body": body}


def _labeled(actor: str, label: str = "automerge:approved") -> dict:
    return {"event": "labeled", "label": {"name": label}, "actor": {"login": actor}}


class TestApprovalPieces:
    def test_lane_disabled_without_actors(self, policy):
        pr = _pr(title="feat: x", author="jimc1682000", labels=["automerge:approved"])
        assert pr_merge_automation.approval_static_blocker(pr, "master", policy)

    def test_gate_files_stay_manual(self, approval_policy):
        pr = _pr(title="ci: x", author="jimc1682000", files=[".github/workflows/ci.yml"])
        blocker = pr_merge_automation.approval_static_blocker(pr, "master", approval_policy)
        assert blocker and "by hand" in blocker

    def test_draft_and_stacked_are_blocked(self, approval_policy):
        draft = _pr(title="feat: x", author="jimc1682000", is_draft=True)
        stacked = _pr(title="feat: x", author="jimc1682000", base="feature")
        static = pr_merge_automation.approval_static_blocker
        assert static(draft, "master", approval_policy) == "draft PR"
        assert static(stacked, "master", approval_policy) == "stacked PR"

    def test_latest_labeled_event_wins(self):
        events = [_labeled("jimc1682000"), _labeled("someone"), _labeled("x", "other")]
        assert pr_merge_automation.approval_actor(events, "automerge:approved") == "someone"

    def test_codex_receipt_must_name_current_head(self):
        reviewed = pr_merge_automation.codex_reviewed_head
        assert reviewed(HEAD, [_codex_receipt()], (CODEX,))
        assert not reviewed(HEAD, [_codex_receipt("aaaaaaaaaa")], (CODEX,))
        assert not reviewed(HEAD, [_codex_receipt(author="mallory")], (CODEX,))

    def test_codex_summary_state_counts_only_when_settled_on_head(self):
        reviewed = pr_merge_automation.codex_reviewed_head
        assert reviewed(HEAD, [_codex_state()], (CODEX,))
        assert not reviewed(HEAD, [_codex_state(sha=HEAD[:10])], (CODEX,))
        assert not reviewed(HEAD, [_codex_state(sha="a" * 40)], (CODEX,))
        assert not reviewed(HEAD, [_codex_state(has_unresolved_findings=True)], (CODEX,))
        assert not reviewed(HEAD, [_codex_state(status="running")], (CODEX,))
        assert not reviewed(HEAD, [_codex_state(author="mallory")], (CODEX,))

    def test_codex_summary_state_fails_closed_on_bad_shape(self):
        reviewed = pr_merge_automation.codex_reviewed_head
        broken = {"user": {"login": CODEX}, "body": "<!-- codex-review-state:v1 {not json} -->"}
        missing = {"user": {"login": CODEX},
                   "body": '<!-- codex-review-state:v1 {"code_review": {"status": "completed"}} -->'}
        assert not reviewed(HEAD, [broken], (CODEX,))
        assert not reviewed(HEAD, [missing], (CODEX,))

    def test_invalid_merge_method_rejected(self):
        data = deep_merge_policy(load_yaml_file(DEFAULT_POLICY_PATH), {"merge_method": "merge"})
        with pytest.raises(ValueError):
            policy_from_dict(data)


class TestApprovedLane:
    """handle_pr end to end with GitHub calls stubbed."""

    def _run(self, monkeypatch, policy, *, labeler="jimc1682000", receipts=None,
             threads=0, files=None):
        pr = _pr(
            title="docs(skills): follow skill authoring best practices",
            author="jimc1682000",
            files=files or [f"dir/f{i}.py" for i in range(10)],
            additions=400,
            head_oid=HEAD,
            labels=["risk:high", "automerge:approved"],
        )
        receipts = [_codex_receipt()] if receipts is None else receipts
        pages = {
            "events": [_labeled(labeler)],
            "comments": receipts,
            "reviews": [],
        }
        merged: list[tuple[str, ...]] = []
        mod = pr_merge_automation
        monkeypatch.setenv("GITHUB_ACTOR", CODEX)
        monkeypatch.setattr(mod, "load_pr", lambda repo, number: pr)
        monkeypatch.setattr(mod, "sync_labels", lambda *a: None)
        monkeypatch.setattr(mod, "request_review_once", lambda *a: False)
        monkeypatch.setattr(mod, "find_marker_comment_live", lambda *a: None)
        monkeypatch.setattr(mod, "comment", lambda *a: None)
        monkeypatch.setattr(mod, "unresolved_threads", lambda repo, number: threads)
        monkeypatch.setattr(
            mod,
            "gh_paginated",
            lambda path: next(v for k, v in pages.items() if f"/{k}?" in path),
        )
        monkeypatch.setattr(mod, "run_gh", lambda *args, **kw: merged.append(args) or "")
        mod.handle_pr("o/r", "master", 1, policy)
        return [args for args in merged if args[:2] == ("pr", "merge")]

    def test_approved_reviewed_clean_pr_merges_with_policy_method(
        self, monkeypatch, approval_policy
    ):
        merges = self._run(monkeypatch, approval_policy)
        assert len(merges) == 1
        assert "--rebase" in merges[0] and HEAD in merges[0]

    def test_open_codex_finding_blocks_merge(self, monkeypatch, approval_policy):
        assert self._run(monkeypatch, approval_policy, threads=1) == []

    def test_no_review_of_current_head_blocks_merge(self, monkeypatch, approval_policy):
        stale = [_codex_receipt("0123456789")]
        assert self._run(monkeypatch, approval_policy, receipts=stale) == []

    def test_label_from_non_actor_blocks_merge(self, monkeypatch, approval_policy):
        assert self._run(monkeypatch, approval_policy, labeler="mallory") == []

    def test_gate_files_block_merge(self, monkeypatch, approval_policy):
        files = [".github/policies/pr-automerge.yml"]
        assert self._run(monkeypatch, approval_policy, files=files) == []
