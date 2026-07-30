#!/usr/bin/env python3
"""Probe whether a GitHub token has Contents write on a repository.

Owner fine-grained PATs report permissions.admin/push=true on owned public
repos even when the repo is NOT on the token allow-list. REST GET /repos is
therefore untrustworthy for discovery.

We POST an orphan git blob (Contents: write). 201 => granted; 403/404 => no.
Orphan blobs are unreachable and eventually GC'd; we never commit them.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

API = "https://api.github.com"
API_VERSION = "2022-11-28"


def _request(
    method: str,
    url: str,
    token: str,
    body: dict | None = None,
) -> tuple[int, dict | list | str | None]:
    data = None
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": API_VERSION,
        "User-Agent": "repo-policy-pat-probe",
    }
    if body is not None:
        raw = json.dumps(body).encode("utf-8")
        data = raw
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
            raw = resp.read().decode("utf-8")
            if not raw:
                return resp.status, None
            try:
                return resp.status, json.loads(raw)
            except json.JSONDecodeError:
                return resp.status, raw
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            parsed: dict | list | str | None = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            parsed = raw
        return exc.code, parsed


def token_kind(token: str) -> str:
    """Return 'classic' | 'fine-grained' | 'unknown' from /user response headers."""
    req = urllib.request.Request(
        f"{API}/user",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": "repo-policy-pat-probe",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
            scopes = resp.headers.get("X-OAuth-Scopes")
            # Classic PATs send X-OAuth-Scopes (possibly empty string with some configs).
            # Fine-grained omit traditional OAuth scopes; GitHub uses permission model.
            if scopes is not None and scopes.strip() != "":
                return "classic"
            # Empty scopes header with 200 often means fine-grained or GitHub App.
            if resp.headers.get("X-OAuth-Client-Id") or scopes == "":
                # Distinguish: classic with no scopes vs fine-grained.
                # Fine-grained tokens typically start with github_pat_
                if token.startswith("github_pat_"):
                    return "fine-grained"
                if scopes == "":
                    return "classic"  # classic with empty scope list is rare
            if token.startswith("github_pat_"):
                return "fine-grained"
            if token.startswith("ghp_"):
                return "classic"
            return "unknown"
    except urllib.error.HTTPError:
        if token.startswith("github_pat_"):
            return "fine-grained"
        if token.startswith("ghp_"):
            return "classic"
        return "unknown"


def has_contents_write(token: str, full_name: str) -> bool:
    """True if token can create a git blob in the repo (Contents: write)."""
    url = f"{API}/repos/{full_name}/git/blobs"
    # Distinct marker; never committed.
    status, _payload = _request(
        "POST",
        url,
        token,
        {
            "content": "repo-policy-pat-probe",
            "encoding": "utf-8",
        },
    )
    if status in (200, 201):
        return True
    # Empty / uninitialized repos may not support git data API yet.
    if status == 409:
        # Fall back: private visibility probe — if we can see private repo at all.
        st, data = _request("GET", f"{API}/repos/{full_name}", token)
        if st != 200 or not isinstance(data, dict):
            return False
        # Uninitialized private + can see it => on allow-list for metadata at least.
        # Without blob write we cannot prove Contents write; treat private visible as yes.
        return bool(data.get("private"))
    return False


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(
            "usage: pat_probe.py kind|check <owner/repo>",
            file=sys.stderr,
        )
        return 2
    token = os.environ.get("AUTOMERGE_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        print("error: AUTOMERGE_TOKEN unset", file=sys.stderr)
        return 2
    cmd = argv[1]
    if cmd == "kind":
        print(token_kind(token))
        return 0
    if cmd == "check":
        if len(argv) < 3:
            print("usage: pat_probe.py check owner/repo", file=sys.stderr)
            return 2
        full = argv[2]
        ok = has_contents_write(token, full)
        print("yes" if ok else "no")
        return 0 if ok else 1
    print(f"unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
