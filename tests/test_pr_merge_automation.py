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
