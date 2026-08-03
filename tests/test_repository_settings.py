import json

import pytest

from scripts.repository_settings import (
    GitHubError,
    apply_repository,
    audit_repository,
    discover_repositories,
    desired_state,
    failure_results,
    main,
    render_report,
    resolve_inventory,
    select_like,
    validate_apply_scope,
    validate_configuration,
    verify_actor,
)


POLICY = {
    "version": 1,
    "baseline": {
        "repository_settings": {"allow_squash_merge": True},
        "security_and_analysis": {"secret_scanning": "enabled"},
        "ruleset": {"name": "baseline", "rules": [{"type": "deletion"}]},
    },
    "profiles": {"python": {"required_status_checks": ["test"]}},
}
INVENTORY = {
    "owner": "example",
    "repositories": [
        {
            "repo": "app",
            "manage": True,
            "apply": True,
            "profile": "python",
            "default_branch": "main",
        }
    ],
}


def test_desired_state_adds_profile_checks_without_mutating_policy():
    desired = desired_state(POLICY, INVENTORY, INVENTORY["repositories"][0])

    assert desired["owner"] == "example"
    assert desired["apply_enabled"] is True
    assert desired["ruleset"]["rules"][-1] == {
        "type": "required_status_checks",
        "parameters": {
            "do_not_enforce_on_create": True,
            "required_status_checks": [{"context": "test"}],
            "strict_required_status_checks_policy": True,
        },
    }
    assert POLICY["baseline"]["ruleset"]["rules"] == [{"type": "deletion"}]


def test_desired_state_applies_repository_override():
    item = {
        **INVENTORY["repositories"][0],
        "overrides": {"repository_settings": {"allow_auto_merge": True}},
    }

    desired = desired_state(POLICY, INVENTORY, item)

    assert desired["repository_settings"]["allow_auto_merge"] is True


def test_validate_configuration_rejects_unknown_profile():
    inventory = {
        **INVENTORY,
        "repositories": [{**INVENTORY["repositories"][0], "profile": "node"}],
    }

    with pytest.raises(ValueError, match="unknown profile"):
        validate_configuration(POLICY, inventory)


def test_validate_configuration_rejects_unknown_profile_in_partial_override():
    inventory = {
        **INVENTORY,
        "discovery": {"enabled": True, "profile": "python"},
        "repositories": [{"repo": "app", "profile": "node"}],
    }

    with pytest.raises(ValueError, match="unknown profile for app: node"):
        validate_configuration(POLICY, inventory)


def test_validate_configuration_rejects_unknown_discovery_profile():
    inventory = {**INVENTORY, "discovery": {"enabled": True, "profile": "node"}}

    with pytest.raises(ValueError, match="unknown discovery profile"):
        validate_configuration(POLICY, inventory)


def test_discover_repositories_paginates_and_classifies_repositories():
    class Client:
        def request(self, method, path):
            assert method == "GET"
            if path.endswith("&page=1"):
                return [
                    {
                        "name": f"repo-{index}",
                        "owner": {"login": "example"},
                        "default_branch": "main",
                        "archived": False,
                        "fork": False,
                        "pushed_at": "2026-08-03T00:00:00Z",
                    }
                    for index in range(100)
                ]
            if path.endswith("&page=2"):
                return [
                    {
                        "name": "archive",
                        "owner": {"login": "example"},
                        "default_branch": "main",
                        "archived": True,
                        "fork": False,
                        "pushed_at": "2026-08-03T00:00:00Z",
                    },
                    {
                        "name": "foreign",
                        "owner": {"login": "someone-else"},
                        "default_branch": "main",
                        "archived": False,
                        "fork": False,
                        "pushed_at": "2026-08-03T00:00:00Z",
                    },
                ]
            raise AssertionError(f"unexpected request: {path}")

    repositories = discover_repositories(
        Client(),
        {
            "owner": "example",
            "discovery": {"enabled": True, "profile": "baseline"},
        },
    )

    assert len(repositories) == 101
    assert repositories[-1]["repo"] == "archive"
    assert repositories[-1]["classification"] == "archived"
    assert repositories[-1]["apply"] is False


def test_discover_repositories_detects_empty_repo_with_default_branch_name():
    class Client:
        def request(self, method, path):
            return [
                {
                    "name": "empty-app",
                    "owner": {"login": "example"},
                    "default_branch": "main",
                    "archived": False,
                    "fork": False,
                    "pushed_at": None,
                }
            ]

    repositories = discover_repositories(
        Client(),
        {
            "owner": "example",
            "discovery": {"enabled": True, "profile": "python"},
        },
    )

    assert repositories[0]["classification"] == "empty"


def test_audit_empty_repository_does_not_request_check_runs():
    class Client:
        def request(self, method, path, payload=None):
            if path == "/repos/example/app":
                return {
                    "default_branch": "main",
                    "allow_squash_merge": True,
                    "security_and_analysis": {"secret_scanning": {"status": "enabled"}},
                }
            if path == "/repos/example/app/rulesets":
                return []
            raise AssertionError(f"unexpected request: {path}")

    item = {**INVENTORY["repositories"][0], "classification": "empty"}
    desired = desired_state(POLICY, INVENTORY, item)
    result = audit_repository(Client(), desired)

    assert result["blockers"] == [
        "required checks cannot be observed: repository has no commits"
    ]


def test_resolve_inventory_applies_explicit_override_to_discovered_repository(
    monkeypatch,
):
    monkeypatch.setattr(
        "scripts.repository_settings.discover_repositories",
        lambda client, inventory: [
            {
                "repo": "app",
                "manage": True,
                "apply": False,
                "profile": "baseline",
                "default_branch": "main",
                "classification": "audit-only",
            }
        ],
    )
    inventory = {
        "owner": "example",
        "discovery": {"enabled": True, "profile": "baseline"},
        "repositories": [
            {
                "repo": "app",
                "apply": True,
                "profile": "python",
                "classification": "active",
            }
        ],
    }

    repositories = resolve_inventory(object(), inventory)

    assert repositories == [
        {
            "repo": "app",
            "manage": True,
            "apply": True,
            "profile": "python",
            "default_branch": "main",
            "classification": "active",
        }
    ]


def test_validate_apply_scope_rejects_audit_only_repository():
    with pytest.raises(ValueError, match="apply is disabled.*docs"):
        validate_apply_scope(
            [
                {"repo": "app", "apply": True},
                {"repo": "docs", "apply": False},
            ]
        )


def test_select_like_ignores_server_fields_and_matches_rules_by_type():
    desired = {"name": "baseline", "rules": [{"type": "deletion"}]}
    current = {
        "id": 42,
        "name": "baseline",
        "rules": [
            {"id": 7, "type": "non_fast_forward"},
            {"id": 8, "type": "deletion"},
        ],
    }

    assert select_like(current, desired) == desired


def test_select_like_ignores_integration_id_added_to_required_check():
    desired = {"required_status_checks": [{"context": "test"}]}
    current = {
        "required_status_checks": [
            {"context": "test", "integration_id": 15368},
        ]
    }

    assert select_like(current, desired) == desired


def test_apply_refuses_when_audit_has_blockers():
    with pytest.raises(ValueError, match="blockers present"):
        apply_repository(
            object(),
            {"owner": "example", "repo": "app"},
            {"repository": "example/app", "blockers": ["missing check"]},
        )


def test_audit_marks_plan_limited_ruleset_unavailable():
    class Client:
        def request(self, method, path, payload=None):
            if path == "/repos/example/app":
                return {
                    "default_branch": "main",
                    "allow_squash_merge": True,
                    "security_and_analysis": {"secret_scanning": {"status": "enabled"}},
                }
            if path == "/repos/example/app/rulesets":
                raise GitHubError(
                    "GET /repos/example/app/rulesets: Upgrade to GitHub Pro or make "
                    "this repository public to enable this feature. (HTTP 403)"
                )
            if "check-runs" in path:
                return {"check_runs": [{"name": "test"}], "total_count": 1}
            raise AssertionError(f"unexpected request: {path}")

    desired = desired_state(POLICY, INVENTORY, INVENTORY["repositories"][0])
    result = audit_repository(Client(), desired)

    assert result["changes"] == []
    assert result["unavailable"] == [
        {
            "area": "ruleset",
            "key": "baseline",
            "reason": "GitHub plan limitation",
        }
    ]


def test_audit_does_not_hide_unrelated_ruleset_403():
    class Client:
        def request(self, method, path, payload=None):
            if path == "/repos/example/app":
                return {
                    "default_branch": "main",
                    "allow_squash_merge": True,
                    "security_and_analysis": {"secret_scanning": {"status": "enabled"}},
                }
            raise GitHubError("GET rulesets: Resource not accessible (HTTP 403)")

    desired = desired_state(POLICY, INVENTORY, INVENTORY["repositories"][0])

    with pytest.raises(GitHubError, match="Resource not accessible"):
        audit_repository(Client(), desired)


def test_audit_preserves_missing_security_feature_as_unavailable():
    class Client:
        def request(self, method, path, payload=None):
            if path == "/repos/example/app":
                return {
                    "default_branch": "main",
                    "allow_squash_merge": True,
                    "security_and_analysis": {},
                }
            if path == "/repos/example/app/rulesets":
                return []
            if "check-runs" in path:
                return {"check_runs": [{"name": "test"}], "total_count": 1}
            raise AssertionError(f"unexpected request: {path}")

    desired = desired_state(POLICY, INVENTORY, INVENTORY["repositories"][0])
    result = audit_repository(Client(), desired)

    assert not any(change["area"] == "security" for change in result["changes"])
    assert result["unavailable"] == [
        {
            "area": "security",
            "key": "secret_scanning",
            "reason": "not returned by repository API",
        }
    ]


def test_apply_only_sends_security_features_observed_as_drift():
    class Client:
        def __init__(self):
            self.payload = None

        def request(self, method, path, payload=None):
            assert (method, path) == ("PATCH", "/repos/example/app")
            self.payload = payload

    client = Client()
    desired = desired_state(POLICY, INVENTORY, INVENTORY["repositories"][0])
    audit = {
        "repository": "example/app",
        "blockers": [],
        "changes": [
            {
                "area": "security",
                "key": "secret_scanning",
                "from": "disabled",
                "to": "enabled",
            }
        ],
        "ruleset_id": None,
    }

    apply_repository(client, desired, audit)

    assert client.payload["security_and_analysis"] == {
        "secret_scanning": {"status": "enabled"}
    }


def test_verify_actor_rejects_a_different_authenticated_user():
    class Client:
        def request(self, method, path):
            assert (method, path) == ("GET", "/user")
            return {"login": "someone-else"}

    with pytest.raises(ValueError, match="authenticated actor mismatch"):
        verify_actor(Client(), "example")


def test_render_report_makes_report_only_and_drift_visible():
    report = render_report(
        [
            {
                "repository": "example/app",
                "profile": "python",
                "blockers": [],
                "changes": [
                    {"area": "repository", "key": "x", "from": False, "to": True}
                ],
            }
        ]
    )

    assert "Mode: report-only" in report
    assert "DRIFT repository.x" in report
    assert "Planned changes: 1" in report


def test_render_report_does_not_mark_unavailable_audit_compliant():
    report = render_report(
        [
            {
                "repository": "example/private-app",
                "profile": "baseline",
                "blockers": [],
                "changes": [],
                "unavailable": [
                    {
                        "area": "ruleset",
                        "key": "baseline",
                        "reason": "GitHub plan limitation",
                    }
                ],
            }
        ]
    )

    assert "UNAVAILABLE ruleset.baseline" in report
    assert "PASS compliant" not in report


def test_report_only_finds_required_check_on_later_page(tmp_path, capsys):
    policy_path = tmp_path / "policy.yml"
    inventory_path = tmp_path / "repositories.yml"
    policy_path.write_text(json.dumps(POLICY), encoding="utf-8")
    inventory_path.write_text(json.dumps(INVENTORY), encoding="utf-8")

    class Client:
        def request(self, method, path):
            assert method == "GET"
            if path == "/user":
                return {"login": "example"}
            if path == "/repos/example/app":
                return {
                    "default_branch": "main",
                    "allow_squash_merge": True,
                    "security_and_analysis": {"secret_scanning": {"status": "enabled"}},
                }
            if path == "/repos/example/app/rulesets":
                return []
            if "check-runs" in path and "page=2" in path:
                return {"check_runs": [{"name": "test"}], "total_count": 101}
            if "check-runs" in path:
                return {
                    "check_runs": [
                        {"name": f"scheduled-{index}"} for index in range(100)
                    ],
                    "total_count": 101,
                }
            raise AssertionError(f"unexpected request: {path}")

    result = main(
        [
            "--policy",
            str(policy_path),
            "--inventory",
            str(inventory_path),
            "--repo",
            "app",
        ],
        client=Client(),
    )

    assert result == 0
    assert "BLOCKED" not in capsys.readouterr().out


def test_failure_results_can_limit_exit_code_to_active_repositories():
    results = [
        {
            "repository": "example/app",
            "classification": "active",
            "blockers": ["missing check"],
            "changes": [],
        },
        {
            "repository": "example/lab",
            "classification": "audit-only",
            "blockers": ["default branch mismatch"],
            "changes": [{"area": "repository", "key": "x", "from": 1, "to": 2}],
        },
    ]

    assert failure_results(results, active_only=True) == [results[0]]
    assert failure_results(results, active_only=False) == results


def test_fail_on_active_ignores_audit_only_blockers_and_drift(tmp_path, capsys):
    policy_path = tmp_path / "policy.yml"
    inventory_path = tmp_path / "repositories.yml"
    policy = {
        "version": 1,
        "baseline": {
            "repository_settings": {"allow_squash_merge": True},
            "security_and_analysis": {"secret_scanning": "enabled"},
            "ruleset": {"name": "baseline", "rules": [{"type": "deletion"}]},
        },
        "profiles": {
            "baseline": {"required_status_checks": []},
            "python": {"required_status_checks": ["test"]},
        },
    }
    inventory = {
        "owner": "example",
        "discovery": {"enabled": False},
        "repositories": [
            {
                "repo": "app",
                "manage": True,
                "apply": True,
                "profile": "python",
                "default_branch": "main",
                "classification": "active",
            },
            {
                "repo": "lab",
                "manage": True,
                "apply": False,
                "profile": "baseline",
                "default_branch": "main",
                "classification": "audit-only",
            },
        ],
    }
    policy_path.write_text(json.dumps(policy), encoding="utf-8")
    inventory_path.write_text(json.dumps(inventory), encoding="utf-8")

    class Client:
        def request(self, method, path, payload=None):
            assert method == "GET"
            if path == "/user":
                return {"login": "example"}
            if path == "/repos/example/app":
                return {
                    "default_branch": "main",
                    "allow_squash_merge": True,
                    "security_and_analysis": {"secret_scanning": {"status": "enabled"}},
                }
            if path == "/repos/example/lab":
                return {
                    "default_branch": "main",
                    "allow_squash_merge": False,
                    "security_and_analysis": {
                        "secret_scanning": {"status": "disabled"}
                    },
                }
            if path == "/repos/example/app/rulesets":
                return [
                    {
                        "id": 1,
                        "name": "baseline",
                        "source_type": "Repository",
                    }
                ]
            if path == "/repos/example/lab/rulesets":
                return []
            if path == "/repos/example/app/rulesets/1":
                return {
                    "id": 1,
                    "name": "baseline",
                    "source_type": "Repository",
                    "rules": [
                        {"type": "deletion"},
                        {
                            "type": "required_status_checks",
                            "parameters": {
                                "do_not_enforce_on_create": True,
                                "required_status_checks": [{"context": "test"}],
                                "strict_required_status_checks_policy": True,
                            },
                        },
                    ],
                }
            if "check-runs" in path:
                return {"check_runs": [{"name": "test"}], "total_count": 1}
            raise AssertionError(f"unexpected request: {path}")

    result = main(
        [
            "--policy",
            str(policy_path),
            "--inventory",
            str(inventory_path),
            "--fail-on-active",
            "--fail-on-drift",
        ],
        client=Client(),
    )
    output = capsys.readouterr().out

    assert result == 0
    assert "example/lab" in output
    assert "DRIFT" in output


def test_fail_on_active_still_fails_when_active_has_drift(tmp_path, capsys):
    policy_path = tmp_path / "policy.yml"
    inventory_path = tmp_path / "repositories.yml"
    inventory = {
        **INVENTORY,
        "repositories": [
            {
                **INVENTORY["repositories"][0],
                "classification": "active",
            }
        ],
    }
    policy_path.write_text(json.dumps(POLICY), encoding="utf-8")
    inventory_path.write_text(json.dumps(inventory), encoding="utf-8")

    class Client:
        def request(self, method, path, payload=None):
            if path == "/user":
                return {"login": "example"}
            if path == "/repos/example/app":
                return {
                    "default_branch": "main",
                    "allow_squash_merge": False,
                    "security_and_analysis": {"secret_scanning": {"status": "enabled"}},
                }
            if path == "/repos/example/app/rulesets":
                return []
            if "check-runs" in path:
                return {"check_runs": [{"name": "test"}], "total_count": 1}
            raise AssertionError(f"unexpected request: {path}")

    result = main(
        [
            "--policy",
            str(policy_path),
            "--inventory",
            str(inventory_path),
            "--fail-on-active",
            "--fail-on-drift",
        ],
        client=Client(),
    )

    assert result == 1
    assert "DRIFT" in capsys.readouterr().out


def test_output_paths_write_json_and_text_reports(tmp_path, capsys):
    policy_path = tmp_path / "policy.yml"
    inventory_path = tmp_path / "repositories.yml"
    output_json = tmp_path / "out" / "audit.json"
    output_text = tmp_path / "out" / "audit.md"
    policy_path.write_text(json.dumps(POLICY), encoding="utf-8")
    inventory_path.write_text(json.dumps(INVENTORY), encoding="utf-8")

    class Client:
        def request(self, method, path, payload=None):
            if path == "/user":
                return {"login": "example"}
            if path == "/repos/example/app":
                return {
                    "default_branch": "main",
                    "allow_squash_merge": True,
                    "security_and_analysis": {"secret_scanning": {"status": "enabled"}},
                }
            if path == "/repos/example/app/rulesets":
                return []
            if "check-runs" in path:
                return {"check_runs": [{"name": "test"}], "total_count": 1}
            raise AssertionError(f"unexpected request: {path}")

    result = main(
        [
            "--policy",
            str(policy_path),
            "--inventory",
            str(inventory_path),
            "--output-json",
            str(output_json),
            "--output-text",
            str(output_text),
        ],
        client=Client(),
    )

    assert result == 0
    payload = json.loads(output_json.read_text(encoding="utf-8"))
    text = output_text.read_text(encoding="utf-8")
    assert payload["mode"] == "report-only"
    assert payload["results"][0]["repository"] == "example/app"
    assert "Mode: report-only" in text
    assert "Mode: report-only" in capsys.readouterr().out
