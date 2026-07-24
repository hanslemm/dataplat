"""``dp status`` — one-shot health dashboard across every system.

Each section degrades independently: a failing service renders as
"unavailable" with the reason instead of killing the whole command.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from typing import Any

import typer
from rich.console import Console

from dataplat.services.db.connection import SqlEngine
from dataplat.services.db.targets import load_targets

console = Console()

app = typer.Typer(name="status", help="One-shot health overview", invoke_without_command=True)

_LONG_QUERY_THRESHOLD_S = 60


def _db_section() -> dict[str, Any]:
    import psycopg

    from dataplat.cli.db._common import ConnCliParams
    from dataplat.core.errors import DataplatError
    from dataplat.services.db.long_queries import (
        fetch_long_queries,
        fetch_long_queries_postgres,
    )

    out: dict[str, Any] = {}
    for name, target in load_targets().items():
        try:
            params = ConnCliParams(target=name).resolve()
            kwargs: dict = {**params.as_psycopg_kwargs(), "connect_timeout": 10}
            with psycopg.connect(**kwargs) as conn, conn.cursor() as cursor:
                if target.engine == SqlEngine.redshift:
                    rows = fetch_long_queries(
                        cursor,
                        min_seconds=_LONG_QUERY_THRESHOLD_S,
                        limit=50,
                        cutoff=datetime.now(UTC),
                        running_only=True,
                    )
                else:
                    rows = fetch_long_queries_postgres(
                        cursor,
                        min_seconds=_LONG_QUERY_THRESHOLD_S,
                        limit=50,
                    )
            out[name] = {"reachable": True, "long_running": len(rows)}
        except (DataplatError, psycopg.Error) as exc:
            out[name] = {"reachable": False, "error": str(exc)[:160]}
    return out


def _airbyte_section() -> dict[str, Any]:
    from dataplat.core.errors import AuthError, ConfigError, ServiceError
    from dataplat.services.airbyte.client import build_authenticated_client
    from dataplat.services.airbyte.jobs import list_jobs

    try:
        client, base_url = build_authenticated_client()
    except (ConfigError, AuthError) as exc:
        return {"available": False, "error": str(exc)[:160]}

    cutoff = datetime.now(UTC) - timedelta(hours=24)
    try:
        jobs = list_jobs(client, base_url, limit=100)
    except ServiceError as exc:
        return {"available": False, "error": str(exc)[:160]}
    finally:
        client.close()

    def _started_at(job: dict) -> datetime | None:
        raw = job.get("startTime") or job.get("createdAt")
        if not raw:
            return None
        try:
            return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            return None

    recent = [j for j in jobs if (ts := _started_at(j)) and ts >= cutoff]
    failed = [j for j in recent if str(j.get("status", "")).lower() == "failed"]
    running = [
        j for j in recent if str(j.get("status", "")).lower() in {"running", "pending"}
    ]
    return {
        "available": True,
        "jobs_last_24h": len(recent),
        "failed": [
            {"jobId": j.get("jobId"), "connectionId": j.get("connectionId")}
            for j in failed
        ],
        "running": len(running),
    }


def _runners_section() -> dict[str, Any]:
    if shutil.which("docker") is None:
        return {"available": False, "error": "docker not found on PATH"}
    try:
        proc = subprocess.run(
            [
                "docker", "ps", "-a",
                "--filter", "name=gha-runner-",
                "--format", "{{.Names}}\t{{.Status}}",
            ],
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        return {"available": False, "error": str(exc)[:160]}
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip().splitlines()
        return {
            "available": False,
            "error": detail[0] if detail else "docker daemon unreachable",
        }
    runners = []
    for line in proc.stdout.strip().splitlines():
        parts = line.split("\t", 1)
        if len(parts) == 2:
            runners.append(
                {
                    "name": parts[0],
                    "status": parts[1],
                    "running": parts[1].lower().startswith("up"),
                }
            )
    return {"available": True, "runners": runners}


def _aws_section() -> dict[str, Any]:
    """RDS key metrics; runs ``aws sso login`` when the token is expired."""
    try:
        import botocore.exceptions
    except Exception:
        return {"available": False, "error": "boto3 not installed"}

    import os

    from dataplat.cli.cloud.aws._common import default_profile, default_region
    from dataplat.cli.cloud.aws.rds import _fetch_metric_summaries
    from dataplat.core.errors import AuthError, ServiceError
    from dataplat.services.aws.auth import get_session

    instance = os.getenv("DP_RDS_INSTANCE")
    if not instance:
        return {"available": False, "error": "DP_RDS_INSTANCE not set"}

    try:
        session = get_session(
            profile=default_profile(),
            region=default_region(),
            notify=lambda msg: console.print(f"[yellow]{msg}[/yellow]"),
        )
        cw = session.client("cloudwatch")
        now = datetime.now(UTC)
        summaries = _fetch_metric_summaries(
            cw, instance, now - timedelta(hours=1), now, 300
        )
    except (AuthError, ServiceError) as exc:
        return {"available": False, "error": str(exc)[:160]}
    except (
        botocore.exceptions.BotoCoreError,
        botocore.exceptions.ClientError,
    ) as exc:
        return {"available": False, "error": str(exc)[:160]}

    metrics = {
        name: summary["latest"]
        for name, summary in summaries
        if summary is not None and name in {"CPUUtilization", "FreeStorageSpace"}
    }
    return {"available": True, "instance": instance, "metrics": metrics}


def _print_db(section: dict[str, Any]) -> None:
    console.print("[bold cyan]Databases[/bold cyan]")
    if not section:
        console.print("  [dim]no targets configured (set DP_TARGETS)[/dim]")
    for name, info in section.items():
        if not info.get("reachable"):
            console.print(f"  [red]✗ {name}[/red] [dim]— {info.get('error')}[/dim]")
            continue
        count = info.get("long_running", 0)
        marker = "[yellow]![/yellow]" if count else "[green]✓[/green]"
        detail = (
            f"{count} query(ies) running >{_LONG_QUERY_THRESHOLD_S}s"
            if count
            else "no long-running queries"
        )
        console.print(f"  {marker} {name} [dim]— {detail}[/dim]")


def _print_airbyte(section: dict[str, Any]) -> None:
    console.print("[bold cyan]Airbyte[/bold cyan]")
    if not section.get("available"):
        console.print(f"  [red]✗ unavailable[/red] [dim]— {section.get('error')}[/dim]")
        return
    failed = section.get("failed", [])
    marker = "[red]✗[/red]" if failed else "[green]✓[/green]"
    console.print(
        f"  {marker} {section.get('jobs_last_24h', 0)} job(s) in 24h — "
        f"{len(failed)} failed, {section.get('running', 0)} running"
    )
    for job in failed[:5]:
        console.print(
            f"    [red]failed[/red] job {job.get('jobId')} "
            f"[dim](connection {job.get('connectionId')})[/dim]"
        )


def _print_runners(section: dict[str, Any]) -> None:
    console.print("[bold cyan]GitHub runners[/bold cyan]")
    if not section.get("available"):
        console.print(f"  [red]✗ unavailable[/red] [dim]— {section.get('error')}[/dim]")
        return
    runners = section.get("runners", [])
    if not runners:
        console.print("  [dim]no runner containers[/dim]")
        return
    for r in runners:
        marker = "[green]✓[/green]" if r["running"] else "[yellow]![/yellow]"
        console.print(f"  {marker} {r['name']} [dim]— {r['status']}[/dim]")


def _print_aws(section: dict[str, Any]) -> None:
    console.print("[bold cyan]AWS (RDS)[/bold cyan]")
    if not section.get("available"):
        console.print(
            f"  [yellow]! unavailable[/yellow] [dim]— {section.get('error')}[/dim]"
        )
        return
    metrics = section.get("metrics", {})
    cpu = metrics.get("CPUUtilization")
    storage = metrics.get("FreeStorageSpace")
    parts = []
    if cpu is not None:
        parts.append(f"CPU {cpu:.1f}%")
    if storage is not None:
        parts.append(f"free storage {storage / 1024**3:.1f} GB")
    console.print(
        f"  [green]✓[/green] {section.get('instance')} "
        f"[dim]— {' · '.join(parts) if parts else 'no datapoints'}[/dim]"
    )


@app.callback(invoke_without_command=True)
def status(
    aws: bool = typer.Option(
        True,
        "--aws/--no-aws",
        help="Include the AWS (RDS) section. Runs `aws sso login` if the "
        "SSO token is expired; pass --no-aws to skip.",
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON instead of text."),
) -> None:
    """Health overview: databases, Airbyte jobs, runners, and RDS."""
    payload: dict[str, Any] = {}

    with console.status("[bold blue]Checking databases…[/bold blue]"):
        payload["databases"] = _db_section()
    with console.status("[bold blue]Checking Airbyte…[/bold blue]"):
        payload["airbyte"] = _airbyte_section()
    payload["runners"] = _runners_section()
    if aws:
        # No spinner here: the section may hand the terminal to an
        # interactive `aws sso login` (browser + code prompt).
        payload["aws"] = _aws_section()

    if as_json:
        typer.echo(json.dumps(payload, indent=2, default=str))
        return

    _print_db(payload["databases"])
    console.print()
    _print_airbyte(payload["airbyte"])
    console.print()
    _print_runners(payload["runners"])
    if aws:
        console.print()
        _print_aws(payload["aws"])
