from __future__ import annotations

import json

import pytest
from botocore.exceptions import ClientError
from rich.console import Console
from typer.testing import CliRunner

from dataplat.cli.cloud.aws import _common
from dataplat.cli.cloud.aws import redshift as redshift_cli
from dataplat.cli.cloud.aws.app import app as aws_app
from dataplat.cli.cloud.aws.redshift import app as redshift_app
from dataplat.core.trace import verbose

runner = CliRunner()

HOSTILE_WG = "wg-[/x]-[bold]prod[/bold]"


def test_long_queries_alias_removed() -> None:
    result = runner.invoke(redshift_app, ["long-queries"])
    assert result.exit_code == 2  # no such command


def test_metrics_command_exists() -> None:
    result = runner.invoke(redshift_app, ["metrics", "--help"])
    assert result.exit_code == 0


class _Session:
    """A boto3 session stand-in returning canned clients."""

    def __init__(self, **clients: object) -> None:
        self._clients = clients

    def client(self, name: str) -> object:
        return self._clients[name]


class _Serverless:
    def __init__(self, workgroups: list[dict]) -> None:
        self._workgroups = workgroups

    def list_workgroups(self, **kwargs: object) -> dict:
        return {"workgroups": self._workgroups}


class _MetricsPaginator:
    def paginate(self, **kwargs: object):
        # Echo the requested dimensions back so _discover_metric_dims matches.
        yield {"Metrics": [{"Dimensions": kwargs["Dimensions"]}]}


class _Cw:
    def get_paginator(self, name: str) -> _MetricsPaginator:
        return _MetricsPaginator()

    def get_metric_data(self, **kwargs: object) -> dict:
        queries = kwargs["MetricDataQueries"]
        assert isinstance(queries, list)
        return {
            "MetricDataResults": [
                {"Id": q["Id"], "Values": [1.0, 2.0]} for q in queries
            ]
        }


@pytest.fixture
def wide(monkeypatch: pytest.MonkeyPatch) -> None:
    """Render wide and unstyled so assertions do not depend on the terminal."""
    monkeypatch.setattr(
        redshift_cli,
        "console",
        Console(width=400, no_color=True, legacy_windows=False),
    )


def _patch_session(monkeypatch: pytest.MonkeyPatch, session: _Session) -> None:
    monkeypatch.setattr(redshift_cli, "_get_session", lambda profile, region: session)


def test_metrics_renders_hostile_workgroup_scopes(monkeypatch, wide) -> None:
    workgroups = [{"workgroupName": HOSTILE_WG, "namespaceName": "ns-[/y]"}]
    _patch_session(
        monkeypatch,
        _Session(**{"redshift-serverless": _Serverless(workgroups)}, cloudwatch=_Cw()),
    )

    result = runner.invoke(aws_app, ["redshift", "metrics"])

    assert result.exit_code == 0, result.stdout
    assert f"workgroup:{HOSTILE_WG}" in result.stdout
    assert "namespace:ns-[/y]" in result.stdout


def test_metrics_json_stays_machine_readable(monkeypatch, wide) -> None:
    workgroups = [{"workgroupName": HOSTILE_WG, "namespaceName": "ns"}]
    _patch_session(
        monkeypatch,
        _Session(**{"redshift-serverless": _Serverless(workgroups)}, cloudwatch=_Cw()),
    )

    result = runner.invoke(aws_app, ["redshift", "metrics", "--json"])

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload[0]["scope"] == f"workgroup:{HOSTILE_WG}"


def test_metrics_missing_workgroup_message_is_not_markup(monkeypatch, wide) -> None:
    _patch_session(
        monkeypatch,
        _Session(**{"redshift-serverless": _Serverless([])}, cloudwatch=_Cw()),
    )

    result = runner.invoke(aws_app, ["redshift", "metrics", "-w", HOSTILE_WG])

    assert result.exit_code == 1
    assert f"No workgroup named '{HOSTILE_WG}' found." in result.stdout


def test_metrics_client_error_message_is_not_markup(monkeypatch, wide) -> None:
    class _Broken:
        def list_workgroups(self, **kwargs: object) -> dict:
            raise ClientError(
                {
                    "Error": {
                        "Code": "AccessDenied[/x]",
                        "Message": "denied [bold]hard[/bold]",
                    }
                },
                "ListWorkgroups",
            )

    _patch_session(
        monkeypatch, _Session(**{"redshift-serverless": _Broken()}, cloudwatch=_Cw())
    )

    result = runner.invoke(aws_app, ["redshift", "metrics"])

    assert result.exit_code == 1
    assert "AccessDenied[/x]" in result.stdout
    assert "denied [bold]hard[/bold]" in result.stdout


@pytest.mark.parametrize(
    ("argv", "expected"),
    [(["-p", "prod"], "AdminAccess-Prod"), ([], "default")],
    ids=["alias", "no-flag"],
)
def test_profile_alias_reaches_boto3_and_the_footer(
    argv: list[str], expected: str, monkeypatch, wide
) -> None:
    """--profile's help promises DP_AWS_PROFILE_ALIASES for redshift too."""
    monkeypatch.setenv("DP_AWS_PROFILE_ALIASES", "prod=AdminAccess-Prod")
    session = _Session(
        **{"redshift-serverless": _Serverless([{"workgroupName": "wg"}])},
        cloudwatch=_Cw(),
    )
    seen: dict[str, object] = {}

    def _record(*, profile: str, region: str | None, notify=None):
        seen["profile"] = profile
        return session

    monkeypatch.setattr(_common, "get_session", _record)

    result = runner.invoke(aws_app, ["redshift", "metrics", *argv])

    assert result.exit_code == 0, result.stdout
    assert seen["profile"] == expected
    # The footer must name the profile boto3 got, not the shorthand typed in.
    assert f"Profile: {expected}" in result.stdout


# ── --verbose ────────────────────────────────────────────────────────────────
# This command makes three different kinds of call and the middle one — the
# per-metric dimension discovery — is why a row goes missing from the table
# without any error at all. Tracing is the only way to see that happen.


def test_verbose_traces_discovery_and_the_batched_fetch(monkeypatch, wide) -> None:
    for var in ("AWS_REGION", "AWS_DEFAULT_REGION"):
        monkeypatch.delenv(var, raising=False)
    workgroups = [{"workgroupName": "wg-1", "namespaceName": "ns-1"}]
    _patch_session(
        monkeypatch,
        _Session(**{"redshift-serverless": _Serverless(workgroups)}, cloudwatch=_Cw()),
    )

    with verbose():
        result = runner.invoke(
            aws_app,
            ["redshift", "metrics", "-r", "eu-central-1", "--hours", "2"],
        )

    assert result.exit_code == 0, result.stdout
    err = result.stderr
    assert (
        "[dp:aws] redshift-serverless.list_workgroups | profile=default | "
        "region=eu-central-1 | workgroup=(all)" in err
    )
    # Seven workgroup metrics plus two namespace metrics, each discovered
    # separately, then one batched call for all nine.
    assert err.count("cloudwatch.list_metrics") == 9
    assert "| metric=ComputeCapacity | workgroup=wg-1" in err
    assert "| metric=DataStorage | namespace=ns-1" in err
    assert (
        "[dp:aws] cloudwatch.get_metric_data | profile=default | "
        "region=eu-central-1 | series=9 | window=2.0h | period=300s" in err
    )


def test_verbose_keeps_the_json_payload_clean(monkeypatch, wide) -> None:
    _patch_session(
        monkeypatch,
        _Session(
            **{"redshift-serverless": _Serverless([{"workgroupName": "wg"}])},
            cloudwatch=_Cw(),
        ),
    )

    with verbose():
        result = runner.invoke(aws_app, ["redshift", "metrics", "--json"])

    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout)[0]["scope"] == "workgroup:wg"
    assert "[dp:aws]" in result.stderr


def test_nothing_is_traced_without_verbose(monkeypatch, wide) -> None:
    _patch_session(
        monkeypatch,
        _Session(
            **{"redshift-serverless": _Serverless([{"workgroupName": "wg"}])},
            cloudwatch=_Cw(),
        ),
    )

    result = runner.invoke(aws_app, ["redshift", "metrics"])

    assert result.exit_code == 0, result.stdout
    assert result.stderr == ""
