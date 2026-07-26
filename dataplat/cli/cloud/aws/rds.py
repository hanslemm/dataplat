"""AWS RDS monitoring commands."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from math import ceil
from shutil import get_terminal_size

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
)

app = typer.Typer(
    name="rds",
    help="Monitor AWS RDS instances",
    no_args_is_help=True,
)

console = Console()

# defaults

# CloudWatch metric definitions: (MetricName, Unit, display suffix)
_CW_METRICS: list[tuple[str, str, str]] = [
    ("CPUUtilization", "Percent", "%"),
    ("FreeableMemory", "Bytes", "bytes"),
    ("FreeStorageSpace", "Bytes", "bytes"),
    ("DatabaseConnections", "Count", ""),
    ("EBSByteBalance%", "Percent", "%"),
    ("EBSIOBalance%", "Percent", "%"),
]

# Thresholds for colour-coding (metric_name -> (warn_func, crit_func))
# Each function receives the metric value and returns True when the threshold
# is breached.  "None" means no threshold is defined for that level.
_THRESHOLDS: dict[
    str, tuple[Callable[[float], bool] | None, Callable[[float], bool] | None]
] = {
    "CPUUtilization": (lambda v: v >= 70, lambda v: v >= 90),
    "FreeableMemory": (lambda v: v < 1 * 1024**3, lambda v: v < 512 * 1024**2),
    "FreeStorageSpace": (lambda v: v < 20 * 1024**3, lambda v: v < 5 * 1024**3),
    "DatabaseConnections": (lambda v: v >= 200, lambda v: v >= 400),
    "EBSByteBalance%": (lambda v: v < 40, lambda v: v < 20),
    "EBSIOBalance%": (lambda v: v < 40, lambda v: v < 20),
}


def _get_session(profile: str | None, region: str | None):
    """Return a boto3 session with shared AWS auth handling."""
    return cli_session(console, profile, region)


def _human_bytes(n: float) -> str:
    """Return a human-readable byte string."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def _format_value(metric_name: str, value: float) -> str:
    """Format a raw metric value for display."""
    if "Memory" in metric_name or "Storage" in metric_name:
        return _human_bytes(value)
    if "Percent" in metric_name or metric_name.endswith("%"):
        return f"{value:.1f}%"
    if metric_name == "CPUUtilization":
        return f"{value:.1f}%"
    if metric_name == "DatabaseConnections":
        return str(int(value))
    return f"{value:.2f}"


def _colorize(metric_name: str, value: float, text: str) -> str:
    """Wrap *text* in a Rich colour tag based on threshold breaches."""
    thresholds = _THRESHOLDS.get(metric_name)
    if not thresholds:
        return text
    warn_fn, crit_fn = thresholds
    if crit_fn and crit_fn(value):
        return f"[bold red]{text}[/bold red]"
    if warn_fn and warn_fn(value):
        return f"[yellow]{text}[/yellow]"
    return f"[green]{text}[/green]"


def _friendly_name(metric_name: str) -> str:
    """Convert CloudWatch metric names to friendlier labels."""
    mapping = {
        "CPUUtilization": "CPU Utilization",
        "FreeableMemory": "Freeable Memory (RAM)",
        "FreeStorageSpace": "Free Storage Space (Disk)",
        "DatabaseConnections": "Database Connections",
        "EBSByteBalance%": "EBS Byte Balance",
        "EBSIOBalance%": "EBS IO Balance",
    }
    return mapping.get(metric_name, metric_name)


_make_table = make_table


# shared CLI options
ProfileOption = profile_option()
RegionOption = region_option()
InstanceOption = typer.Option(
    None,
    "--instance",
    "-i",
    help="RDS DB instance identifier. Defaults to DP_RDS_INSTANCE.",
)


def resolve_instance(instance: str | None) -> str:
    """Resolve the instance flag against DP_RDS_INSTANCE, or exit."""
    resolved = instance or os.getenv("DP_RDS_INSTANCE")
    if not resolved:
        console.print(
            "[red]Error: no RDS instance given. Pass --instance or set "
            "DP_RDS_INSTANCE.[/red]"
        )
        raise typer.Exit(code=1)
    return resolved


def _fetch_metric_summaries(
    cw,
    instance: str,
    start: datetime,
    end: datetime,
    period: int,
) -> list[tuple[str, dict[str, float] | None]]:
    """Fetch every metric's summary in a single get_metric_data call."""
    queries: list[dict] = []
    stats = (("avg", "Average"), ("min", "Minimum"), ("max", "Maximum"))
    for i, (metric_name, unit, _suffix) in enumerate(_CW_METRICS):
        for stat_key, stat in stats:
            queries.append(
                {
                    "Id": f"m{i}_{stat_key}",
                    "MetricStat": {
                        "Metric": {
                            "Namespace": "AWS/RDS",
                            "MetricName": metric_name,
                            "Dimensions": [
                                {
                                    "Name": "DBInstanceIdentifier",
                                    "Value": instance,
                                }
                            ],
                        },
                        "Period": period,
                        "Stat": stat,
                        "Unit": unit,
                    },
                    "ReturnData": True,
                }
            )
    resp = cw.get_metric_data(
        MetricDataQueries=queries,
        StartTime=start,
        EndTime=end,
        ScanBy="TimestampAscending",
    )
    by_id: dict[str, list[float]] = {
        r["Id"]: r.get("Values", []) for r in resp.get("MetricDataResults", [])
    }

    summaries: list[tuple[str, dict[str, float] | None]] = []
    for i, (metric_name, _unit, _suffix) in enumerate(_CW_METRICS):
        avgs = by_id.get(f"m{i}_avg", [])
        if not avgs:
            summaries.append((metric_name, None))
            continue
        mins = by_id.get(f"m{i}_min", []) or avgs
        maxs = by_id.get(f"m{i}_max", []) or avgs
        summaries.append(
            (
                metric_name,
                {
                    "latest": avgs[-1],
                    "average": sum(avgs) / len(avgs),
                    "min": min(mins),
                    "max": max(maxs),
                },
            )
        )
    return summaries


# commands


@app.command("metrics")
def metrics(
    instance: str | None = InstanceOption,
    profile: str | None = ProfileOption,
    region: str | None = RegionOption,
    period: int = typer.Option(
        300,
        "--period",
        help="CloudWatch aggregation period in seconds (default 300 = 5 min).",
    ),
    hours: float = typer.Option(
        1.0,
        "--hours",
        help="Look-back window in hours (default 1).",
    ),
    as_json: bool = JsonOption,
) -> None:
    """Show key RDS metrics for an instance.

    Fetches the latest CloudWatch data points for CPU, RAM, Disk,
    Connections, EBS Byte Balance % and EBS IO Balance % — one batched
    get_metric_data call.
    """
    instance = resolve_instance(instance)
    session = _get_session(profile, region)
    cw = session.client("cloudwatch")

    now = datetime.now(UTC)
    start = now - timedelta(hours=hours)

    try:
        with console.status("[bold blue]Fetching CloudWatch metrics…[/bold blue]"):
            summaries = _fetch_metric_summaries(cw, instance, start, now, period)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "ClientError")
        msg = exc.response.get("Error", {}).get("Message", str(exc))
        console.print(f"[red]{esc(code)} on get_metric_data: {esc(msg)}[/red]")
        raise typer.Exit(code=1)

    if as_json:
        payload = [{"metric": name, **(summary or {})} for name, summary in summaries]
        typer.echo(json.dumps(payload, indent=2))
        return

    table = _make_table(f"RDS Metrics — {esc(instance)}")
    table.add_column("Metric", style="bold cyan", min_width=24)
    table.add_column("Latest", justify="right", min_width=12, style="bright_white")
    table.add_column("Average", justify="right", min_width=12, style="bright_white")
    table.add_column("Min", justify="right", min_width=12, style="bright_white")
    table.add_column("Max", justify="right", min_width=12, style="bright_white")

    for metric_name, summary in summaries:
        if summary is None:
            table.add_row(
                _friendly_name(metric_name),
                "[dim]no data[/dim]",
                "[dim]—[/dim]",
                "[dim]—[/dim]",
                "[dim]—[/dim]",
            )
            continue
        latest = summary["latest"]
        table.add_row(
            _friendly_name(metric_name),
            _colorize(metric_name, latest, _format_value(metric_name, latest)),
            _format_value(metric_name, summary["average"]),
            _format_value(metric_name, summary["min"]),
            _format_value(metric_name, summary["max"]),
        )

    console.print()
    console.print(table)
    console.print(
        f"\n[dim]Window: last {hours}h  ·  Period: {period}s  ·  "
        f"Database: {esc(instance)}[/dim]"
    )


@app.command("plot")
def plot(
    instance: str | None = InstanceOption,
    profile: str | None = ProfileOption,
    region: str | None = RegionOption,
    period: int = typer.Option(
        300,
        "--period",
        help="CloudWatch aggregation period in seconds (default 300 = 5 min).",
    ),
    hours: float = typer.Option(
        6.0,
        "--hours",
        help="Look-back window in hours (default 6).",
    ),
    metric: list[str] | None = typer.Option(
        None,
        "--metric",
        "-m",
        help=(
            "Metric(s) to plot. Can be specified multiple times. "
            "Choices: cpu, memory, storage, connections, ebs-byte, ebs-io. "
            "Defaults to all."
        ),
    ),
) -> None:
    """Plot key RDS metrics over time in the terminal.

    Renders sparkline-style charts for CPU, RAM, Disk, Connections and
    EBS balance metrics using the last N hours of CloudWatch data.
    """
    import plotext as plt

    # Map short names to CW metric names
    _METRIC_ALIASES: dict[str, str] = {
        "cpu": "CPUUtilization",
        "memory": "FreeableMemory",
        "storage": "FreeStorageSpace",
        "connections": "DatabaseConnections",
        "ebs-byte": "EBSByteBalance%",
        "ebs-io": "EBSIOBalance%",
    }

    # Determine which metrics to plot
    if metric:
        selected = []
        for m in metric:
            key = m.lower().strip()
            if key not in _METRIC_ALIASES:
                console.print(
                    f"[red]Unknown metric '{esc(m)}'. "
                    f"Choose from: {', '.join(_METRIC_ALIASES)}[/red]"
                )
                raise typer.Exit(code=1)
            selected.append(_METRIC_ALIASES[key])
        cw_metrics = [row for row in _CW_METRICS if row[0] in selected]
    else:
        cw_metrics = list(_CW_METRICS)

    instance = resolve_instance(instance)
    session = _get_session(profile, region)
    cw = session.client("cloudwatch")

    now = datetime.now(UTC)
    start = now - timedelta(hours=hours)

    series: list[tuple[str, list[datetime], list[float]]] = []

    with console.status("[bold blue]Fetching CloudWatch metrics…[/bold blue]"):
        for metric_name, unit, _suffix in cw_metrics:
            try:
                resp = cw.get_metric_statistics(
                    Namespace="AWS/RDS",
                    MetricName=metric_name,
                    Dimensions=[
                        {"Name": "DBInstanceIdentifier", "Value": instance},
                    ],
                    StartTime=start,
                    EndTime=now,
                    Period=period,
                    Statistics=["Average"],
                    Unit=unit,
                )
            except Exception as exc:
                console.print(f"[red]Error fetching {metric_name}: {esc(exc)}[/red]")
                continue

            datapoints = resp.get("Datapoints", [])
            if not datapoints:
                console.print(
                    f"[yellow]No data for {_friendly_name(metric_name)}[/yellow]"
                )
                continue

            datapoints.sort(key=lambda d: d["Timestamp"])
            timestamps = [d["Timestamp"] for d in datapoints]
            values = [d["Average"] for d in datapoints]
            series.append((metric_name, timestamps, values))

    if not series:
        console.print("[yellow]No metric data to plot.[/yellow]")
        raise typer.Exit()

    def _even_ticks(lower: float, upper: float, count: int) -> list[float]:
        if count <= 1 or lower == upper:
            return [lower]
        step = (upper - lower) / (count - 1)
        return [lower + step * i for i in range(count)]

    def _gb_ticks(values: list[float]) -> tuple[list[float], list[str], float, float]:
        v_min = min(values)
        v_max = max(values)
        if v_min == v_max:
            pad = max(0.5, v_max * 0.05) if v_max != 0 else 0.5
            lower, upper = v_min - pad, v_max + pad
        else:
            pad = (v_max - v_min) * 0.10
            lower, upper = v_min - pad, v_max + pad
        if lower < 0:
            lower = 0.0
        ticks = _even_ticks(lower, upper, 5)
        span = upper - lower
        decimals = 0 if span >= 50 else (1 if span >= 5 else 2)
        labels = [f"{tick:.{decimals}f}" for tick in ticks]
        return ticks, labels, lower, upper

    def _x_ticks(
        timestamps: list[datetime], max_ticks: int = 5
    ) -> tuple[list[float], list[str]]:
        n = len(timestamps)
        if n == 0:
            return [], []
        if n == 1:
            return [0], [timestamps[0].strftime("%H:%M")]

        tick_count = min(max_ticks, n)
        positions = _even_ticks(0.0, float(n - 1), tick_count)

        same_day = timestamps[0].date() == timestamps[-1].date()
        label_fmt = "%H:%M" if same_day else "%m/%d %H:%M"
        labels = [timestamps[round(pos)].strftime(label_fmt) for pos in positions]
        return positions, labels

    def _value_ticks(
        values: list[float], target_ticks: int = 5, clamp_zero: bool = False
    ) -> tuple[list[float], list[str], float, float]:
        v_min = min(values)
        v_max = max(values)
        if v_min == v_max:
            pad = max(1.0, abs(v_max) * 0.1) if v_max != 0 else 1.0
            lower, upper = v_min - pad, v_max + pad
        else:
            pad = (v_max - v_min) * 0.10
            lower, upper = v_min - pad, v_max + pad
        if clamp_zero:
            lower = max(0.0, lower)
        ticks = _even_ticks(lower, upper, max(2, target_ticks))
        span = upper - lower
        decimals = 0 if span >= 50 else (1 if span >= 5 else 2)
        labels = [f"{tick:.{decimals}f}" for tick in ticks]
        return ticks, labels, lower, upper

    def _format_plot_value(metric_name: str, value: float) -> str:
        if metric_name == "CPUUtilization" or metric_name.endswith("%"):
            value = min(value, 100.0)
            return f"{value:.1f}%"
        if "Memory" in metric_name or "Storage" in metric_name:
            return f"{value:.1f} GB"
        if metric_name == "DatabaseConnections":
            return str(int(round(value)))
        return f"{value:.2f}"

    # Render metrics in a 3x2 grid (for the default 6 metrics).
    num = len(series)
    cols = 1 if num == 1 else 2
    rows = ceil(num / cols)
    plt.subplots(rows, cols)
    plt.theme("dark")
    term = get_terminal_size((140, 42))
    plt.plotsize(max(60, term.columns - 2), max(18, term.lines - 6))
    subplot_width = max(24, (term.columns - 2) // cols)
    x_tick_target = max(4, min(8, subplot_width // 12))
    # plotext API changed across versions (`cls` vs `clt`)
    plt.clt()

    for idx, (metric_name, timestamps, values) in enumerate(series, start=1):
        row = (idx - 1) // cols + 1
        col = (idx - 1) % cols + 1
        plt.subplot(row, col)

        # Convert to human-readable values for memory/storage
        display_values = values
        y_label = _friendly_name(metric_name)
        if "Memory" in metric_name or "Storage" in metric_name:
            display_values = [v / 1024**3 for v in values]
            y_label += " (GB)"
        elif metric_name == "CPUUtilization" or metric_name.endswith("%"):
            y_label += " (%)"

        # Use numeric X coordinates and custom tick labels to avoid
        # plotext date parsing requirements (which vary by version).
        x_values = list(range(len(timestamps)))
        tick_positions, tick_labels = _x_ticks(timestamps, max_ticks=x_tick_target)

        # Render as dot chart to emphasize datapoints.
        plt.scatter(x_values, display_values, marker="dot")
        if len(x_values) > 1:
            plt.xlim(x_values[0], x_values[-1])
        plt.xticks(tick_positions, tick_labels)

        y_tick_values: list[float]
        y_tick_labels: list[str]
        y_plot_min = min(display_values)
        y_plot_max = max(display_values)
        if metric_name == "CPUUtilization" or metric_name.endswith("%"):
            capped_values = [min(v, 100.0) for v in display_values]
            y_min = min(capped_values)
            y_max = max(capped_values)
            if y_min == y_max:
                pad = max(0.5, y_max * 0.1 if y_max != 0 else 1.0)
                y_min = max(0.0, y_min - pad)
                y_max = min(100.0, y_max + pad)
            else:
                pad = (y_max - y_min) * 0.10
                y_min = max(0.0, y_min - pad)
                y_max = min(100.0, y_max + pad)
            if y_min >= y_max:
                y_min = max(0.0, y_max - 1.0)
            y_tick_values = _even_ticks(y_min, y_max, 6)
            span = y_max - y_min
            decimals = 0 if span >= 50 else (1 if span >= 5 else 2)
            y_tick_labels = [f"{tick:.{decimals}f}" for tick in y_tick_values]
            y_plot_min, y_plot_max = y_min, y_max
        elif "Memory" in metric_name or "Storage" in metric_name:
            y_ticks, y_labels, y_min, y_max = _gb_ticks(display_values)
            y_tick_values = y_ticks
            y_tick_labels = y_labels
            y_plot_min, y_plot_max = y_min, y_max
        else:
            y_ticks, y_labels, y_min, y_max = _value_ticks(
                display_values,
                target_ticks=5,
                clamp_zero=metric_name == "DatabaseConnections",
            )
            y_tick_values = y_ticks
            y_tick_labels = y_labels
            y_plot_min, y_plot_max = y_min, y_max

        # Highlight extrema per chart.
        min_idx = min(range(len(display_values)), key=display_values.__getitem__)
        max_idx = max(range(len(display_values)), key=display_values.__getitem__)
        min_x, min_y = x_values[min_idx], display_values[min_idx]
        max_x, max_y = x_values[max_idx], display_values[max_idx]

        y_axis_lower = y_plot_min
        y_axis_upper = y_plot_max
        if min_idx != max_idx:
            y_span = max(y_plot_max - y_plot_min, 1e-9)
            y_offset = max(
                y_span * 0.045,
                (
                    1.0
                    if metric_name == "CPUUtilization" or metric_name.endswith("%")
                    else 0.1
                ),
            )
            x_offset = max(1, (len(x_values) - 1) // 28)
            x_mid = (x_values[0] + x_values[-1]) / 2

            # Prefer labels away from points and keep them inside the plot by
            # choosing side-aware horizontal alignment.
            if max_x >= x_mid:
                max_label_x = max(max_x - x_offset, x_values[0])
                max_align = "right"
            else:
                max_label_x = min(max_x + x_offset, x_values[-1])
                max_align = "left"

            if min_x >= x_mid:
                min_label_x = max(min_x - x_offset, x_values[0])
                min_align = "right"
            else:
                min_label_x = min(min_x + x_offset, x_values[-1])
                min_align = "left"

            # Always keep MAX above and MIN below their markers.
            max_label_y = max_y + y_offset
            min_label_y = min_y - y_offset

            # Add chart space only when label coordinates would clip.
            y_pad = max(y_span * 0.02, y_offset * 0.25)
            if max_label_y + y_pad > y_axis_upper:
                y_axis_upper = max_label_y + y_pad
            if min_label_y - y_pad < y_axis_lower:
                y_axis_lower = min_label_y - y_pad
            if metric_name == "CPUUtilization" or metric_name.endswith("%"):
                y_axis_lower = max(y_axis_lower, 0.0)
                y_axis_upper = min(y_axis_upper, 100.0)
                max_label_y = min(max_label_y, y_axis_upper)
                min_label_y = max(min_label_y, y_axis_lower)

            plt.scatter([max_x], [max_y], marker="x", color="green")
            plt.scatter([min_x], [min_y], marker="x", color="red")
            plt.text(
                f"MAX {_format_plot_value(metric_name, max_y)}",
                max_label_x,
                max_label_y,
                color="green",
                alignment=max_align,
            )
            plt.text(
                f"MIN {_format_plot_value(metric_name, min_y)}",
                min_label_x,
                min_label_y,
                color="red",
                alignment=min_align,
            )

        plt.ylim(y_axis_lower, y_axis_upper)
        plt.yticks(y_tick_values, y_tick_labels)
        plt.title(y_label)
    plt.show()

    console.print(
        f"\n[dim]Window: last {hours}h  ·  Period: {period}s  ·  "
        f"Database: {esc(instance)}[/dim]"
    )


@app.command("list")
def list_instances(
    profile: str | None = ProfileOption,
    region: str | None = RegionOption,
    as_json: bool = JsonOption,
) -> None:
    """List all RDS instances in the account."""
    session = _get_session(profile, region)
    rds = session.client("rds")

    try:
        with console.status("[bold blue]Fetching RDS instances…[/bold blue]"):
            paginator = rds.get_paginator("describe_db_instances")
            instances = []
            for page in paginator.paginate():
                instances.extend(page["DBInstances"])
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "ClientError")
        msg = exc.response.get("Error", {}).get("Message", str(exc))
        console.print(f"[red]{esc(code)} on describe_db_instances: {esc(msg)}[/red]")
        raise typer.Exit(code=1)

    if as_json:
        payload = [
            {
                "identifier": inst["DBInstanceIdentifier"],
                "class": inst["DBInstanceClass"],
                "engine": f"{inst['Engine']} {inst.get('EngineVersion', '')}".strip(),
                "status": inst["DBInstanceStatus"],
                "storage_gb": inst.get("AllocatedStorage"),
                "multi_az": bool(inst.get("MultiAZ")),
            }
            for inst in sorted(instances, key=lambda i: i["DBInstanceIdentifier"])
        ]
        typer.echo(json.dumps(payload, indent=2))
        return

    if not instances:
        console.print("[yellow]No RDS instances found.[/yellow]")
        return

    table = _make_table("RDS Instances")
    table.add_column("Identifier", style="bold cyan")
    table.add_column("Class")
    table.add_column("Engine")
    table.add_column("Status")
    table.add_column("Storage (GB)", justify="right", style="bright_white")
    table.add_column("Multi-AZ", justify="center")

    for inst in sorted(instances, key=lambda i: i["DBInstanceIdentifier"]):
        status = inst["DBInstanceStatus"]
        table.add_row(
            cell(inst["DBInstanceIdentifier"]),
            cell(inst["DBInstanceClass"]),
            cell(f"{inst['Engine']} {inst.get('EngineVersion', '')}"),
            cell(status, style="green" if status == "available" else "yellow"),
            cell(inst.get("AllocatedStorage", "—")),
            "Yes" if inst.get("MultiAZ") else "No",
        )

    console.print()
    console.print(table)
    # Report what the session was actually built with: when --profile is an
    # alias, the flag holds the shorthand, not the profile boto3 received.
    console.print(
        f"\n[dim]{len(instances)} instance(s)  ·  "
        f"Profile: {esc(effective_profile(profile))}  ·  "
        f"Region: {esc(region or default_region())}[/dim]"
    )
