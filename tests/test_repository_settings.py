import json

import pytest

from scripts.repository_settings import (
    apply_repository,
    desired_state,
    main,
    render_report,
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
