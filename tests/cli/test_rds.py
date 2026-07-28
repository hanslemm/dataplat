"""RDS renderers: CloudWatch and RDS data must render, never be interpreted.

Most of this module is a plotext chart, which is why it was the worst-covered
file in the repo: a chart is 200 lines that produce box-drawing characters no
assertion can sensibly pin. What *is* assertable sits either side of the drawing
— the request (window, period, which metrics), the arithmetic (summaries,
extrema, tick labels), the JSON payload, and the error branches — so that is what
the second half of this file covers, plus one end-to-end render that proves the
whole chart path runs against a fake CloudWatch without raising.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta

import pytest
from botocore.exceptions import ClientError
from rich.console import Console
from typer.testing import CliRunner

from dataplat.cli.cloud.aws import _common
from dataplat.cli.cloud.aws import rds as rds_cli
from dataplat.core.errors import AuthError, ExitCode
from dataplat.core.trace import verbose

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


def test_auth_error_is_rendered_literally_and_exits_four(monkeypatch, wide) -> None:
    """cli_session's only failure path: exit 4, and the message is data.

    The code is the load-bearing half. Every aws command reaches AWS through
    ``cli_session``, so this is where "your SSO session expired" stops being
    indistinguishable from every other failure: a wrapper script retries 4 by
    re-running ``aws sso login`` and must not retry a 3 (bad config) that way.
    """
    message = "SSO login failed for profile [/x] [bold]prod[/bold]"

    def _boom(**kwargs: object):
        raise AuthError(message)

    monkeypatch.setattr(_common, "get_session", _boom)

    result = runner.invoke(rds_cli.app, ["list"])

    assert result.exit_code == ExitCode.AUTH == 4
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


# ── the request: window, period and units ────────────────────────────────────
# Everything that decides what CloudWatch sends back is computed before the
# call, and a wrong window is invisible afterwards — the table renders happily
# either way — so it is pinned on the request instead of the output.


class _StatCw:
    """CloudWatch for ``metrics``: records the request, answers per query Id.

    Answering per Id is the point. Average, Minimum and Maximum come back as
    three separate series, and a summary that read the wrong one would still
    produce a plausible-looking table.
    """

    def __init__(
        self,
        by_id: dict[str, list[float]] | None = None,
        default: list[float] | None = None,
    ) -> None:
        self.by_id = by_id or {}
        self.default = [1.0, 2.0] if default is None else default
        self.calls: list[dict] = []

    def get_metric_data(self, **kwargs: object) -> dict:
        self.calls.append(dict(kwargs))
        queries = kwargs["MetricDataQueries"]
        assert isinstance(queries, list)
        return {
            "MetricDataResults": [
                {"Id": q["Id"], "Values": self.by_id.get(q["Id"], self.default)}
                for q in queries
            ]
        }


# The first datapoint of every plotted series; every x tick label below is
# derived from it, so it is fixed rather than "now".
FIRST_POINT = datetime(2026, 7, 27, 9, 15, tzinfo=UTC)

_ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")


def _plain(text: str) -> str:
    """Drop the escape sequences plotext emits regardless of NO_COLOR.

    The chart is drawn with its own colours, so a label such as ``MIN 12.0%``
    survives as contiguous characters only once the sequences around it are gone.
    """
    return _ANSI.sub("", text)


class _StatsCw:
    """CloudWatch for ``plot``: one get_metric_statistics call per metric.

    ``series`` maps a metric name to what it answers with. An empty list is the
    "no datapoints" case and an ``Exception`` is raised instead of answering —
    both are branches the chart path has to survive. ``newest_first`` returns the
    points in the order CloudWatch is free to use, which is not the order a chart
    can plot.
    """

    def __init__(
        self,
        series: dict[str, list[float] | Exception] | None = None,
        *,
        default: list[float] | None = None,
        step: timedelta = timedelta(minutes=5),
        newest_first: bool = False,
    ) -> None:
        self.series = series or {}
        self.default = [12.0, 40.0, 88.0] if default is None else default
        self.step = step
        self.newest_first = newest_first
        self.calls: list[dict] = []

    def get_metric_statistics(self, **kwargs: object) -> dict:
        self.calls.append(dict(kwargs))
        answer = self.series.get(str(kwargs["MetricName"]), self.default)
        if isinstance(answer, Exception):
            raise answer
        points = [
            {"Timestamp": FIRST_POINT + i * self.step, "Average": value}
            for i, value in enumerate(answer)
        ]
        if self.newest_first:
            points.reverse()
        return {"Datapoints": points}


def test_metrics_sends_one_call_carrying_window_period_and_units(
    monkeypatch, wide
) -> None:
    cw = _StatCw()
    _patch_session(monkeypatch, _Session(cloudwatch=cw))

    result = runner.invoke(
        rds_cli.app,
        ["metrics", "-i", "db-1", "--hours", "2.5", "--period", "60", "--json"],
    )

    assert result.exit_code == 0, result.stdout
    assert len(cw.calls) == 1, "six metrics must cost one call, not six"
    call = cw.calls[0]

    # The window ends "now", so only its span is deterministic — and the span is
    # the part --hours is responsible for.
    assert call["EndTime"] - call["StartTime"] == timedelta(hours=2.5)
    assert call["StartTime"].tzinfo is not None, "a naive datetime is a wrong window"
    assert call["ScanBy"] == "TimestampAscending", "latest = avgs[-1] depends on it"

    queries = call["MetricDataQueries"]
    assert len(queries) == 3 * len(rds_cli._CW_METRICS)
    assert {q["MetricStat"]["Period"] for q in queries} == {60}
    assert {q["MetricStat"]["Stat"] for q in queries} == {
        "Average",
        "Minimum",
        "Maximum",
    }
    assert all(
        q["MetricStat"]["Metric"]["Namespace"] == "AWS/RDS"
        and q["MetricStat"]["Metric"]["Dimensions"]
        == [{"Name": "DBInstanceIdentifier", "Value": "db-1"}]
        for q in queries
    )
    # Each metric carries its own unit; CloudWatch returns nothing at all for a
    # mismatched one, which is a silent empty chart rather than an error.
    assert {
        q["MetricStat"]["Metric"]["MetricName"]: q["MetricStat"]["Unit"]
        for q in queries
    } == {name: unit for name, unit, _suffix in rds_cli._CW_METRICS}


def test_metrics_reads_each_statistic_from_its_own_series(monkeypatch, wide) -> None:
    """m0 is CPUUtilization: latest/average come from the averages, min/max not.

    A summary that took ``min(avgs)`` would report 1.0 below instead of 0.5, and
    nothing in the rendered table would look wrong.
    """
    cw = _StatCw(
        by_id={
            "m0_avg": [1.0, 2.0, 3.0],
            "m0_min": [0.5, 4.0],
            "m0_max": [9.0, 1.0],
        }
    )
    _patch_session(monkeypatch, _Session(cloudwatch=cw))

    result = runner.invoke(rds_cli.app, ["metrics", "-i", "db-1", "--json"])

    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout)[0] == {
        "metric": "CPUUtilization",
        "latest": 3.0,
        "average": 2.0,
        "min": 0.5,
        "max": 9.0,
    }


def test_metrics_falls_back_to_the_averages_when_min_max_come_back_empty(
    monkeypatch, wide
) -> None:
    """CloudWatch can answer a Minimum query with no values at all.

    The fallback keeps the row honest — min/max of what we do have — instead of
    ``min([])`` raising and taking the whole command down.
    """
    cw = _StatCw(by_id={"m0_avg": [4.0, 2.0, 6.0], "m0_min": [], "m0_max": []})
    _patch_session(monkeypatch, _Session(cloudwatch=cw))

    result = runner.invoke(rds_cli.app, ["metrics", "-i", "db-1", "--json"])

    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout)[0] == {
        "metric": "CPUUtilization",
        "latest": 6.0,
        "average": 4.0,
        "min": 2.0,
        "max": 6.0,
    }


def test_metrics_json_omits_the_stats_for_a_metric_with_no_data(
    monkeypatch, wide
) -> None:
    """An absent metric is ``{"metric": name}`` — not a zero, which would lie."""
    _patch_session(monkeypatch, _Session(cloudwatch=_StatCw(default=[])))

    result = runner.invoke(rds_cli.app, ["metrics", "-i", "db-1", "--json"])

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload == [{"metric": name} for name, _unit, _suffix in rds_cli._CW_METRICS]


def test_metrics_table_says_no_data_instead_of_a_zero_row(monkeypatch, wide) -> None:
    _patch_session(monkeypatch, _Session(cloudwatch=_StatCw(default=[])))

    result = runner.invoke(rds_cli.app, ["metrics", "-i", "db-1"])

    assert result.exit_code == 0, result.stdout
    assert result.stdout.count("no data") == len(rds_cli._CW_METRICS)
    assert "0.0%" not in result.stdout


def test_metrics_footer_reports_the_window_and_period_it_used(
    monkeypatch, wide
) -> None:
    _patch_session(monkeypatch, _Session(cloudwatch=_StatCw()))

    result = runner.invoke(
        rds_cli.app, ["metrics", "-i", "db-1", "--hours", "3", "--period", "60"]
    )

    assert result.exit_code == 0, result.stdout
    assert "Window: last 3.0h" in result.stdout
    assert "Period: 60s" in result.stdout


# ── resolving the instance ───────────────────────────────────────────────────


def test_metrics_without_an_instance_names_both_ways_to_give_one(
    monkeypatch, wide
) -> None:
    monkeypatch.delenv("DP_RDS_INSTANCE", raising=False)

    result = runner.invoke(rds_cli.app, ["metrics"])

    # Bare exit, no typed error: 1 is what this site has always returned, and
    # the exit-code contract deliberately does not renumber those.
    assert result.exit_code == 1
    assert "--instance" in result.stdout
    assert "DP_RDS_INSTANCE" in result.stdout


def test_metrics_falls_back_to_the_env_instance(monkeypatch, wide) -> None:
    monkeypatch.setenv("DP_RDS_INSTANCE", "db-from-env")
    _patch_session(monkeypatch, _Session(cloudwatch=_StatCw()))

    result = runner.invoke(rds_cli.app, ["metrics"])

    assert result.exit_code == 0, result.stdout
    assert "db-from-env" in result.stdout


# ── value formatting and thresholds ──────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (512, "512.0 B"),
        (1024, "1.0 KB"),
        (1024**2, "1.0 MB"),
        (1024**3, "1.0 GB"),
        (1024**4, "1.0 TB"),
        (1024**5, "1.0 PB"),
        (-2048, "-2.0 KB"),
    ],
)
def test_human_bytes_scales_and_keeps_the_sign(value: float, expected: str) -> None:
    assert rds_cli._human_bytes(value) == expected


@pytest.mark.parametrize(
    ("metric", "value", "expected"),
    [
        ("CPUUtilization", 12.345, "12.3%"),
        ("EBSByteBalance%", 99.94, "99.9%"),
        ("FreeableMemory", 1024**3, "1.0 GB"),
        ("FreeStorageSpace", 5 * 1024**3, "5.0 GB"),
        # Connections are whole things: "12.7 connections" is not a state a
        # database can be in.
        ("DatabaseConnections", 12.7, "12"),
        ("BurstBalance", 1.239, "1.24"),
    ],
)
def test_format_value_per_metric_family(
    metric: str, value: float, expected: str
) -> None:
    assert rds_cli._format_value(metric, value) == expected


@pytest.mark.parametrize(
    ("metric", "value", "expected"),
    [
        ("CPUUtilization", 10.0, "[green]v[/green]"),
        ("CPUUtilization", 70.0, "[yellow]v[/yellow]"),
        ("CPUUtilization", 90.0, "[bold red]v[/bold red]"),
        ("FreeableMemory", 2 * 1024**3, "[green]v[/green]"),
        ("FreeableMemory", 900 * 1024**2, "[yellow]v[/yellow]"),
        ("FreeableMemory", 100 * 1024**2, "[bold red]v[/bold red]"),
        ("EBSIOBalance%", 10.0, "[bold red]v[/bold red]"),
        # No threshold defined: the value is returned untouched rather than
        # coloured green, which would claim it had been judged.
        ("BurstBalance", 0.0, "v"),
    ],
)
def test_colorize_marks_breaches_at_the_boundary(
    metric: str, value: float, expected: str
) -> None:
    assert rds_cli._colorize(metric, value, "v") == expected


def test_friendly_name_passes_unknown_metrics_through(monkeypatch) -> None:
    assert rds_cli._friendly_name("CPUUtilization") == "CPU Utilization"
    assert rds_cli._friendly_name("BurstBalance") == "BurstBalance"


# ── --verbose: what boto3 was actually asked for ─────────────────────────────
# The trace is metadata only, and it goes to stderr: `dp ... --json --verbose |
# jq` has to stay valid, which is the one property a tracer can break for good.


@pytest.fixture
def no_ambient_region(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop a developer's AWS_REGION from leaking into a trace assertion.

    conftest clears DP_AWS_*, but ``default_region()`` also reads AWS_REGION and
    AWS_DEFAULT_REGION, which nothing else in the suite depends on.
    """
    for var in ("AWS_REGION", "AWS_DEFAULT_REGION"):
        monkeypatch.delenv(var, raising=False)


def test_verbose_traces_the_batched_cloudwatch_call(
    monkeypatch, wide, no_ambient_region
) -> None:
    monkeypatch.setenv("DP_AWS_PROFILE_ALIASES", "prod=AdminAccess-Prod")
    _patch_session(monkeypatch, _Session(cloudwatch=_StatCw()))

    with verbose():
        result = runner.invoke(
            rds_cli.app,
            [
                "metrics",
                "-i",
                "db-1",
                "-p",
                "prod",
                "-r",
                "eu-central-1",
                "--hours",
                "3",
                "--period",
                "60",
            ],
        )

    assert result.exit_code == 0, result.stdout
    # The profile is the resolved one: an alias in the trace would send the
    # reader to the wrong account.
    assert (
        "[dp:aws] cloudwatch.get_metric_data | profile=AdminAccess-Prod | "
        "region=eu-central-1 | instance=db-1 | metrics=6 | window=3.0h | "
        "period=60s" in result.stderr
    )


def test_verbose_says_unset_rather_than_guessing_the_region(
    monkeypatch, wide, no_ambient_region
) -> None:
    """We did not pass a region; boto3 resolved one from the profile.

    Printing a blank there reads as a broken tracer, and printing a guess would
    be worse: the trace can only attest to what we sent.
    """
    _patch_session(monkeypatch, _Session(rds=_Rds([{"DBInstances": []}])))

    with verbose():
        result = runner.invoke(rds_cli.app, ["list"])

    assert result.exit_code == 0, result.stdout
    assert (
        "[dp:aws] rds.describe_db_instances | profile=default | region=unset"
        in result.stderr
    )


def test_verbose_keeps_stdout_machine_readable(monkeypatch, wide) -> None:
    _patch_session(monkeypatch, _Session(cloudwatch=_StatCw()))

    with verbose():
        result = runner.invoke(rds_cli.app, ["metrics", "-i", "db-1", "--json"])

    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout)[0]["metric"] == "CPUUtilization"
    assert "[dp:aws]" in result.stderr


def test_nothing_is_traced_without_verbose(monkeypatch, wide) -> None:
    _patch_session(monkeypatch, _Session(cloudwatch=_StatCw()))

    result = runner.invoke(rds_cli.app, ["metrics", "-i", "db-1"])

    assert result.exit_code == 0, result.stdout
    assert result.stderr == ""


def test_verbose_traces_one_line_per_plotted_metric(monkeypatch, wide) -> None:
    """``plot`` calls per metric, unlike ``metrics``, so it traces per metric.

    That is how one denied or slow metric is told apart from all six failing.
    """
    _patch_session(monkeypatch, _Session(cloudwatch=_StatsCw()))

    with verbose():
        result = runner.invoke(
            rds_cli.app, ["plot", "-i", "db-1", "-m", "cpu", "-m", "memory"]
        )

    assert result.exit_code == 0, result.stdout
    traced = [
        line for line in result.stderr.splitlines() if "get_metric_statistics" in line
    ]
    assert len(traced) == 2
    assert "metric=CPUUtilization" in traced[0]
    assert "metric=FreeableMemory" in traced[1]


# ── the chart ────────────────────────────────────────────────────────────────
# A chart is the part of this module that resists assertion: 200 lines that end
# in box-drawing characters. So the tests here aim at the three things around it
# that do not — which metrics were fetched, the numbers the axis and label
# arithmetic produced, and that the render survives every shape of series — and
# leave the pixels alone.


@pytest.fixture
def fixed_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the chart's size.

    ``plot`` sizes itself from ``shutil.get_terminal_size``, which prefers
    COLUMNS/LINES. conftest sets COLUMNS but not LINES, so without this the chart
    height — and therefore which labels fit inside it — is whatever the
    developer's window happens to be.
    """
    monkeypatch.setenv("COLUMNS", "140")
    monkeypatch.setenv("LINES", "42")


def test_plot_renders_all_six_metrics_and_reports_the_window(
    monkeypatch, wide, fixed_terminal
) -> None:
    """The end-to-end render: six subplots in a grid, one fetch each."""
    cw = _StatsCw(
        {
            "CPUUtilization": [12.0, 40.0, 88.0],
            "FreeableMemory": [1 * 1024**3, 2 * 1024**3, 3 * 1024**3],
            "FreeStorageSpace": [40 * 1024**3] * 3,
            "DatabaseConnections": [0.0, 120.0, 60.0],
            "EBSByteBalance%": [100.0, 60.0, 99.0],
            "EBSIOBalance%": [55.5, 60.25, 58.0],
        }
    )
    _patch_session(monkeypatch, _Session(cloudwatch=cw))

    result = runner.invoke(rds_cli.app, ["plot", "-i", "db-1"])

    assert result.exit_code == 0, result.stdout
    assert [call["MetricName"] for call in cw.calls] == [
        name for name, _unit, _suffix in rds_cli._CW_METRICS
    ]
    # Something was actually drawn, rather than the command exiting early.
    assert "┤" in _plain(result.stdout)
    assert "Window: last 6.0h" in result.stdout
    assert "Database: db-1" in result.stdout


def test_plot_labels_the_extremes_in_the_metrics_own_unit(
    monkeypatch, wide, fixed_terminal
) -> None:
    """The one assertion that reaches inside the chart, and it earns its place.

    ``_format_plot_value`` is a closure inside ``plot``, so the MIN/MAX labels are
    the only place its arithmetic is observable: a percentage rounded to one
    decimal, bytes divided down to GB. Both peaks sit late in the series here on
    purpose — a peak at the very first datapoint has its label right-aligned onto
    the y-axis and clipped, which is cosmetic and not what this pins.
    """
    _patch_session(
        monkeypatch,
        _Session(
            cloudwatch=_StatsCw(
                {
                    "CPUUtilization": [12.0, 40.0, 88.0],
                    "FreeableMemory": [1 * 1024**3, 2 * 1024**3, 3 * 1024**3],
                }
            )
        ),
    )

    result = runner.invoke(rds_cli.app, ["plot", "-i", "db-1", "-m", "cpu"])
    assert result.exit_code == 0, result.stdout
    plain = _plain(result.stdout)
    assert "MIN 12.0%" in plain
    assert "MAX 88.0%" in plain

    result = runner.invoke(rds_cli.app, ["plot", "-i", "db-1", "-m", "memory"])
    assert result.exit_code == 0, result.stdout
    plain = _plain(result.stdout)
    assert "MIN 1.0 GB" in plain
    assert "MAX 3.0 GB" in plain


def test_plot_fetches_only_the_requested_metrics(
    monkeypatch, wide, fixed_terminal
) -> None:
    cw = _StatsCw()
    _patch_session(monkeypatch, _Session(cloudwatch=cw))

    result = runner.invoke(
        rds_cli.app, ["plot", "-i", "db-1", "-m", "cpu", "-m", "EBS-Byte"]
    )

    assert result.exit_code == 0, result.stdout
    # Aliases are case- and whitespace-insensitive, and the order follows
    # _CW_METRICS rather than the order they were typed.
    assert [call["MetricName"] for call in cw.calls] == [
        "CPUUtilization",
        "EBSByteBalance%",
    ]


def test_plot_x_axis_runs_forwards_whatever_order_cloudwatch_answered_in(
    monkeypatch, wide, fixed_terminal
) -> None:
    """CloudWatch may return datapoints newest-first; a time axis may not.

    The two labels are read off the rendered axis line, because that is the only
    place the sort is observable — plotted against unsorted timestamps the chart
    still draws, it just runs backwards.
    """
    _patch_session(
        monkeypatch,
        _Session(
            cloudwatch=_StatsCw(
                {"DatabaseConnections": [10.0, 20.0]}, newest_first=True
            )
        ),
    )

    result = runner.invoke(rds_cli.app, ["plot", "-i", "db-1", "-m", "connections"])

    assert result.exit_code == 0, result.stdout
    axis = next(line for line in _plain(result.stdout).splitlines() if "09:15" in line)
    assert axis.index("09:15") < axis.index("09:20")


def test_plot_x_labels_carry_the_date_once_the_window_crosses_midnight(
    monkeypatch, wide, fixed_terminal
) -> None:
    """ "09:15" is ambiguous across a two-day window, so the date joins it."""
    _patch_session(
        monkeypatch,
        _Session(
            cloudwatch=_StatsCw(
                {"DatabaseConnections": [10.0, 20.0]}, step=timedelta(hours=18)
            )
        ),
    )

    result = runner.invoke(
        rds_cli.app, ["plot", "-i", "db-1", "-m", "connections", "--hours", "48"]
    )

    assert result.exit_code == 0, result.stdout
    plain = _plain(result.stdout)
    assert "07/27 09:15" in plain
    assert "07/28 03:15" in plain


@pytest.mark.parametrize(
    ("flag", "values"),
    [
        ("cpu", [12.0, 40.0, 88.0]),
        ("cpu", [0.0, 0.0, 0.0]),
        ("cpu", [140.0, 99.0, 100.0]),
        ("cpu", [99.5, 100.0]),
        ("memory", [int(0.4 * 1024**3), int(0.9 * 1024**3)]),
        ("memory", [int(0.02 * 1024**3), 1024**3]),
        ("memory", [0.0, 0.0]),
        ("memory", [3 * 1024**3] * 3),
        ("storage", [40 * 1024**3, 41 * 1024**3, 39 * 1024**3]),
        ("connections", [0.0, 220.0, 40.0]),
        ("connections", [5.0]),
        ("connections", [0.0, 0.0]),
        ("ebs-byte", [100.0, 20.0]),
        ("ebs-io", [58.25, 58.5]),
    ],
    ids=[
        "percent-varying",
        "percent-flat-at-zero",
        "percent-above-100",
        "percent-narrow-span",
        "gb-below-one",
        "gb-padded-below-zero",
        "gb-flat-at-zero",
        "gb-flat",
        "gb-wide-span",
        "count-touching-zero",
        "count-single-datapoint",
        "count-all-zero",
        "percent-peak-first",
        "percent-tiny-span",
    ],
)
def test_plot_survives_every_axis_shape(
    flag: str, values: list[float], monkeypatch, wide, fixed_terminal
) -> None:
    """Each case walks a different branch of the tick/pad/extrema arithmetic.

    Flat series skip the extrema labels entirely (min and max are the same
    datapoint), a sub-GB series pads its lower bound below zero and has to be
    clamped, and a series above 100% is capped — all of it inside closures that
    only a real render reaches. The assertion is deliberately just "it drew
    something and exited 0": pinning the geometry would pin plotext's, not ours.
    """
    _patch_session(monkeypatch, _Session(cloudwatch=_StatsCw({}, default=list(values))))

    result = runner.invoke(rds_cli.app, ["plot", "-i", "db-1", "-m", flag])

    assert result.exit_code == 0, result.stdout
    assert "┤" in _plain(result.stdout)


def test_plot_leaves_the_extremes_unlabelled_for_a_flat_series(
    monkeypatch, wide, fixed_terminal
) -> None:
    """One value repeated has no interesting extreme; labelling it is noise."""
    _patch_session(
        monkeypatch, _Session(cloudwatch=_StatsCw({}, default=[42.0, 42.0, 42.0]))
    )

    result = runner.invoke(rds_cli.app, ["plot", "-i", "db-1", "-m", "cpu"])

    assert result.exit_code == 0, result.stdout
    plain = _plain(result.stdout)
    assert "MIN" not in plain
    assert "MAX" not in plain


def test_plot_reports_a_failed_metric_and_still_draws_the_rest(
    monkeypatch, wide, fixed_terminal
) -> None:
    """One metric denied must not cost the other five their chart."""
    cw = _StatsCw({"CPUUtilization": RuntimeError("throttled [/x] [bold]hard[/bold]")})
    _patch_session(monkeypatch, _Session(cloudwatch=cw))

    result = runner.invoke(rds_cli.app, ["plot", "-i", "db-1"])

    assert result.exit_code == 0, result.stdout
    assert len(cw.calls) == len(rds_cli._CW_METRICS)
    # The driver's message is data: unescaped, "[/x]" would raise MarkupError
    # and lose both the error and the charts.
    assert "Error fetching CPUUtilization" in result.stdout
    assert "throttled [/x] [bold]hard[/bold]" in result.stdout
    assert "┤" in _plain(result.stdout)


def test_plot_names_the_metric_that_returned_nothing(
    monkeypatch, wide, fixed_terminal
) -> None:
    _patch_session(monkeypatch, _Session(cloudwatch=_StatsCw({"FreeableMemory": []})))

    result = runner.invoke(rds_cli.app, ["plot", "-i", "db-1"])

    assert result.exit_code == 0, result.stdout
    # The friendly name, because that is what the chart would have been titled.
    assert "No data for Freeable Memory (RAM)" in result.stdout


def test_plot_with_nothing_to_draw_says_so_and_succeeds(
    monkeypatch, wide, fixed_terminal
) -> None:
    """An idle instance is not a failure, so an empty window exits 0."""
    _patch_session(monkeypatch, _Session(cloudwatch=_StatsCw({}, default=[])))

    result = runner.invoke(rds_cli.app, ["plot", "-i", "db-1"])

    assert result.exit_code == 0, result.stdout
    assert "No metric data to plot." in result.stdout
    assert "┤" not in _plain(result.stdout)
