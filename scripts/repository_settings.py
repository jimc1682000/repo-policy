#!/usr/bin/env python3
"""Audit and reconcile GitHub repository settings for a personal account."""

from __future__ import annotations

import argparse
import copy
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml


class GitHubError(RuntimeError):
    """Raised when a GitHub API command fails."""


class GitHubFeatureUnavailable(GitHubError):
    """Raised when GitHub explicitly reports a plan-limited feature."""


class GitHubClient:
    def request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> Any:
        command = [
            "gh",
            "api",
            "--method",
            method,
            "-H",
            "Accept: application/vnd.github+json",
            "-H",
            "X-GitHub-Api-Version: 2026-03-10",
            path,
        ]
        if payload is not None:
            command.extend(["--input", "-"])
        result = subprocess.run(
            command,
            input=json.dumps(payload) if payload is not None else None,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            message = result.stderr.strip() or "GitHub API request failed"
            raise GitHubError(f"{method} {path}: {message}")
        if not result.stdout.strip():
            return None
        return json.loads(result.stdout)


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as stream:
        value = yaml.safe_load(stream)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    return value


def validate_configuration(policy: dict[str, Any], inventory: dict[str, Any]) -> None:
    if policy.get("version") != 1:
        raise ValueError("policy version must be 1")
    if not inventory.get("owner"):
        raise ValueError("inventory owner is required")

    profiles = policy.get("profiles", {})
    seen: set[str] = set()
    for item in inventory.get("repositories", []):
        repo = item.get("repo")
        if not repo:
            raise ValueError("every repository entry needs repo")
        if repo in seen:
            raise ValueError(f"duplicate repository entry: {repo}")
        seen.add(repo)
        profile = item.get("profile")
        if profile is not None and profile not in profiles:
            raise ValueError(f"unknown profile for {repo}: {profile}")
        if item.get("manage", False) and profile is None:
            raise ValueError(f"unknown profile for {repo}: {profile}")

    discovery = inventory.get("discovery", {})
    if discovery.get("enabled", False):
        profile = discovery.get("profile")
        if profile not in profiles:
            raise ValueError(f"unknown discovery profile: {profile}")


def discover_repositories(
    client: GitHubClient, inventory: dict[str, Any]
) -> list[dict[str, Any]]:
    """Return every repository owned by the authenticated personal account."""
    owner = inventory["owner"]
    repositories: list[dict[str, Any]] = []
    page = 1
    while True:
        batch = client.request(
            "GET",
            "/user/repos?affiliation=owner&visibility=all&sort=full_name"
            f"&direction=asc&per_page=100&page={page}",
        )
        for repo in batch:
            if (repo.get("owner") or {}).get("login") != owner:
                continue
            repositories.append(
                {
                    "repo": repo["name"],
                    "manage": True,
                    "apply": False,
                    "profile": inventory["discovery"]["profile"],
                    "default_branch": repo.get("default_branch"),
                    "classification": (
                        "archived"
                        if repo.get("archived")
                        else "fork"
                        if repo.get("fork")
                        else "empty"
                        if not repo.get("default_branch")
                        else "audit-only"
                    ),
                }
            )
        if len(batch) < 100:
            return repositories
        page += 1


def resolve_inventory(
    client: GitHubClient, inventory: dict[str, Any]
) -> list[dict[str, Any]]:
    """Merge auto-discovered repositories with explicit inventory overrides."""
    explicit = {
        item["repo"]: copy.deepcopy(item) for item in inventory.get("repositories", [])
    }
    if not inventory.get("discovery", {}).get("enabled", False):
        return list(explicit.values())

    resolved: list[dict[str, Any]] = []
    discovered_names: set[str] = set()
    for item in discover_repositories(client, inventory):
        discovered_names.add(item["repo"])
        item.update(explicit.get(item["repo"], {}))
        resolved.append(item)

    missing = sorted(set(explicit) - discovered_names)
    if missing:
        raise ValueError(
            "explicit repositories not owned by authenticated owner: "
            + ", ".join(missing)
        )
    return resolved


def desired_state(
    policy: dict[str, Any], inventory: dict[str, Any], item: dict[str, Any]
) -> dict[str, Any]:
    baseline = copy.deepcopy(policy["baseline"])
    profile = policy["profiles"][item["profile"]]
    checks = profile.get("required_status_checks", [])
    if checks:
        baseline["ruleset"]["rules"].append(
            {
                "type": "required_status_checks",
                "parameters": {
                    "do_not_enforce_on_create": True,
                    "required_status_checks": [
                        {"context": context} for context in checks
                    ],
                    "strict_required_status_checks_policy": True,
                },
            }
        )

    overrides = item.get("overrides", {})
    baseline["repository_settings"].update(overrides.get("repository_settings", {}))
    baseline["security_and_analysis"].update(overrides.get("security_and_analysis", {}))
    baseline["owner"] = inventory["owner"]
    baseline["repo"] = item["repo"]
    baseline["default_branch"] = item["default_branch"]
    baseline["profile"] = item["profile"]
    baseline["apply_enabled"] = item.get("apply", False)
    baseline["classification"] = item.get("classification", "unspecified")
    return baseline


def validate_apply_scope(items: list[dict[str, Any]]) -> None:
    audit_only = [item["repo"] for item in items if not item.get("apply", False)]
    if audit_only:
        raise ValueError(
            "apply is disabled in inventory for: " + ", ".join(sorted(audit_only))
        )


def select_like(current: Any, desired: Any) -> Any:
    """Project API output onto desired keys so server-added fields are ignored."""
    if isinstance(desired, dict):
        current = current if isinstance(current, dict) else {}
        return {
            key: select_like(current.get(key), value) for key, value in desired.items()
        }
    if isinstance(desired, list):
        current = current if isinstance(current, list) else []
        if desired and all(
            isinstance(value, dict) and "type" in value for value in desired
        ):
            by_type = {
                value.get("type"): value for value in current if isinstance(value, dict)
            }
            return [
                select_like(by_type.get(value["type"], {}), value) for value in desired
            ]
        if desired and all(isinstance(value, dict) for value in desired):
            if all("context" in value for value in desired):
                by_context = {
                    value.get("context"): value
                    for value in current
                    if isinstance(value, dict)
                }
                return [
                    select_like(by_context.get(value["context"], {}), value)
                    for value in desired
                ]
            return [
                select_like(current[index] if index < len(current) else {}, value)
                for index, value in enumerate(desired)
            ]
        return current
    return current


def find_ruleset(
    client: GitHubClient, owner: str, repo: str, name: str
) -> dict[str, Any] | None:
    try:
        rulesets = client.request("GET", f"/repos/{owner}/{repo}/rulesets")
    except GitHubError as error:
        if (
            "Upgrade to GitHub Pro or make this repository public to enable this feature"
            in str(error)
        ):
            raise GitHubFeatureUnavailable(str(error)) from error
        raise
    for ruleset in rulesets:
        if ruleset.get("name") == name and ruleset.get("source_type") == "Repository":
            return client.request(
                "GET", f"/repos/{owner}/{repo}/rulesets/{ruleset['id']}"
            )
    return None


def verify_actor(client: GitHubClient, expected_owner: str) -> None:
    actor = client.request("GET", "/user").get("login")
    if actor != expected_owner:
        raise ValueError(
            f"authenticated actor mismatch: expected={expected_owner} actual={actor}"
        )


def list_check_runs(
    client: GitHubClient, owner: str, repo: str, ref: str
) -> list[dict[str, Any]]:
    check_runs: list[dict[str, Any]] = []
    page = 1
    while True:
        response = client.request(
            "GET",
            f"/repos/{owner}/{repo}/commits/{ref}/check-runs?per_page=100&page={page}",
        )
        batch = response.get("check_runs", [])
        check_runs.extend(batch)
        if len(batch) < 100 or len(check_runs) >= response.get("total_count", 0):
            return check_runs
        page += 1


def audit_repository(client: GitHubClient, desired: dict[str, Any]) -> dict[str, Any]:
    owner = desired["owner"]
    repo = desired["repo"]
    info = client.request("GET", f"/repos/{owner}/{repo}")
    blockers: list[str] = []
    changes: list[dict[str, Any]] = []
    unavailable: list[dict[str, str]] = []

    if info.get("default_branch") != desired["default_branch"]:
        blockers.append(
            "default branch mismatch: "
            f"inventory={desired['default_branch']} live={info.get('default_branch')}"
        )

    for key, expected in desired["repository_settings"].items():
        actual = info.get(key)
        if actual != expected:
            changes.append(
                {"area": "repository", "key": key, "from": actual, "to": expected}
            )

    live_security = info.get("security_and_analysis", {}) or {}
    for key, expected in desired["security_and_analysis"].items():
        if key not in live_security:
            unavailable.append(
                {
                    "area": "security",
                    "key": key,
                    "reason": "not returned by repository API",
                }
            )
            continue
        actual = (live_security.get(key) or {}).get("status")
        if actual != expected:
            changes.append(
                {"area": "security", "key": key, "from": actual, "to": expected}
            )

    expected_ruleset = desired["ruleset"]
    try:
        live_ruleset = find_ruleset(client, owner, repo, expected_ruleset["name"])
    except GitHubFeatureUnavailable:
        live_ruleset = None
        unavailable.append(
            {
                "area": "ruleset",
                "key": expected_ruleset["name"],
                "reason": "GitHub plan limitation",
            }
        )

    ruleset_unavailable = any(item["area"] == "ruleset" for item in unavailable)
    if live_ruleset is None and not ruleset_unavailable:
        changes.append(
            {
                "area": "ruleset",
                "key": expected_ruleset["name"],
                "from": None,
                "to": "create",
            }
        )
    elif (
        live_ruleset is not None
        and select_like(live_ruleset, expected_ruleset) != expected_ruleset
    ):
        changes.append(
            {
                "area": "ruleset",
                "key": expected_ruleset["name"],
                "from": "drift",
                "to": "update",
            }
        )

    required_checks = []
    for rule in expected_ruleset["rules"]:
        if rule["type"] == "required_status_checks":
            required_checks.extend(
                check["context"]
                for check in rule["parameters"]["required_status_checks"]
            )
    if required_checks:
        check_runs = list_check_runs(client, owner, repo, desired["default_branch"])
        observed = {check["name"] for check in check_runs}
        missing = sorted(set(required_checks) - observed)
        if missing:
            blockers.append(
                f"required checks not observed on default branch: {', '.join(missing)}"
            )

    return {
        "repository": f"{owner}/{repo}",
        "profile": desired["profile"],
        "classification": desired["classification"],
        "apply_enabled": desired["apply_enabled"],
        "changes": changes,
        "blockers": blockers,
        "unavailable": unavailable,
        "ruleset_id": live_ruleset.get("id") if live_ruleset else None,
    }


def apply_repository(
    client: GitHubClient, desired: dict[str, Any], audit: dict[str, Any]
) -> None:
    if audit["blockers"]:
        raise ValueError(f"refusing to apply {audit['repository']}: blockers present")

    owner = desired["owner"]
    repo = desired["repo"]
    areas = {change["area"] for change in audit["changes"]}
    if areas & {"repository", "security"}:
        repo_payload = copy.deepcopy(desired["repository_settings"])
        security_keys = {
            change["key"] for change in audit["changes"] if change["area"] == "security"
        }
        if security_keys:
            repo_payload["security_and_analysis"] = {
                key: {"status": desired["security_and_analysis"][key]}
                for key in security_keys
            }
        client.request("PATCH", f"/repos/{owner}/{repo}", repo_payload)

    if "ruleset" in areas:
        ruleset = desired["ruleset"]
        ruleset_id = audit["ruleset_id"]
        if ruleset_id is None:
            client.request("POST", f"/repos/{owner}/{repo}/rulesets", ruleset)
        else:
            client.request(
                "PUT", f"/repos/{owner}/{repo}/rulesets/{ruleset_id}", ruleset
            )


def render_report(results: list[dict[str, Any]], applied: bool = False) -> str:
    lines = [f"Mode: {'apply' if applied else 'report-only'}"]
    for result in results:
        lines.append(f"\nRepository: {result['repository']}")
        lines.append(f"Profile: {result['profile']}")
        lines.append(f"Classification: {result.get('classification', 'unspecified')}")
        lines.append(
            f"Apply: {'enabled' if result.get('apply_enabled', False) else 'disabled'}"
        )
        for blocker in result["blockers"]:
            lines.append(f"BLOCKED {blocker}")
        for unavailable in result.get("unavailable", []):
            lines.append(
                f"UNAVAILABLE {unavailable['area']}.{unavailable['key']}: "
                f"{unavailable['reason']}"
            )
        for change in result["changes"]:
            lines.append(
                f"DRIFT {change['area']}.{change['key']}: "
                f"{change['from']!r} -> {change['to']!r}"
            )
        if (
            not result["blockers"]
            and not result["changes"]
            and not result.get("unavailable", [])
        ):
            lines.append("PASS compliant")
        lines.append(f"Planned changes: {len(result['changes'])}")
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--policy", type=Path, default=Path("policies/repository-settings.yml")
    )
    parser.add_argument("--inventory", type=Path, default=Path("repositories.yml"))
    parser.add_argument("--repo", help="Limit execution to one repository name")
    parser.add_argument(
        "--json", action="store_true", help="Print machine-readable report"
    )
    parser.add_argument(
        "--fail-on-drift", action="store_true", help="Exit non-zero on drift"
    )
    parser.add_argument("--apply", action="store_true", help="Apply approved changes")
    parser.add_argument(
        "--confirm-owner", help="Required owner confirmation for --apply"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, client: GitHubClient | None = None) -> int:
    args = parse_args(argv)
    policy = load_yaml(args.policy)
    inventory = load_yaml(args.inventory)
    validate_configuration(policy, inventory)

    if args.apply and args.confirm_owner != inventory["owner"]:
        print(
            "--apply requires --confirm-owner matching inventory owner", file=sys.stderr
        )
        return 2

    client = client or GitHubClient()
    verify_actor(client, inventory["owner"])
    try:
        resolved = resolve_inventory(client, inventory)
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2

    selected = [
        item
        for item in resolved
        if item.get("manage", False)
        and (args.repo is None or item["repo"] == args.repo)
    ]
    if args.repo and not selected:
        print(f"managed repository not found: {args.repo}", file=sys.stderr)
        return 2
    if args.apply:
        try:
            validate_apply_scope(selected)
        except ValueError as error:
            print(str(error), file=sys.stderr)
            return 2

    desired_by_repo = [desired_state(policy, inventory, item) for item in selected]
    results = [audit_repository(client, desired) for desired in desired_by_repo]

    if args.apply:
        for desired, result in zip(desired_by_repo, results, strict=True):
            apply_repository(client, desired, result)
        results = [audit_repository(client, desired) for desired in desired_by_repo]

    if args.json:
        print(
            json.dumps(
                {"mode": "apply" if args.apply else "report-only", "results": results},
                indent=2,
            )
        )
    else:
        print(render_report(results, applied=args.apply))
    if any(result["blockers"] for result in results):
        return 1
    if args.fail_on_drift and any(result["changes"] for result in results):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
