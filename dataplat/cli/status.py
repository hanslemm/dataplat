"""``dp status`` — one-shot health dashboard across every system.

Each section degrades independently: a failing service renders as
"unavailable" with the reason instead of killing the whole command.

Every probe here is blocking I/O — a TCP connect, an HTTP round trip, a
``docker ps`` — so the sections run in a thread pool rather than one after
another. Two properties of the serial version survive that unchanged, because
both are contracts rather than accidents:

- **Order.** The JSON payload's keys and the human sections come out in the
  order declared in :func:`status`, never in the order the probes answered.
- **Independence.** :func:`_guarded` turns an exception escaping a section into
  that section's error line, so one broken system still costs one line.

The AWS section is the exception and stays serial; see :func:`status`.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any

import typer
from rich.console import Console

from dataplat.cli._options import json_option
from dataplat.cli._render import esc
from dataplat.services.db.connection import SqlEngine
from dataplat.services.db.targets import DbTarget, load_targets

console = Console()
# Spinners, notices and warnings go here, never to stdout: `dp status --json`
# must stay a parseable document, and an expired SSO token used to write its
# "running aws sso login" notice straight into the middle of the payload.
err_console = Console(stderr=True)

app = typer.Typer(
    name="status", help="One-shot health overview", invoke_without_command=True
)

_LONG_QUERY_THRESHOLD_S = 60

# One probe per target, capped. The work is waiting on a socket, so the useful
# width is the number of targets rather than anything CPU-derived; the cap is
# only there so a 200-target config cannot turn a health check into a thread
# storm. Every realistic target list fits in one wave.
_MAX_TARGET_PROBES = 16

# What a section does: no arguments, one JSON-shaped mapping out, never raises
# once wrapped by _guarded.
Section = Callable[[], dict[str, Any]]


def _probe_target(name: str, target: DbTarget) -> dict[str, Any]:
    """Connect to one target and count its long-running queries.

    Failures are *returned*, not raised, exactly as they were when this ran
    inline: one unreachable warehouse is a red line in the overview, not the end
    of it. Anything outside the two expected families still propagates, where
    :func:`_guarded` degrades the whole databases section instead of the command.
    """
    import psycopg

    from dataplat.cli.db._common import ConnCliParams
    from dataplat.core.errors import DataplatError
    from dataplat.services.db.long_queries import (
        fetch_long_queries,
        fetch_long_queries_postgres,
    )

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
        return {"reachable": True, "long_running": len(rows)}
    except (DataplatError, psycopg.Error) as exc:
        return {"reachable": False, "error": str(exc)[:160]}


def _db_section() -> dict[str, Any]:
    # Imported for its side effect: without the driver there is nothing to probe,
    # and every target should say so once rather than N times from N threads.
    # Not `find_spec`, which ruff suggests here — the driver being *importable*
    # is the question, and a broken install answers that differently.
    try:
        import psycopg  # noqa: F401
    except ImportError:
        return {
            name: {
                "reachable": False,
                "error": "psycopg not installed (run `dp config sync`)",
            }
            for name in load_targets()
        }

    targets = load_targets()
    if not targets:
        return {}
    # Each probe carries a 10s connect timeout and they used to be spent one
    # after another, so three targets behind a dead host cost 30s of a "one-shot
    # overview". Overlapping the waits is the whole fix; the mapping is rebuilt
    # from `targets` afterwards so the key order stays the configured order.
    with ThreadPoolExecutor(
        max_workers=min(len(targets), _MAX_TARGET_PROBES),
        thread_name_prefix="dp-status-db",
    ) as pool:
        probes = {
            name: pool.submit(_probe_target, name, target)
            for name, target in targets.items()
        }
    return {name: probe.result() for name, probe in probes.items()}


def _airbyte_section() -> dict[str, Any]:
    from dataplat.core.errors import AuthError, ConfigError, ServiceError

    try:
        from dataplat.services.airbyte.client import build_authenticated_client
        from dataplat.services.airbyte.jobs import list_jobs
    except ImportError:
        return {
            "available": False,
            "error": "ingest dependencies not installed (run `dp config sync`)",
        }

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
                "docker",
                "ps",
                "-a",
                "--filter",
                "name=gha-runner-",
                "--format",
                "{{.Names}}\t{{.Status}}",
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
            notify=lambda msg: err_console.print(f"[yellow]{esc(msg)}[/yellow]"),
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


def _guarded(section: Section) -> dict[str, Any]:
    """Run one section; report anything that escapes it as that section's error.

    Sections are written to return their failures, but only for the families
    they anticipated — a malformed ``DP_TARGETS`` raises ``ConfigError`` out of
    ``load_targets()`` and used to end the entire command in a traceback, before
    a single section had rendered and with ``--json`` producing no document at
    all. Catching broadly is the point: "this system is unavailable, here is
    why" is a strictly better answer than a stack trace for every system.

    The type name is kept in the message because an unexpected exception's
    ``str`` is routinely empty or a bare key, and ``✗ unavailable — ''`` tells
    the reader nothing.
    """
    try:
        return section()
    except Exception as exc:
        return {"available": False, "error": f"{type(exc).__name__}: {exc}"[:160]}


def _run_concurrently(sections: dict[str, Section]) -> dict[str, Any]:
    """Probe every section at once; return them in the order they were declared.

    Iterating ``sections`` rather than ``as_completed`` is deliberate and is what
    keeps the payload stable: whichever probe answers first, the caller sees the
    declared order, so ``--json`` consumers and the human layout are unaffected
    by how fast anything was.
    """
    with ThreadPoolExecutor(
        max_workers=len(sections), thread_name_prefix="dp-status"
    ) as pool:
        running = {name: pool.submit(_guarded, fn) for name, fn in sections.items()}
    # The pool has joined here, so every result is already in hand; _guarded
    # means none of these can raise.
    return {name: future.result() for name, future in running.items()}


def _print_db(section: dict[str, Any]) -> None:
    console.print("[bold cyan]Databases[/bold cyan]")
    reason = section.get("error")
    if isinstance(reason, str):
        # A whole-section failure rather than a per-target one: the probe raised
        # before it could enumerate targets. Every value in a healthy payload is
        # a mapping, so a string here is unambiguous even for a target someone
        # actually named "error".
        console.print(f"  [red]✗ unavailable[/red] [dim]— {esc(reason)}[/dim]")
        return
    if not section:
        console.print("  [dim]no targets configured (set DP_TARGETS)[/dim]")
    # Target names come from DP_TARGETS and the errors from psycopg: both are
    # external, so each is escaped on its own, never the whole markup string.
    for name, info in section.items():
        if not info.get("reachable"):
            console.print(
                f"  [red]✗ {esc(name)}[/red] [dim]— {esc(info.get('error'))}[/dim]"
            )
            continue
        count = info.get("long_running", 0)
        marker = "[yellow]![/yellow]" if count else "[green]✓[/green]"
        detail = (
            f"{count} query(ies) running >{_LONG_QUERY_THRESHOLD_S}s"
            if count
            else "no long-running queries"
        )
        console.print(f"  {marker} {esc(name)} [dim]— {detail}[/dim]")


def _print_airbyte(section: dict[str, Any]) -> None:
    console.print("[bold cyan]Airbyte[/bold cyan]")
    if not section.get("available"):
        console.print(
            f"  [red]✗ unavailable[/red] [dim]— {esc(section.get('error'))}[/dim]"
        )
        return
    failed = section.get("failed", [])
    marker = "[red]✗[/red]" if failed else "[green]✓[/green]"
    console.print(
        f"  {marker} {section.get('jobs_last_24h', 0)} job(s) in 24h — "
        f"{len(failed)} failed, {section.get('running', 0)} running"
    )
    for job in failed[:5]:
        # Job and connection ids are whatever the Airbyte API returned.
        console.print(
            f"    [red]failed[/red] job {esc(job.get('jobId'))} "
            f"[dim](connection {esc(job.get('connectionId'))})[/dim]"
        )


def _print_runners(section: dict[str, Any]) -> None:
    console.print("[bold cyan]GitHub runners[/bold cyan]")
    if not section.get("available"):
        console.print(
            f"  [red]✗ unavailable[/red] [dim]— {esc(section.get('error'))}[/dim]"
        )
        return
    runners = section.get("runners", [])
    if not runners:
        console.print("  [dim]no runner containers[/dim]")
        return
    for r in runners:
        marker = "[green]✓[/green]" if r["running"] else "[yellow]![/yellow]"
        # Container names and status strings are docker's, not ours.
        console.print(f"  {marker} {esc(r['name'])} [dim]— {esc(r['status'])}[/dim]")


def _print_aws(section: dict[str, Any]) -> None:
    console.print("[bold cyan]AWS (RDS)[/bold cyan]")
    if not section.get("available"):
        console.print(
            f"  [yellow]! unavailable[/yellow] [dim]— {esc(section.get('error'))}[/dim]"
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
    # The instance id comes from DP_RDS_INSTANCE; the metric strings are ours.
    console.print(
        f"  [green]✓[/green] {esc(section.get('instance'))} "
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
    as_json: bool = json_option("Emit JSON instead of text."),
) -> None:
    """Health overview: databases, Airbyte jobs, runners, and RDS."""
    # Built here, not at module scope, so each name resolves at call time: the
    # tests replace these functions on the module, and a dict captured at import
    # would hold the originals. Insertion order is the payload's key order and
    # the order the sections print in — both are pinned by tests.
    concurrent: dict[str, Section] = {
        "databases": _db_section,
        "airbyte": _airbyte_section,
        "runners": _runners_section,
    }

    with err_console.status(
        "[bold blue]Checking databases, Airbyte, runners…[/bold blue]"
    ):
        payload: dict[str, Any] = _run_concurrently(concurrent)

    if aws:
        # AWS stays serial, last, and outside the spinner. The section may hand
        # the terminal to an interactive `aws sso login` (browser + device code),
        # which cannot share stdin with a Live spinner or with another section
        # that might prompt; running it only after the pool has joined is what
        # guarantees it has the terminal to itself. There is no way to find out
        # in advance whether the token is expired without doing the work, so
        # "start it concurrently and serialise only the prompt" is not available.
        payload["aws"] = _guarded(_aws_section)

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
