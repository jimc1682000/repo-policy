"""Central poller: evaluate PR merge policy for every adopt:true consumer.

Consumer repos only fire pr_merge_automation.py on PR events.  The "wait 30
minutes, then merge if nothing is unhandled" rule has no event to hang off, so
someone has to re-evaluate open PRs on a timer.  Running that timer inside each
consumer costs one billed minute per poll per private repo; running it here does
not, because this repository is public.

The evaluation itself is unchanged: this script sets the same environment the
reusable workflow sets and shells out to pr_merge_automation.py once per repo.

Discovery deliberately asks each repo for its open PRs instead of using the
search API: fine-grained PATs do not reliably see private repos in search, and
an empty search result is indistinguishable from "nothing to do".
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CONSUMERS_PATH = ROOT / "consumers.yml"
DEFAULT_OVERRIDE_PATH = ".github/policies/pr-automerge.yml"
DEFAULT_AUTHORS = "github-actions[bot],jimc1682000"
DEFAULT_WORKFLOW_NAME = "PR merge automation"
GH_BIN = shutil.which("gh")


class OverrideFetchError(RuntimeError):
    """The override YAML could not be read, and it is not a confirmed 404."""


class ConsumerUnreadable(RuntimeError):
    """The consumer's open PRs could not be listed at all."""


@dataclass(frozen=True)
class Consumer:
    repo: str  # owner/name
    default_branch: str
    override_path: str
    authors: str
    workflow_name: str


def load_consumers(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def selected_consumers(data: dict) -> list[Consumer]:
    """adopt:true entries only, in inventory order."""
    owner = data["owner"]
    out: list[Consumer] = []
    for entry in data.get("consumers") or []:
        if not entry.get("adopt"):
            continue
        out.append(
            Consumer(
                repo=f"{owner}/{entry['repo']}",
                default_branch=entry.get("default_branch") or "main",
                override_path=entry.get("policy_override_path", DEFAULT_OVERRIDE_PATH),
                authors=entry.get("automation_comment_authors", DEFAULT_AUTHORS),
                workflow_name=entry.get("automation_workflow_name", DEFAULT_WORKFLOW_NAME),
            )
        )
    return out


def run_gh(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    if GH_BIN is None:
        print("gh is required but was not found in PATH", file=sys.stderr)
        raise SystemExit(1)
    result = subprocess.run(  # noqa: S603
        [GH_BIN, *args],
        text=True,
        capture_output=True,
        check=False,
    )
    if check and result.returncode != 0:
        print(result.stderr.strip(), file=sys.stderr)
        raise SystemExit(result.returncode)
    return result


def open_pr_numbers(repo: str) -> list[int]:
    """Every open PR number, paginated.

    Raises instead of returning an empty list when the repo cannot be read: an
    expired PAT or a revoked grant looks exactly like "no open PRs", and this
    poller is now the only timer, so a silently skipped repo would stop being
    re-evaluated forever with a green run to show for it.

    Paginating here (rather than letting the pinned script call `gh pr list`,
    which defaults to 30) is also what keeps repos with many open PRs swept.
    """
    result = run_gh(
        "api",
        "--paginate",
        f"repos/{repo}/pulls?state=open&per_page=100",
        "--jq",
        ".[].number",
        check=False,
    )
    if result.returncode != 0:
        raise ConsumerUnreadable(
            f"{repo}: cannot list PRs — "
            f"{result.stderr.strip() or f'gh exit {result.returncode}'}"
        )
    return [int(line) for line in result.stdout.split() if line.strip().isdigit()]


def fetch_override(consumer: Consumer, dest_dir: Path) -> Path | None:
    """Copy the consumer's override YAML out of its default branch, if any.

    Only a confirmed 404 means "this repo has no override".  Every other gh
    failure — expired token, rate limit, bad ref — must not silently fall back
    to the default policy: the override is what makes a consumer *more*
    restrictive, so losing it could merge a PR the repo meant to hold.
    """
    if not consumer.override_path:
        return None
    result = run_gh(
        "api",
        "-H",
        "Accept: application/vnd.github.raw",
        f"repos/{consumer.repo}/contents/{consumer.override_path}"
        f"?ref={consumer.default_branch}",
        check=False,
    )
    if result.returncode != 0:
        if "HTTP 404" in result.stderr:
            return None
        raise OverrideFetchError(
            f"{consumer.repo}: cannot read {consumer.override_path} — "
            f"{result.stderr.strip() or f'gh exit {result.returncode}'}"
        )
    dest = dest_dir / f"{consumer.repo.replace('/', '__')}-override.yml"
    dest.write_text(result.stdout, encoding="utf-8")
    return dest


def evaluate(
    consumer: Consumer,
    policy_dir: Path,
    override: Path | None,
    dry_run: bool,
    pr_number: int,
) -> int:
    """Evaluate one PR.

    One PR per invocation, with PR_NUMBER pinned: the pinned script's own
    listing goes through `gh pr list` without --limit, which stops at 30.
    """
    env = os.environ | {
        "GITHUB_REPOSITORY": consumer.repo,
        "DEFAULT_BRANCH": consumer.default_branch,
        "POLICY_PATH": str(policy_dir / "policies" / "pr-automerge.yml"),
        "AUTOMATION_COMMENT_AUTHORS": consumer.authors,
        "AUTOMATION_WORKFLOW_NAME": consumer.workflow_name,
        "DRY_RUN": "1" if dry_run else "",
        "PR_NUMBER": str(pr_number),
    }
    if override is None:
        env.pop("POLICY_OVERRIDE_PATH", None)
    else:
        env["POLICY_OVERRIDE_PATH"] = str(override)
    result = subprocess.run(  # noqa: S603
        [sys.executable, str(policy_dir / "scripts" / "pr_merge_automation.py")],
        env=env,
        check=False,
    )
    return result.returncode


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--policy-dir",
        type=Path,
        default=ROOT,
        help="Checkout holding the pinned script + default policy (default: this repo)",
    )
    parser.add_argument("--consumers", type=Path, default=CONSUMERS_PATH)
    parser.add_argument("--work-dir", type=Path, default=Path("poller-work"))
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Classify and print actions without mutating any PR",
    )
    parser.add_argument("--repo", help="Evaluate a single consumer (owner/name or name)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    consumers = selected_consumers(load_consumers(args.consumers))
    if args.repo:
        wanted = args.repo if "/" in args.repo else None
        consumers = [
            c for c in consumers if c.repo == wanted or c.repo.split("/")[1] == args.repo
        ]
        if not consumers:
            print(f"no adopt:true consumer matches {args.repo}", file=sys.stderr)
            return 1

    args.work_dir.mkdir(parents=True, exist_ok=True)
    evaluated: list[str] = []
    failed: list[str] = []
    for consumer in consumers:
        try:
            numbers = open_pr_numbers(consumer.repo)
        except ConsumerUnreadable as exc:
            print(str(exc), file=sys.stderr)
            failed.append(f"{consumer.repo} (unreadable)")
            continue
        if not numbers:
            continue

        print(f"== {consumer.repo}: {len(numbers)} open PR(s)")
        try:
            override = fetch_override(consumer, args.work_dir)
        except OverrideFetchError as exc:
            print(str(exc), file=sys.stderr)
            failed.append(f"{consumer.repo} (override unreadable)")
            continue

        evaluated.append(consumer.repo)
        for number in numbers:
            code = evaluate(
                consumer, args.policy_dir, override, args.dry_run, number
            )
            if code != 0:
                failed.append(f"{consumer.repo}#{number} (exit {code})")

    print(
        json.dumps(
            {
                "consumers": len(consumers),
                "evaluated": evaluated,
                "failed": failed,
                "dry_run": args.dry_run,
            },
            indent=2,
        )
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
