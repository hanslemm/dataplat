"""RDS renderers: CloudWatch and RDS data must render, never be interpreted."""

from __future__ import annotations

import json

import pytest
from botocore.exceptions import ClientError
from rich.console import Console
from typer.testing import CliRunner

from dataplat.cli.cloud.aws import _common
from dataplat.cli.cloud.aws import rds as rds_cli
from dataplat.core.errors import AuthError

runner = CliRunner()

HOSTILE_INSTANCE = "db-[/x]-[bold]prod[/bold]"


class _Session:
    """A boto3 session stand-in returning canned clients."""

    def __init__(self, **clients: object) -> None:
        self._clients = clients

    def client(self, name: str) -> object:
        return self._clients[name]


class _Cw:
    def get_metric_data(self, **kwargs: object) -> dict:
        queries = kwargs["MetricDataQueries"]
        assert isinstance(queries, list)
        return {
            "MetricDataResults": [
                {"Id": q["Id"], "Values": [1.0, 2.0]} for q in queries
            ]
        }


class _Paginator:
    def __init__(self, pages: list[dict]) -> None:
        self._pages = pages

    def paginate(self, **kwargs: object) -> list[dict]:
        return self._pages


class _Rds:
    def __init__(self, pages: list[dict]) -> None:
        self._pages = pages

    def get_paginator(self, name: str) -> _Paginator:
        return _Paginator(self._pages)


@pytest.fixture
def wide(monkeypatch: pytest.MonkeyPatch) -> None:
    """Render wide and unstyled so assertions do not depend on the terminal."""
    monkeypatch.setattr(
        rds_cli,
        "console",
        Console(width=400, no_color=True, legacy_windows=False),
    )


def _patch_session(monkeypatch: pytest.MonkeyPatch, session: _Session) -> None:
    monkeypatch.setattr(rds_cli, "_get_session", lambda profile, region: session)


def test_metrics_title_and_footer_keep_the_instance_name(monkeypatch, wide) -> None:
    _patch_session(monkeypatch, _Session(cloudwatch=_Cw()))

    result = runner.invoke(rds_cli.app, ["metrics", "--instance", HOSTILE_INSTANCE])

    assert result.exit_code == 0, result.stdout
    # once in the table title, once in the footer
    assert result.stdout.count(HOSTILE_INSTANCE) == 2


def test_metrics_client_error_message_is_not_markup(monkeypatch, wide) -> None:
    class _Broken:
        def get_metric_data(self, **kwargs: object) -> dict:
            raise ClientError(
                {
                    "Error": {
                        "Code": "Throttling[/x]",
                        "Message": "slow down [bold]now[/bold]",
                    }
                },
                "GetMetricData",
            )

    _patch_session(monkeypatch, _Session(cloudwatch=_Broken()))

    result = runner.invoke(rds_cli.app, ["metrics", "--instance", "db-1"])

    assert result.exit_code == 1
    assert "Throttling[/x]" in result.stdout
    assert "slow down [bold]now[/bold]" in result.stdout


def test_metrics_json_stays_machine_readable(monkeypatch, wide) -> None:
    _patch_session(monkeypatch, _Session(cloudwatch=_Cw()))

    result = runner.invoke(
        rds_cli.app, ["metrics", "--instance", HOSTILE_INSTANCE, "--json"]
    )

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload[0]["metric"] == "CPUUtilization"
    assert payload[0]["latest"] == 2.0


def test_list_renders_hostile_instance_rows(monkeypatch, wide) -> None:
    pages = [
        {
            "DBInstances": [
                {
                    "DBInstanceIdentifier": HOSTILE_INSTANCE,
                    "DBInstanceClass": "db.[/x].large",
                    "Engine": "postgres[/x]",
                    "EngineVersion": "16.[bold]3[/bold]",
                    "DBInstanceStatus": "backing-up[/x]",
                    "AllocatedStorage": 100,
                    "MultiAZ": True,
                }
            ]
        }
    ]
    _patch_session(monkeypatch, _Session(rds=_Rds(pages)))

    result = runner.invoke(rds_cli.app, ["list"])

    assert result.exit_code == 0, result.stdout
    assert HOSTILE_INSTANCE in result.stdout
    assert "db.[/x].large" in result.stdout
    assert "postgres[/x] 16.[bold]3[/bold]" in result.stdout
    assert "backing-up[/x]" in result.stdout
    assert "100" in result.stdout


def test_list_json_stays_machine_readable(monkeypatch, wide) -> None:
    pages = [
        {
            "DBInstances": [
                {
                    "DBInstanceIdentifier": HOSTILE_INSTANCE,
                    "DBInstanceClass": "db.t3.large",
                    "Engine": "postgres",
                    "EngineVersion": "16.3",
                    "DBInstanceStatus": "available",
                    "AllocatedStorage": 100,
                    "MultiAZ": False,
                }
            ]
        }
    ]
    _patch_session(monkeypatch, _Session(rds=_Rds(pages)))

    result = runner.invoke(rds_cli.app, ["list", "--json"])

    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout)[0]["identifier"] == HOSTILE_INSTANCE


def test_list_client_error_message_is_not_markup(monkeypatch, wide) -> None:
    class _Broken:
        def get_paginator(self, name: str) -> _Paginator:
            raise ClientError(
                {"Error": {"Code": "AccessDenied", "Message": "denied [/x]"}},
                "DescribeDBInstances",
            )

    _patch_session(monkeypatch, _Session(rds=_Broken()))

    result = runner.invoke(rds_cli.app, ["list"])

    assert result.exit_code == 1
    assert "denied [/x]" in result.stdout


def test_plot_rejects_unknown_metric_without_interpreting_it(monkeypatch, wide) -> None:
    result = runner.invoke(rds_cli.app, ["plot", "-m", "cpu[/x]"])

    assert result.exit_code == 1
    assert "Unknown metric 'cpu[/x]'" in result.stdout


# ── the shared aws session/table helpers, exercised through rds ──────────────
# rds is the thinnest caller of _common.cli_session() and _common.make_table();
# both are group-wide contracts, so they are pinned once, here.


def test_auth_error_is_rendered_literally_and_exits_one(monkeypatch, wide) -> None:
    """cli_session's only failure path: an SSO message is data, not markup."""
    message = "SSO login failed for profile [/x] [bold]prod[/bold]"

    def _boom(**kwargs: object):
        raise AuthError(message)

    monkeypatch.setattr(_common, "get_session", _boom)

    result = runner.invoke(rds_cli.app, ["list"])

    assert result.exit_code == 1
    # Unescaped, "[/x]" raises MarkupError and nothing is printed at all.
    assert message in result.stdout


def test_auth_notification_is_rendered_literally(monkeypatch, wide) -> None:
    """The notify callback carries the same profile name into markup."""
    note = "expired token for [/x] [bold]prod[/bold]"

    def _notifying(*, profile: str, region: str | None, notify=None):
        assert notify is not None
        notify(note)
        return _Session(rds=_Rds([{"DBInstances": []}]))

    monkeypatch.setattr(_common, "get_session", _notifying)

    result = runner.invoke(rds_cli.app, ["list"])

    assert result.exit_code == 0, result.stdout
    assert note in result.stdout


def test_make_table_paints_no_row_background() -> None:
    """A hardcoded row background is legible on exactly one terminal theme."""
    table = _common.make_table("Instances")
    table.add_column("Value")
    for i in range(4):
        table.add_row(str(i))

    # no_color must be forced off: the suite sets NO_COLOR, which would strip
    # the very escape sequence this test looks for.
    console = Console(
        width=40,
        force_terminal=True,
        color_system="truecolor",
        no_color=False,
        legacy_windows=False,
    )
    with console.capture() as capture:
        console.print(table)

    # "48;" introduces an ANSI background; row_styles=["", "on #2a2a2a"] emitted
    # 48;2;42;42;42 on every other row. The table's own styles are foregrounds.
    assert "48;" not in capture.get()
    assert table.row_styles == []


@pytest.mark.parametrize(
    ("aliases", "argv", "expected"),
    [
        ("prod=AdminAccess-Prod", ["-p", "prod"], "AdminAccess-Prod"),
        ("prod=AdminAccess-Prod", ["-p", "AdminAccess-QA"], "AdminAccess-QA"),
        ("prod=AdminAccess-Prod", [], "default"),
    ],
    ids=["alias", "full-name", "no-flag"],
)
def test_profile_alias_reaches_boto3_and_the_footer(
    aliases: str, argv: list[str], expected: str, monkeypatch, wide
) -> None:
    """--profile's help promises DP_AWS_PROFILE_ALIASES, so rds must honour it."""
    monkeypatch.setenv("DP_AWS_PROFILE_ALIASES", aliases)
    pages = [
        {
            "DBInstances": [
                {
                    "DBInstanceIdentifier": "db-1",
                    "DBInstanceClass": "db.t3.micro",
                    "Engine": "postgres",
                    "EngineVersion": "16.3",
                    "DBInstanceStatus": "available",
                    "AllocatedStorage": 20,
                    "MultiAZ": False,
                }
            ]
        }
    ]
    seen: dict[str, object] = {}

    def _record(*, profile: str, region: str | None, notify=None):
        seen["profile"] = profile
        return _Session(rds=_Rds(pages))

    monkeypatch.setattr(_common, "get_session", _record)

    result = runner.invoke(rds_cli.app, ["list", *argv])

    assert result.exit_code == 0, result.stdout
    assert seen["profile"] == expected
    # The footer must name the profile boto3 got, not the shorthand typed in.
    assert f"Profile: {expected}" in result.stdout
