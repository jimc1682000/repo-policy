#!/usr/bin/env python3
"""Classify PR risk from a shared policy YAML and auto-merge only risk:low."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from copy import deepcopy
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - runtime installs PyYAML
    print("PyYAML is required (pip install pyyaml)", file=sys.stderr)
    raise SystemExit(1)

# List keys: override values are unioned (order-preserving) with the default.
LIST_UNION_KEYS = frozenset(
    {
        "trusted_comment_authors",
        "allowed_merge_actors",
        "high_risk_dependencies",
        "safe_dependency_file_patterns",
        "docs_file_patterns",
        "high_risk_file_patterns",
        "dependabot_structural_file_patterns",
        "manual_only_title_keywords",
        "high_risk_title_keywords",
    }
)

DRY_RUN = os.getenv("DRY_RUN") == "1"
GH_BIN = shutil.which("gh")


@dataclass
class Policy:
    risk_labels: dict[str, dict[str, str]]
    codex_label: dict[str, str]
    markers: dict[str, str]
    trusted_comment_authors: tuple[str, ...]
    allowed_merge_actors: tuple[str, ...]
    high_risk_dependencies: frozenset[str]
    safe_dependency_file_patterns: tuple[str, ...]
    docs_file_patterns: tuple[str, ...]
    high_risk_file_patterns: tuple[str, ...]
    dependabot_structural_file_patterns: tuple[str, ...]
    manual_only_title_keywords: tuple[str, ...]
    high_risk_title_keywords: tuple[str, ...]
    docs_only_max_lines: int
    small_pr_max_files: int
    small_pr_max_lines: int
    automation_workflow_name: str

    @property
    def looks_good_marker(self) -> str:
        return self.markers["looks_good"]

    @property
    def codex_review_prefix(self) -> str:
        return self.markers["codex_review_prefix"]

    @property
    def codex_label_name(self) -> str:
        return self.codex_label["name"]


@dataclass
class Decision:
    risk: str
    automerge: bool
    request_codex_review: bool
    reason: str


def deep_merge_policy(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Merge override into base. List union keys append; maps deep-merge; else replace."""
    result = deepcopy(base)
    for key, value in override.items():
        if key in LIST_UNION_KEYS and isinstance(value, list):
            existing = result.get(key) or []
            if not isinstance(existing, list):
                existing = []
            merged: list[Any] = []
            for item in [*existing, *value]:
                if item not in merged:
                    merged.append(item)
            result[key] = merged
        elif isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge_policy(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def load_yaml_file(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"policy must be a mapping: {path}")
    return data


def policy_from_dict(data: dict[str, Any]) -> Policy:
    thresholds = data.get("thresholds") or {}
    risk_labels = data.get("risk_labels") or {}
    if not risk_labels:
        raise ValueError("policy missing risk_labels")
    markers = data.get("markers") or {}
    if "looks_good" not in markers or "codex_review_prefix" not in markers:
        raise ValueError("policy markers must include looks_good and codex_review_prefix")
    codex = data.get("codex_label") or {}
    if "name" not in codex:
        raise ValueError("policy codex_label.name is required")

    high_risk_deps = {
        str(name).lower() for name in (data.get("high_risk_dependencies") or [])
    }
    return Policy(
        risk_labels={str(k): dict(v) for k, v in risk_labels.items()},
        codex_label={str(k): str(v) for k, v in codex.items()},
        markers={str(k): str(v) for k, v in markers.items()},
        trusted_comment_authors=tuple(data.get("trusted_comment_authors") or ()),
        allowed_merge_actors=tuple(data.get("allowed_merge_actors") or ()),
        high_risk_dependencies=frozenset(high_risk_deps),
        safe_dependency_file_patterns=tuple(data.get("safe_dependency_file_patterns") or ()),
        docs_file_patterns=tuple(data.get("docs_file_patterns") or ()),
        high_risk_file_patterns=tuple(data.get("high_risk_file_patterns") or ()),
        dependabot_structural_file_patterns=tuple(
            data.get("dependabot_structural_file_patterns") or ()
        ),
        manual_only_title_keywords=tuple(
            str(x).lower() for x in (data.get("manual_only_title_keywords") or ())
        ),
        high_risk_title_keywords=tuple(
            str(x).lower() for x in (data.get("high_risk_title_keywords") or ())
        ),
        docs_only_max_lines=int(thresholds.get("docs_only_max_lines", 100)),
        small_pr_max_files=int(thresholds.get("small_pr_max_files", 3)),
        small_pr_max_lines=int(thresholds.get("small_pr_max_lines", 150)),
        automation_workflow_name=str(
            data.get("automation_workflow_name")
            or os.getenv("AUTOMATION_WORKFLOW_NAME")
            or "PR merge automation"
        ),
    )


def load_policy(
    policy_path: str | Path | None = None,
    override_path: str | Path | None = None,
) -> Policy:
    path = Path(policy_path or os.environ["POLICY_PATH"])
    data = load_yaml_file(path)
    override = override_path if override_path is not None else os.getenv("POLICY_OVERRIDE_PATH")
    if override:
        override_p = Path(override)
        if override_p.is_file():
            data = deep_merge_policy(data, load_yaml_file(override_p))
        elif str(override).strip():
            print(f"warning: override path not found: {override_p}", file=sys.stderr)
    # Env authors override / extend trusted authors for marker comments.
    env_authors = os.getenv("AUTOMATION_COMMENT_AUTHORS")
    if env_authors:
        extra = [a.strip() for a in env_authors.split(",") if a.strip()]
        data = deep_merge_policy(data, {"trusted_comment_authors": extra})
    if os.getenv("AUTOMATION_WORKFLOW_NAME"):
        data["automation_workflow_name"] = os.environ["AUTOMATION_WORKFLOW_NAME"]
    return policy_from_dict(data)


def run_gh(*args: str, input_text: str | None = None) -> str:
    if GH_BIN is None:
        print("gh is required but was not found in PATH", file=sys.stderr)
        raise SystemExit(1)
    result = subprocess.run(  # noqa: S603
        [GH_BIN, *args],
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        print(result.stderr.strip(), file=sys.stderr)
        raise SystemExit(result.returncode)
    return result.stdout


def gh_json(*args: str) -> Any:
    output = run_gh(*args)
    return json.loads(output) if output.strip() else None


def pr_numbers(repo: str) -> list[int]:
    event_pr = os.getenv("PR_NUMBER")
    if event_pr:
        return [int(event_pr)]
    prs = gh_json(
        "pr",
        "list",
        "--repo",
        repo,
        "--state",
        "open",
        "--json",
        "number",
    )
    return [int(pr["number"]) for pr in prs]


def load_pr(repo: str, number: int) -> dict[str, Any]:
    return gh_json(
        "pr",
        "view",
        str(number),
        "--repo",
        repo,
        "--json",
        ",".join(
            [
                "number",
                "title",
                "body",
                "author",
                "baseRefName",
                "headRefName",
                "headRefOid",
                "isDraft",
                "labels",
                "additions",
                "deletions",
                "changedFiles",
                "mergeable",
                "files",
                "commits",
                "statusCheckRollup",
                "url",
            ]
        ),
    )


def is_dependabot(pr: dict[str, Any]) -> bool:
    login = (pr.get("author") or {}).get("login") or ""
    return login in {"app/dependabot", "dependabot[bot]", "app/renovate", "renovate[bot]"}


def dependency_names(pr: dict[str, Any]) -> set[str]:
    text = "\n".join(
        [
            pr.get("title") or "",
            pr.get("body") or "",
            "\n".join(commit.get("messageBody") or "" for commit in pr.get("commits", [])),
        ]
    )
    names = set(re.findall(r"dependency-name:\s*([A-Za-z0-9_.@/-]+)", text))
    title_match = re.search(r"\bbump\s+([A-Za-z0-9_.@/-]+)\s+from\b", pr.get("title") or "", re.I)
    if title_match:
        names.add(title_match.group(1))
    title_match = re.search(
        r"\bupdate\s+([A-Za-z0-9_.@/-]+)\s+requirement\b",
        pr.get("title") or "",
        re.I,
    )
    if title_match:
        names.add(title_match.group(1))
    # Renovate-style: "Update dependency foo to v1.2.3"
    title_match = re.search(
        r"\bupdate(?:\s+dependency)?\s+([A-Za-z0-9_.@/-]+)\s+to\b",
        pr.get("title") or "",
        re.I,
    )
    if title_match:
        names.add(title_match.group(1))
    return {name.lower() for name in names}


def dependency_update_text(pr: dict[str, Any]) -> str:
    return "\n".join(
        [
            pr.get("title") or "",
            pr.get("body") or "",
            "\n".join(commit.get("messageBody") or "" for commit in pr.get("commits", [])),
        ]
    )


def parse_version_pairs(text: str) -> list[tuple[tuple[int, ...], tuple[int, ...]]]:
    version = r"[vV]?[><=~^ ]*(\d+(?:\.\d+){0,2})"
    return [
        (
            tuple(int(part) for part in match.group(1).split(".")),
            tuple(int(part) for part in match.group(2).split(".")),
        )
        for match in re.finditer(rf"\bfrom\s+{version}\s+to\s+{version}\b", text, re.I)
    ]


# Dependabot/Renovate declare the semver level themselves, in the commit trailer
# ("update-type: version-update:semver-minor") or the legacy body form
# ("version-update:semver-minor"). That declaration is authoritative; the rendered
# body is not.
SEMVER_LEVEL_RE = re.compile(
    r"(?:update-type:\s*)?version-update:semver-(major|minor|patch)", re.I
)

# Lines the bot writes about *this* PR's dependencies. Anything else in the body —
# release notes, changelog excerpts, commit lists — describes upstream history and
# must not be parsed for version pairs.
DECLARED_UPDATE_LINE_RE = re.compile(
    r"^\s*(?:updates?|bumps?)\s+[`\"']?[A-Za-z0-9_.@/-]+[`\"']?\s+from\s+.*$",
    re.I | re.M,
)


def declared_semver_levels(pr: dict[str, Any]) -> set[str]:
    """Semver levels the bot declared for this PR (authoritative, may be empty)."""
    return {m.group(1).lower() for m in SEMVER_LEVEL_RE.finditer(dependency_update_text(pr))}


def declared_version_pairs(pr: dict[str, Any]) -> list[tuple[tuple[int, ...], tuple[int, ...]]]:
    """Version pairs from the bot's own update lines and the PR title only.

    Scanning the whole body instead lets an upstream changelog entry such as
    "bump eslint-plugin from 71.1.0 to 72.0.0" mark an unrelated patch/minor PR as a
    major update, which permanently blocks grouped dependency PRs from automation.
    """
    text = dependency_update_text(pr)
    scoped = "\n".join(DECLARED_UPDATE_LINE_RE.findall(text))
    pairs = parse_version_pairs(scoped)
    if not pairs:
        pairs = parse_version_pairs(pr.get("title") or "")
    return pairs


def is_major_update(pr: dict[str, Any]) -> bool:
    levels = declared_semver_levels(pr)
    if levels:
        # The bot told us. Trust it and stop reading prose.
        return "major" in levels
    return any(before[0] != after[0] for before, after in declared_version_pairs(pr))


def is_unparseable_grouped_update(pr: dict[str, Any]) -> bool:
    title = (pr.get("title") or "").lower()
    grouped = " group " in title or title.startswith("chore(deps): bump the ")
    if not grouped:
        return False
    # Fail closed: grouped updates without parseable from→to pairs cannot be proven low-risk.
    return not declared_semver_levels(pr) and not declared_version_pairs(pr)


def files(pr: dict[str, Any]) -> list[str]:
    return [item["path"] for item in pr.get("files", [])]


def changed_lines(pr: dict[str, Any]) -> int:
    return int(pr.get("additions") or 0) + int(pr.get("deletions") or 0)


def match_path(path: str, pattern: str) -> bool:
    """fnmatch, plus the usual glob reading of a leading "**/" as *zero* or more dirs.

    Plain fnmatch requires a separator, so "**/package-lock.json" misses the lockfile at
    the repository root — which is exactly where npm and uv put theirs, so every such PR
    fell out of the low-risk allowlist.
    """
    if fnmatch(path, pattern):
        return True
    return pattern.startswith("**/") and fnmatch(path, pattern[3:])


def all_files_match(paths: list[str], patterns: tuple[str, ...]) -> bool:
    return bool(paths) and all(
        any(match_path(path, pattern) for pattern in patterns) for path in paths
    )


def any_file_matches(paths: list[str], patterns: tuple[str, ...]) -> bool:
    return any(any(match_path(path, pattern) for pattern in patterns) for path in paths)


def has_green_checks(pr: dict[str, Any], policy: Policy) -> tuple[bool, str]:
    checks = [
        check
        for check in pr.get("statusCheckRollup", [])
        if check.get("workflowName") != policy.automation_workflow_name
        and check.get("name") != "evaluate"
    ]
    if not checks:
        return False, "no completed checks found"
    failures: list[str] = []
    for check in checks:
        if check["__typename"] == "CheckRun":
            if check.get("status") != "COMPLETED" or check.get("conclusion") != "SUCCESS":
                failures.append(
                    f"{check.get('name')}={check.get('status')}/{check.get('conclusion')}"
                )
        elif check["__typename"] == "StatusContext":
            if check.get("state") != "SUCCESS":
                failures.append(f"{check.get('context')}={check.get('state')}")
        else:
            failures.append(f"{check.get('name', check['__typename'])}=unknown")
    if failures:
        return False, ", ".join(failures)
    return True, "all checks green"


def unresolved_threads(repo: str, number: int) -> int:
    """Count unresolved review threads with full GraphQL pagination (page size 100)."""
    owner, name = repo.split("/", 1)
    query = """
    query($owner: String!, $name: String!, $number: Int!, $cursor: String) {
      repository(owner: $owner, name: $name) {
        pullRequest(number: $number) {
          reviewThreads(first: 100, after: $cursor) {
            nodes { isResolved }
            pageInfo { hasNextPage endCursor }
          }
        }
      }
    }
    """
    total = 0
    cursor: str | None = None
    while True:
        args = [
            "api",
            "graphql",
            "-f",
            f"query={query}",
            "-F",
            f"owner={owner}",
            "-F",
            f"name={name}",
            "-F",
            f"number={number}",
        ]
        if cursor is not None:
            args.extend(["-F", f"cursor={cursor}"])
        data = gh_json(*args)
        threads = data["data"]["repository"]["pullRequest"]["reviewThreads"]
        total += sum(1 for node in threads["nodes"] if not node["isResolved"])
        page_info = threads["pageInfo"]
        if not page_info["hasNextPage"]:
            return total
        cursor = page_info["endCursor"]


def count_unresolved_from_pages(pages: list[dict[str, Any]]) -> int:
    """Test helper: count unresolved threads across pre-fetched GraphQL pages."""
    total = 0
    for page in pages:
        threads = page["data"]["repository"]["pullRequest"]["reviewThreads"]
        total += sum(1 for node in threads["nodes"] if not node["isResolved"])
        if not threads["pageInfo"]["hasNextPage"]:
            break
    return total


def issue_comments(repo: str, number: int) -> list[dict[str, Any]]:
    return gh_json("api", f"repos/{repo}/issues/{number}/comments", "--paginate")


def trusted_comment_authors(policy: Policy) -> set[str]:
    return set(policy.trusted_comment_authors)


def has_exact_marker(comment: dict[str, Any], marker: str) -> bool:
    return f"<!-- {marker} -->" in (comment.get("body") or "")


def find_marker_comment(
    comments: list[dict[str, Any]],
    marker: str,
    trusted_authors: set[str],
) -> dict[str, Any] | None:
    """Return first comment with marker from a trusted author only."""
    for comment in comments:
        author = (comment.get("user") or {}).get("login")
        if author in trusted_authors and has_exact_marker(comment, marker):
            return comment
    return None


def find_marker_comment_live(repo: str, number: int, marker: str, policy: Policy) -> dict[str, Any] | None:
    return find_marker_comment(issue_comments(repo, number), marker, trusted_comment_authors(policy))


def comment(repo: str, number: int, body: str) -> None:
    if DRY_RUN:
        first_line = body.splitlines()[0] if body else ""
        print(f"#{number}: dry-run comment - {first_line}")
        return
    run_gh("pr", "comment", str(number), "--repo", repo, "--body", body)


def ensure_labels(repo: str, policy: Policy) -> None:
    if DRY_RUN:
        print("dry-run label setup")
        return
    for name, meta in policy.risk_labels.items():
        run_gh(
            "label",
            "create",
            name,
            "--repo",
            repo,
            "--color",
            meta.get("color", "CCCCCC"),
            "--description",
            meta.get("description", name),
            "--force",
        )
    run_gh(
        "label",
        "create",
        policy.codex_label_name,
        "--repo",
        repo,
        "--color",
        policy.codex_label.get("color", "5319E7"),
        "--description",
        policy.codex_label.get("description", "Codex review requested"),
        "--force",
    )


def current_label_names(pr: dict[str, Any]) -> set[str]:
    return {label["name"] for label in pr.get("labels", [])}


def add_label(repo: str, number: int, label: str) -> None:
    if DRY_RUN:
        print(f"#{number}: dry-run add label - {label}")
        return
    run_gh("pr", "edit", str(number), "--repo", repo, "--add-label", label)


def remove_label(repo: str, number: int, label: str) -> None:
    if DRY_RUN:
        print(f"#{number}: dry-run remove label - {label}")
        return
    run_gh("pr", "edit", str(number), "--repo", repo, "--remove-label", label)


def sync_labels(repo: str, pr: dict[str, Any], decision: Decision, policy: Policy) -> None:
    number = pr["number"]
    existing = current_label_names(pr)
    for label in policy.risk_labels:
        if label == decision.risk and label not in existing:
            add_label(repo, number, label)
        elif label != decision.risk and label in existing:
            remove_label(repo, number, label)
    if decision.request_codex_review and policy.codex_label_name not in existing:
        add_label(repo, number, policy.codex_label_name)
    elif not decision.request_codex_review and policy.codex_label_name in existing:
        remove_label(repo, number, policy.codex_label_name)


def is_manual_only(pr: dict[str, Any], default_branch: str, policy: Policy) -> bool:
    title = (pr.get("title") or "").lower()
    if pr["baseRefName"] != default_branch:
        return True
    return any(word in title for word in policy.manual_only_title_keywords)


def is_high_risk(pr: dict[str, Any], policy: Policy) -> bool:
    title = (pr.get("title") or "").lower()
    if any(word in title for word in policy.high_risk_title_keywords):
        return True
    return any_file_matches(files(pr), policy.high_risk_file_patterns)


def classify_dependabot(pr: dict[str, Any], pr_files: list[str], policy: Policy) -> Decision:
    names = dependency_names(pr)
    # Fail closed when dependency identity is missing/ambiguous for non-lockfile-only PRs.
    lockfile_only = all_files_match(
        pr_files,
        tuple(
            p
            for p in policy.safe_dependency_file_patterns
            if "lock" in p.lower() or p.endswith(".sum")
        ),
    )
    if not names and not lockfile_only and not declared_version_pairs(pr):
        return Decision(
            "risk:manual-only",
            False,
            True,
            "dependency metadata missing or ambiguous; fail closed",
        )
    if any_file_matches(pr_files, policy.dependabot_structural_file_patterns):
        return Decision(
            "risk:high",
            False,
            True,
            "dependency PR changes structural files",
        )
    if is_unparseable_grouped_update(pr):
        return Decision(
            "risk:high",
            False,
            True,
            "grouped dependency PR without parseable version changes",
        )
    if is_major_update(pr) or names & policy.high_risk_dependencies:
        return Decision("risk:high", False, True, "major or high-risk dependency update")
    if all_files_match(pr_files, policy.safe_dependency_file_patterns):
        return Decision(
            "risk:low",
            True,
            False,
            "low-risk dependency patch/minor lockfile or workflow update",
        )
    return Decision(
        "risk:medium",
        False,
        True,
        "dependency PR changes files outside the low-risk allowlist",
    )


def classify(pr: dict[str, Any], default_branch: str, policy: Policy) -> Decision:
    if pr["isDraft"]:
        return Decision("risk:manual-only", False, False, "draft PR")
    if is_manual_only(pr, default_branch, policy):
        return Decision("risk:manual-only", False, True, "stacked or manual-only PR")

    pr_files = files(pr)
    if not is_dependabot(pr):
        if is_high_risk(pr, policy):
            return Decision(
                "risk:high",
                False,
                True,
                "sensitive path or structural keyword; human review required",
            )
        if (
            all_files_match(pr_files, policy.docs_file_patterns)
            and changed_lines(pr) <= policy.docs_only_max_lines
        ):
            return Decision("risk:low", True, False, "small docs-only PR")
        if len(pr_files) <= policy.small_pr_max_files and changed_lines(pr) <= policy.small_pr_max_lines:
            return Decision("risk:medium", False, True, "small non-dependency PR")
        return Decision("risk:high", False, True, "larger non-dependency PR")

    return classify_dependabot(pr, pr_files, policy)


def request_review_once(repo: str, pr: dict[str, Any], decision: Decision, policy: Policy) -> bool:
    marker = f"{policy.codex_review_prefix}{pr['headRefOid']}"
    if find_marker_comment_live(repo, pr["number"], marker, policy):
        return False
    comment(
        repo,
        pr["number"],
        (
            "@codex review\n\n"
            f"{decision.risk} PR. Automation will not merge this PR. "
            "Please review the diff and leave findings if anything needs changes.\n\n"
            f"<!-- {marker} -->"
        ),
    )
    return True


def actor_allowed_to_merge(policy: Policy) -> tuple[bool, str]:
    actor = os.getenv("GITHUB_ACTOR") or ""
    allowed = set(policy.allowed_merge_actors) | set(policy.trusted_comment_authors)
    # Env-configured automation authors are always allowed merge actors.
    env_authors = os.getenv("AUTOMATION_COMMENT_AUTHORS")
    if env_authors:
        allowed |= {a.strip() for a in env_authors.split(",") if a.strip()}
    if not allowed:
        return False, "no allowed merge actors configured"
    if actor and actor not in allowed:
        return False, f"actor {actor!r} not in allowed merge actors"
    # github.token jobs often set GITHUB_ACTOR to the repo owner or github-actions[bot]
    if not actor:
        return False, "GITHUB_ACTOR unset"
    return True, f"actor {actor} allowed"


def merge(repo: str, pr: dict[str, Any], policy: Policy) -> None:
    number = str(pr["number"])
    ok, reason = actor_allowed_to_merge(policy)
    if not ok:
        print(f"#{number}: skip merge - {reason}")
        return
    if not find_marker_comment_live(repo, pr["number"], policy.looks_good_marker, policy):
        comment(repo, pr["number"], f"Looks Good\n\n<!-- {policy.looks_good_marker} -->")
    if DRY_RUN:
        print(f"#{number}: dry-run merge - {pr['headRefOid']}")
        return
    run_gh(
        "pr",
        "merge",
        number,
        "--repo",
        repo,
        "--squash",
        "--match-head-commit",
        pr["headRefOid"],
    )


def merge_if_clean(repo: str, pr: dict[str, Any], policy: Policy) -> None:
    number = pr["number"]
    green, check_reason = has_green_checks(pr, policy)
    if not green:
        print(f"#{number}: skip - {check_reason}")
        return
    if pr["mergeable"] != "MERGEABLE":
        print(f"#{number}: skip - mergeable is {pr['mergeable']}")
        return
    threads = unresolved_threads(repo, number)
    if threads:
        print(f"#{number}: skip - {threads} unresolved review thread(s)")
        return
    merge(repo, pr, policy)
    print(f"#{number}: merged")


def handle_pr(repo: str, default_branch: str, number: int, policy: Policy) -> None:
    pr = load_pr(repo, number)
    decision = classify(pr, default_branch, policy)
    print(f"#{number}: {decision.risk} - {decision.reason}")
    sync_labels(repo, pr, decision, policy)

    if decision.automerge:
        merge_if_clean(repo, pr, policy)
    elif decision.request_codex_review and request_review_once(repo, pr, decision, policy):
        print(f"#{number}: requested Codex review")


def main() -> None:
    repo = os.environ["GITHUB_REPOSITORY"]
    default_branch = os.getenv("DEFAULT_BRANCH", "master")
    policy = load_policy()
    ensure_labels(repo, policy)
    for number in pr_numbers(repo):
        handle_pr(repo, default_branch, number, policy)


if __name__ == "__main__":
    main()
