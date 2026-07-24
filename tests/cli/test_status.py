from __future__ import annotations

import json

from typer.testing import CliRunner

import dataplat.main as main_module
from dataplat.cli import status as status_cli

runner = CliRunner()


def _disable_envrc(monkeypatch) -> None:
    monkeypatch.setattr(main_module, "load_envrc", lambda: None)


def _patch_sections(monkeypatch) -> None:
    monkeypatch.setattr(
        status_cli,
        "_db_section",
        lambda: {
            "demo_pg": {"reachable": True, "long_running": 0},
            "demo_rs": {"reachable": False, "error": "connection refused"},
        },
    )
    monkeypatch.setattr(
        status_cli,
        "_airbyte_section",
        lambda: {
            "available": True,
            "jobs_last_24h": 12,
            "failed": [{"jobId": 9, "connectionId": "c1"}],
            "running": 2,
        },
    )
    monkeypatch.setattr(
        status_cli,
        "_runners_section",
        lambda: {
            "available": True,
            "runners": [
                {"name": "gha-runner-x", "status": "Up 2 hours", "running": True}
            ],
        },
    )
    monkeypatch.setattr(
        status_cli,
        "_aws_section",
        lambda: {
            "available": True,
            "instance": "prod-db-1",
            "metrics": {"CPUUtilization": 12.5, "FreeStorageSpace": 200 * 1024**3},
        },
    )


def test_status_renders_all_sections(monkeypatch) -> None:
    _disable_envrc(monkeypatch)
    _patch_sections(monkeypatch)

    result = runner.invoke(main_module.app, ["status"])

    assert result.exit_code == 0, result.output
    assert "Databases" in result.output
    assert "demo_pg" in result.output
    assert "connection refused" in result.output  # section degrades, not dies
    assert "Airbyte" in result.output
    assert "1 failed" in result.output
    assert "gha-runner-x" in result.output
    assert "CPU 12.5%" in result.output


def test_status_no_aws_skips_section(monkeypatch) -> None:
    _disable_envrc(monkeypatch)
    _patch_sections(monkeypatch)

    result = runner.invoke(main_module.app, ["status", "--no-aws"])

    assert result.exit_code == 0, result.output
    assert "AWS (RDS)" not in result.output


def test_status_json(monkeypatch) -> None:
    _disable_envrc(monkeypatch)
    _patch_sections(monkeypatch)

    result = runner.invoke(main_module.app, ["status", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["databases"]["demo_pg"]["reachable"] is True
    assert payload["airbyte"]["failed"][0]["jobId"] == 9
    assert payload["runners"]["runners"][0]["name"] == "gha-runner-x"
    assert payload["aws"]["available"] is True


def test_aws_section_uses_sso_login_path(monkeypatch) -> None:
    """The AWS section goes through get_session, which auto-runs `aws sso login`."""
    import dataplat.services.aws.auth as aws_auth

    calls: list[str] = []

    class _FakeCw:
        def get_metric_data(self, **kwargs):
            return {"MetricDataResults": []}

    class _FakeSession:
        def client(self, name):
            return _FakeCw()

    def fake_get_session(*, profile, region, notify=None):
        calls.append(profile)
        return _FakeSession()

    monkeypatch.setattr(aws_auth, "get_session", fake_get_session)
    monkeypatch.setenv("DP_RDS_INSTANCE", "prod-db-1")
    monkeypatch.setenv("DP_AWS_PROFILE", "my-sso-profile")

    section = status_cli._aws_section()

    assert calls == ["my-sso-profile"]
    assert section["available"] is True


def test_aws_section_degrades_on_failed_login(monkeypatch) -> None:
    import dataplat.services.aws.auth as aws_auth
    from dataplat.core.errors import AuthError

    def fake_get_session(*, profile, region, notify=None):
        raise AuthError(f"SSO login failed for profile {profile}")

    monkeypatch.setattr(aws_auth, "get_session", fake_get_session)
    monkeypatch.setenv("DP_RDS_INSTANCE", "prod-db-1")

    section = status_cli._aws_section()

    assert section["available"] is False
    assert "SSO login failed" in section["error"]
