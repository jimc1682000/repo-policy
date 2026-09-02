#!/usr/bin/env python3
"""Enforce the repository's PR-level architecture decision record policy."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Callable

try:
    import yaml
except ImportError:  # pragma: no cover - runtime installs PyYAML
    print("PyYAML is required (pip install pyyaml)", file=sys.stderr)
    raise SystemExit(1)


COMMIT_TYPES = frozenset(
    {"feat", "fix", "docs", "style", "refactor", "perf", "test", "build", "ci", "chore", "revert"}
)
LEVEL_ORDER = {"patch": 1, "minor": 2, "major": 3, "unknown": 4}
CONVENTIONAL_COMMIT_RE = re.compile(
    r"^(?:[A-Z][A-Z0-9]+-[0-9]+\s+)?"
    r"(?P<type>[a-z]+)(?:\([^\r\n)]*\))?(?P<breaking>!)?:"
)
BREAKING_FOOTER_RE = re.compile(r"^BREAKING[ -]CHANGE\s*:", re.IGNORECASE | re.MULTILINE)


@dataclass(frozen=True)
class ChangedFile:
    """A file in the PR diff, using the new path for renames."""

    path: str
    status: str = "modified"


@dataclass(frozen=True)
class ChangeSet:
    """The complete commit and file set for one PR base...head range."""

    commits: tuple[str, ...]
    files: tuple[ChangedFile, ...]


@dataclass(frozen=True)
class AdrGatePolicy:
    """Path policy loaded from central defaults plus a consumer override."""

    architecture_sensitive_paths: tuple[str, ...] = (
        "terraform/**",
        "src/**",
        "lambda_function.py",
        "lambda_weekly_report.py",
        "**/*iam*",
        "**/*alarm*",
    )
    architecture_exclude_paths: tuple[str, ...] = (
        "src/tests/**",
        "tests/**",
        "**/test_*.py",
        "**/*_test.py",
        "docs/**",
        "README.md",
    )
    adr_paths: tuple[str, ...] = ("docs/adr/*.md", "docs/adr/**/*.md")


@dataclass(frozen=True)
class AdrDecision:
    """Observable result of evaluating a complete PR change set."""

    required: bool
    satisfied: bool
    level: str
    reasons: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.required or self.satisfied


def parse_commit_level(message: str) -> str:
    """Map one commit message to its highest required SemVer level."""
    text = message.strip()
    first_line = text.splitlines()[0].strip() if text else ""
    if first_line.startswith(("Merge ", "Revert ", "fixup!", "squash!")):
        return "patch"
    if BREAKING_FOOTER_RE.search(text):
        return "major"

    match = CONVENTIONAL_COMMIT_RE.match(first_line)
    if not match or match.group("type") not in COMMIT_TYPES:
        return "unknown"
    if match.group("breaking"):
        return "major"
    return "minor" if match.group("type") == "feat" else "patch"


def match_path(path: str, pattern: str) -> bool:
    """Match repository paths with a glob whose ``**/`` may be zero directories."""
    if fnmatch(path, pattern):
        return True
    if pattern.startswith("**/") and fnmatch(path, pattern[3:]):
        return True
    if "/**/" in pattern and fnmatch(path, pattern.replace("/**/", "/")):
        return True
    return False


def _matches_any(path: str, patterns: tuple[str, ...]) -> bool:
    return any(match_path(path, pattern) for pattern in patterns)


def _is_architecture_sensitive(path: str, policy: AdrGatePolicy) -> bool:
    return _matches_any(path, policy.architecture_sensitive_paths) and not _matches_any(
        path, policy.architecture_exclude_paths
    )


def _has_adr_change(file: ChangedFile, policy: AdrGatePolicy) -> bool:
    return file.status.lower() != "deleted" and _matches_any(file.path, policy.adr_paths)


def evaluate(change_set: ChangeSet, policy: AdrGatePolicy = AdrGatePolicy()) -> AdrDecision:
    """Evaluate whether a PR range contains a required and present ADR."""
    levels = [parse_commit_level(message) for message in change_set.commits]
    level = max(levels or ["unknown"], key=LEVEL_ORDER.__getitem__)
    sensitive_files = [
        file.path
        for file in change_set.files
        if _is_architecture_sensitive(file.path, policy) and file.status.lower() != "deleted"
    ]
    adr_changed = any(_has_adr_change(file, policy) for file in change_set.files)

    reasons: list[str] = []
    if level == "major":
        reasons.append("major commit or BREAKING CHANGE")
    elif level == "minor":
        reasons.append("feat commit")
    elif level == "unknown":
        reasons.append("unknown commit type; fail closed")
    if sensitive_files:
        reasons.append("architecture-sensitive path: " + ", ".join(sorted(sensitive_files)))

    required = level in {"major", "minor", "unknown"} or bool(sensitive_files)
    satisfied = not required or adr_changed
    return AdrDecision(
        required=required,
        satisfied=satisfied,
        level=level,
        reasons=tuple(reasons),
    )


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        value = yaml.safe_load(handle)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"ADR policy must be a mapping: {path}")
    return value


def _merge_policy_data(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if key in {
            "architecture_sensitive_paths",
            "architecture_exclude_paths",
            "adr_paths",
        } and isinstance(value, list):
            current = merged.get(key) or []
            merged[key] = list(dict.fromkeys([*current, *value]))
        else:
            merged[key] = value
    return merged


def policy_from_dict(data: dict[str, Any]) -> AdrGatePolicy:
    return AdrGatePolicy(
        architecture_sensitive_paths=tuple(data.get("architecture_sensitive_paths") or ()),
        architecture_exclude_paths=tuple(data.get("architecture_exclude_paths") or ()),
        adr_paths=tuple(data.get("adr_paths") or ()),
    )


def load_policy(path: str | Path, override_path: str | Path | None = None) -> AdrGatePolicy:
    data = _load_yaml(Path(path))
    if override_path:
        override = Path(override_path)
        if override.is_file():
            data = _merge_policy_data(data, _load_yaml(override))
    return policy_from_dict(data)


def _run_gh(*args: str) -> str:
    gh = shutil.which("gh")
    if gh is None:
        raise RuntimeError("gh is required but was not found in PATH")
    result = subprocess.run(  # noqa: S603
        [gh, *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"gh exited {result.returncode}")
    return result.stdout


def _gh_json(*args: str) -> Any:
    output = _run_gh(*args)
    return json.loads(output) if output.strip() else None


def _flatten_pages(value: Any) -> list[dict[str, Any]]:
    if not value:
        return []
    if isinstance(value, list) and value and all(isinstance(page, list) for page in value):
        return [item for page in value for item in page]
    if isinstance(value, list):
        return value
    raise ValueError("GitHub API pagination returned an unexpected shape")


def collect_change_set(repo: str, pr_number: int) -> ChangeSet:
    """Read all commits and files for a PR through the GitHub API."""
    commits = _flatten_pages(
        _gh_json("api", f"repos/{repo}/pulls/{pr_number}/commits", "--paginate", "--slurp")
    )
    files = _flatten_pages(
        _gh_json("api", f"repos/{repo}/pulls/{pr_number}/files", "--paginate", "--slurp")
    )
    return ChangeSet(
        commits=tuple(item.get("commit", {}).get("message", "") for item in commits),
        files=tuple(
            ChangedFile(item["filename"], item.get("status", "modified"))
            for item in files
            if item.get("filename")
        ),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=os.getenv("GITHUB_REPOSITORY"))
    parser.add_argument("--pr-number", type=int, default=os.getenv("PR_NUMBER"))
    parser.add_argument("--policy", required=True, type=Path)
    parser.add_argument("--override", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not args.repo or not args.pr_number:
        print("--repo and --pr-number are required", file=sys.stderr)
        return 2
    try:
        policy = load_policy(args.policy, args.override)
        decision = evaluate(collect_change_set(args.repo, args.pr_number), policy)
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(f"ADR gate error: {exc}", file=sys.stderr)
        return 2

    if decision.ok:
        state = "not required" if not decision.required else "present"
        print(f"ADR gate: PASS ({state}; level={decision.level})")
        return 0

    print(
        "ADR gate: FAIL — this PR requires a non-deleted file under "
        + ", ".join(policy.adr_paths),
        file=sys.stderr,
    )
    print(f"  highest commit level: {decision.level}", file=sys.stderr)
    for reason in decision.reasons:
        print(f"  reason: {reason}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
