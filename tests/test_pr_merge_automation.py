"""Unit tests for PR risk classification and merge guards."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

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
