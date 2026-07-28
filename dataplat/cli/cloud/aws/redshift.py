"""AWS Redshift Serverless monitoring commands."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import typer
from botocore.exceptions import ClientError
from rich.console import Console

from dataplat.cli._options import JsonOption
from dataplat.cli._render import cell, esc
from dataplat.cli.cloud.aws._common import (
    cli_session,
    default_region,
    effective_profile,
    make_table,
    profile_option,
    region_option,
    trace_aws,
)

app = typer.Typer(
    name="redshift",
    help="Monitor AWS Redshift Serverless metrics",
    no_args_is_help=True,
)

console = Console()


def _get_session(profile: str | None, region: str | None):
    """Return a boto3 Session with shared AWS auth handling."""
    return cli_session(console, profile, region)


def _human_value(value: float | None, unit: str | None, metric_name: str) -> str:
    if value is None:
        return "—"
    if unit in {"Percent"}:
        return f"{value:.2f}%"
    if unit in {"Bytes"}:
        gb = value / 1024**3
        return f"{gb:.2f} GB"
    if unit in {"Megabytes"}:
        gb = value / 1024
        return f"{gb:.2f} GB"
    if unit in {"Milliseconds", "Microseconds"}:
        return f"{value:.2f} {unit}"
    if unit in {"Seconds"}:
        return f"{value:.2f}s"
    if metric_name.endswith("Count"):
        return str(int(round(value)))
    return f"{value:.2f}"


_make_table = make_table

# CloudWatch units per metric (get_metric_data does not echo units back).
_METRIC_UNITS: dict[str, str] = {
    "ComputeSeconds": "Seconds",
    "QueryDuration": "Microseconds",
    "DataStorage": "Megabytes",
}


def _list_workgroups(client, explicit_workgroup: str | None) -> list[dict]:
    groups: list[dict] = []
    token: str | None = None
    while True:
        kwargs: dict[str, object] = {"maxResults": 100}
        if token:
            kwargs["nextToken"] = token
        resp = client.list_workgroups(**kwargs)
        groups.extend(resp.get("workgroups", []))
        token = resp.get("nextToken")
        if not token:
            break

    if explicit_workgroup:
        groups = [g for g in groups if g.get("workgroupName") == explicit_workgroup]
    return groups


def _discover_metric_dims(
    cw, metric_name: str, base_dims: list[dict]
) -> list[dict] | None:
    """Find a dimension set for a metric that matches the requested scope."""
    paginator = cw.get_paginator("list_metrics")
    candidates: list[list[dict]] = []

    for page in paginator.paginate(
        Namespace="AWS/Redshift-Serverless",
        MetricName=metric_name,
        Dimensions=base_dims,
    ):
        for metric in page.get("Metrics", []):
            dims = metric.get("Dimensions", [])
            dim_map = {d["Name"]: d["Value"] for d in dims}
            matches = all(dim_map.get(d["Name"]) == d["Value"] for d in base_dims)
            if matches:
                candidates.append(dims)

    if not candidates:
        return None

    # Prefer the least-specific dimension set; it usually yields stable series.
    candidates.sort(key=len)
    return candidates[0]


def _batch_metric_summaries(
    cw,
    scoped: list[tuple[str, str, list[dict]]],
    start: datetime,
    end: datetime,
    period: int,
) -> list[tuple[str, str, dict[str, float] | None]]:
    """One get_metric_data call for every ``(scope, metric, dims)`` triple."""
    queries: list[dict] = []
    stats = (("avg", "Average"), ("min", "Minimum"), ("max", "Maximum"))
    for i, (_scope, metric_name, dims) in enumerate(scoped):
        for stat_key, stat in stats:
            queries.append(
                {
                    "Id": f"m{i}_{stat_key}",
                    "MetricStat": {
                        "Metric": {
                            "Namespace": "AWS/Redshift-Serverless",
                            "MetricName": metric_name,
                            "Dimensions": dims,
                        },
                        "Period": period,
                        "Stat": stat,
                    },
                    "ReturnData": True,
                }
            )
    if not queries:
        return []
    resp = cw.get_metric_data(
        MetricDataQueries=queries,
        StartTime=start,
        EndTime=end,
        ScanBy="TimestampAscending",
    )
    by_id: dict[str, list[float]] = {
        r["Id"]: r.get("Values", []) for r in resp.get("MetricDataResults", [])
    }

    out: list[tuple[str, str, dict[str, float] | None]] = []
    for i, (scope, metric_name, _dims) in enumerate(scoped):
        avgs = by_id.get(f"m{i}_avg", [])
        if not avgs:
            out.append((scope, metric_name, None))
            continue
        mins = by_id.get(f"m{i}_min", []) or avgs
        maxs = by_id.get(f"m{i}_max", []) or avgs
        out.append(
            (
                scope,
                metric_name,
                {
                    "latest": avgs[-1],
                    "average": sum(avgs) / len(avgs),
                    "min": min(mins),
                    "max": max(maxs),
                },
            )
        )
    return out


# shared CLI options
ProfileOption = profile_option()
RegionOption = region_option()


# commands
@app.command("metrics")
def metrics(
    profile: str | None = ProfileOption,
    region: str | None = RegionOption,
    workgroup: str | None = typer.Option(
        None,
        "--workgroup",
        "-w",
        help="Redshift Serverless workgroup name. Defaults to all workgroups.",
    ),
    hours: float = typer.Option(
        1.0,
        "--hours",
        help="Look-back window in hours.",
    ),
    period: int = typer.Option(
        300,
        "--period",
        help="CloudWatch aggregation period in seconds.",
    ),
    as_json: bool = JsonOption,
) -> None:
    """Show key Redshift Serverless CloudWatch metrics."""
    # Workgroup + namespace focused metrics with stable operational value.
    workgroup_metrics = [
        "ComputeCapacity",
        "ComputeSeconds",
        "DatabaseConnections",
        "QueriesQueued",
        "QueriesRunning",
        "QueriesCompletedPerSecond",
        "QueryDuration",
    ]
    namespace_metrics = [
        "DataStorage",
        "TotalTableCount",
    ]

    session = _get_session(profile, region)
    rs = session.client("redshift-serverless")
    cw = session.client("cloudwatch")

    now = datetime.now(UTC)
    start = now - timedelta(hours=hours)

    try:
        trace_aws(
            "redshift-serverless",
            "list_workgroups",
            profile=profile,
            region=region,
            workgroup=workgroup or "(all)",
        )
        with console.status(
            "[bold blue]Fetching Redshift Serverless metrics…[/bold blue]"
        ):
            workgroups = _list_workgroups(rs, workgroup)

        if not workgroups:
            message = (
                f"No workgroup named '{esc(workgroup)}' found."
                if workgroup
                else "No Redshift Serverless workgroups found."
            )
            console.print(f"[yellow]{message}[/yellow]")
            raise typer.Exit(code=1 if workgroup else 0)

        # Discover dimension sets per metric, then batch every summary in a
        # single get_metric_data call.
        scoped: list[tuple[str, str, list[dict]]] = []
        with console.status("[bold blue]Collecting CloudWatch datapoints…[/bold blue]"):
            for wg in sorted(workgroups, key=lambda g: g.get("workgroupName", "")):
                wg_name = wg.get("workgroupName")
                ns_name = wg.get("namespaceName")
                if not wg_name:
                    continue
                for metric_name in workgroup_metrics:
                    # Dimension discovery is a list_metrics call per metric, and
                    # a metric that silently has no matching dimension set is
                    # the usual reason a row is missing from the table.
                    trace_aws(
                        "cloudwatch",
                        "list_metrics",
                        profile=profile,
                        region=region,
                        metric=metric_name,
                        workgroup=wg_name,
                    )
                    dims = _discover_metric_dims(
                        cw, metric_name, [{"Name": "Workgroup", "Value": wg_name}]
                    )
                    if dims:
                        scoped.append((f"workgroup:{wg_name}", metric_name, dims))
                if ns_name:
                    for metric_name in namespace_metrics:
                        trace_aws(
                            "cloudwatch",
                            "list_metrics",
                            profile=profile,
                            region=region,
                            metric=metric_name,
                            namespace=ns_name,
                        )
                        dims = _discover_metric_dims(
                            cw,
                            metric_name,
                            [{"Name": "Namespace", "Value": ns_name}],
                        )
                        if dims:
                            scoped.append((f"namespace:{ns_name}", metric_name, dims))

            trace_aws(
                "cloudwatch",
                "get_metric_data",
                profile=profile,
                region=region,
                series=len(scoped),
                window=f"{hours}h",
                period=f"{period}s",
            )
            summaries = _batch_metric_summaries(cw, scoped, start, now, period)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "ClientError")
        msg = exc.response.get("Error", {}).get("Message", str(exc))
        console.print(f"[red]{esc(code)} while fetching metrics: {esc(msg)}[/red]")
        raise typer.Exit(code=1)

    rows = [(scope, name, s) for scope, name, s in summaries if s is not None]

    if as_json:
        payload = [
            {"scope": scope, "metric": name, **summary} for scope, name, summary in rows
        ]
        typer.echo(json.dumps(payload, indent=2))
        return

    if not rows:
        console.print(
            "[yellow]No datapoints found for selected "
            "Redshift Serverless metrics.[/yellow]"
        )
        raise typer.Exit()

    table = _make_table("Redshift Serverless Metrics")
    table.add_column("Scope", style="bold cyan", min_width=24, no_wrap=True)
    table.add_column("Metric", style="magenta", min_width=22)
    table.add_column("Latest", justify="right", style="bright_white")
    table.add_column("Average", justify="right", style="bright_white")
    table.add_column("Min", justify="right", style="bright_white")
    table.add_column("Max", justify="right", style="bright_white")

    for scope, metric_name, summary in rows:
        unit = _METRIC_UNITS.get(metric_name)
        table.add_row(
            # The scope carries workgroup/namespace names straight from the API.
            cell(scope),
            metric_name,
            _human_value(summary["latest"], unit, metric_name),
            _human_value(summary["average"], unit, metric_name),
            _human_value(summary["min"], unit, metric_name),
            _human_value(summary["max"], unit, metric_name),
        )

    console.print()
    console.print(table)
    # Report what the session was actually built with: when --profile is an
    # alias, the flag holds the shorthand, not the profile boto3 received.
    console.print(
        f"\n[dim]Window: last {hours}h  ·  Period: {period}s  ·  "
        f"Profile: {esc(effective_profile(profile))}  ·  "
        f"Region: {esc(region or default_region())}[/dim]"
    )
