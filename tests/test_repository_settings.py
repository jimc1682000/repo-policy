import pytest

from scripts.repository_settings import (
    apply_repository,
    desired_state,
    render_report,
    select_like,
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
            "profile": "python",
            "default_branch": "main",
        }
    ],
}


def test_desired_state_adds_profile_checks_without_mutating_policy():
    desired = desired_state(POLICY, INVENTORY, INVENTORY["repositories"][0])

    assert desired["owner"] == "example"
    assert desired["ruleset"]["rules"][-1] == {
        "type": "required_status_checks",
        "parameters": {
            "do_not_enforce_on_create": True,
            "required_status_checks": [{"context": "test"}],
            "strict_required_status_checks_policy": True,
        },
    }
    assert POLICY["baseline"]["ruleset"]["rules"] == [{"type": "deletion"}]


def test_validate_configuration_rejects_unknown_profile():
    inventory = {
        **INVENTORY,
        "repositories": [{**INVENTORY["repositories"][0], "profile": "node"}],
    }

    with pytest.raises(ValueError, match="unknown profile"):
        validate_configuration(POLICY, inventory)


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
