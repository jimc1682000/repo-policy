"""Behavioral tests for the PR-level ADR requirement gate."""

from __future__ import annotations

from scripts.check_adr_required import (
    AdrGatePolicy,
    ChangedFile,
    ChangeSet,
    collect_change_set,
    evaluate,
    parse_commit_level,
)


def change_set(*messages: str, files: tuple[ChangedFile, ...]) -> ChangeSet:
    return ChangeSet(commits=messages, files=files)


def test_feat_requires_an_adr() -> None:
    decision = evaluate(
        change_set(
            "feat(monitor): add composite alarm routing",
            files=(ChangedFile("src/alarm_handler.py"),),
        )
    )

    assert decision.required is True
    assert decision.satisfied is False
    assert decision.level == "minor"
    assert decision.ok is False


def test_fix_in_an_ordinary_file_does_not_require_an_adr() -> None:
    decision = evaluate(
        change_set("fix: correct a log message", files=(ChangedFile("README.md"),))
    )

    assert decision.required is False
    assert decision.ok is True


def test_refactor_in_terraform_requires_an_adr() -> None:
    decision = evaluate(
        change_set(
            "refactor(terraform): split alarm module",
            files=(ChangedFile("terraform/alarms.tf"),),
        )
    )

    assert decision.required is True
    assert decision.satisfied is False
    assert decision.ok is False


def test_fix_in_an_architecture_sensitive_file_requires_an_adr() -> None:
    decision = evaluate(
        change_set(
            "fix(alarm): correct SNS action",
            files=(ChangedFile("terraform/alarms.tf"),),
        )
    )

    assert decision.required is True
    assert decision.ok is False


def test_tests_and_docs_only_do_not_require_an_adr() -> None:
    decision = evaluate(
        change_set(
            "test: cover parser edge case",
            "docs: explain the fixture",
            files=(
                ChangedFile("src/tests/test_parser.py"),
                ChangedFile("docs/testing.md"),
            ),
        )
    )

    assert decision.required is False
    assert decision.ok is True


def test_feat_with_only_an_adr_change_is_satisfied() -> None:
    decision = evaluate(
        change_set(
            "feat: document the new deployment boundary",
            files=(ChangedFile("docs/adr/010-deployment-boundary.md", "added"),),
        )
    )

    assert decision.required is True
    assert decision.satisfied is True
    assert decision.ok is True


def test_highest_level_across_all_commits_wins() -> None:
    decision = evaluate(
        change_set(
            "fix: tune a retry",
            "feat: add the retry policy",
            "chore: refresh lockfile",
            files=(ChangedFile("README.md"),),
        )
    )

    assert decision.level == "minor"
    assert decision.required is True


def test_breaking_change_footer_requires_an_adr() -> None:
    message = "fix!: preserve the old command\n\nBREAKING CHANGE: remove the flag"

    assert parse_commit_level(message) == "major"
    decision = evaluate(change_set(message, files=(ChangedFile("README.md"),)))
    assert decision.required is True
    assert decision.ok is False


def test_deleted_adr_does_not_satisfy_the_gate() -> None:
    decision = evaluate(
        change_set(
            "feat: remove an obsolete integration",
            files=(ChangedFile("docs/adr/003-old-integration.md", "deleted"),),
        )
    )

    assert decision.required is True
    assert decision.satisfied is False
    assert decision.ok is False


def test_unknown_commit_is_fail_closed() -> None:
    decision = evaluate(change_set("update things", files=(ChangedFile("README.md"),)))

    assert decision.level == "unknown"
    assert decision.required is True
    assert decision.ok is False


def test_custom_policy_can_add_sensitive_and_adr_paths() -> None:
    policy = AdrGatePolicy(
        architecture_sensitive_paths=("infra/**",),
        architecture_exclude_paths=(),
        adr_paths=("decisions/*.md",),
    )
    decision = evaluate(
        change_set(
            "fix: adjust deployment",
            files=(
                ChangedFile("infra/main.tf"),
                ChangedFile("decisions/001.md", "added"),
            ),
        ),
        policy,
    )

    assert decision.required is True
    assert decision.satisfied is True
    assert decision.ok is True


def test_custom_policy_can_make_a_documentation_path_architecture_sensitive() -> None:
    policy = AdrGatePolicy(
        architecture_sensitive_paths=("docs/ARCHITECTURE.md",),
        architecture_exclude_paths=(),
        adr_paths=("docs/adr/*.md",),
    )

    decision = evaluate(
        change_set(
            "fix: correct the architecture contract",
            files=(ChangedFile("docs/ARCHITECTURE.md"),),
        ),
        policy,
    )

    assert decision.required is True
    assert decision.ok is False


def test_collect_change_set_reads_all_paginated_commits_and_files(monkeypatch) -> None:
    responses = iter(
        [
            [[{"commit": {"message": "feat: add an endpoint"}}]],
            [[{"filename": "src/handler.py", "status": "modified"}]],
        ]
    )
    calls: list[tuple[str, ...]] = []

    def fake_gh_json(*args: str):
        calls.append(args)
        return next(responses)

    monkeypatch.setattr("scripts.check_adr_required._gh_json", fake_gh_json)

    changes = collect_change_set("owner/repo", 42)

    assert changes.commits == ("feat: add an endpoint",)
    assert changes.files == (ChangedFile("src/handler.py", "modified"),)
    assert all("--paginate" in call and "--slurp" in call for call in calls)
