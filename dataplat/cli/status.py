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
from dataplat.services.db.capabilities import capabilities_for
from dataplat.services.db.connection import (
    LIBPQ_ENGINES,
    DbConnectionParams,
    DuckDbConnectionParams,
    SqlEngine,
)
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
    """Report whether one target is reachable, and what else it can be asked.

    Failures are *returned*, not raised, exactly as they were when this ran
    inline: one unreachable warehouse is a red line in the overview, not the end
    of it. Anything outside the expected families still propagates, where
    :func:`_guarded` degrades the whole databases section instead of the command.

    Two engine families answer "reachable?" by different means, so the probe
    dispatches on the resolved parameter *shape* — the same test ``db_session``
    makes, for the same reason: the shape is what decides which driver can be
    handed the values. A server is reached over a socket; a DuckDB database is a
    file this process opens.
    """
    from dataplat.cli.db._common import ConnCliParams
    from dataplat.core.errors import DataplatError

    try:
        params = ConnCliParams(target=name).resolve_any()
    except DataplatError as exc:
        # A target that cannot be resolved never reaches a driver, and the
        # message ("Missing required connection settings", or a HOST on a duckdb
        # target) is the actionable half of the failure.
        return {"reachable": False, "error": str(exc)[:160]}
    if isinstance(params, DuckDbConnectionParams):
        return _probe_duckdb(params, target.engine)
    return _probe_libpq(params, target.engine)


def _probe_libpq(params: DbConnectionParams, engine: SqlEngine) -> dict[str, Any]:
    """Connect to a server over libpq and count its long-running queries."""
    import psycopg

    from dataplat.core.errors import DataplatError
    from dataplat.services.db.long_queries import (
        fetch_long_queries,
        fetch_long_queries_postgres,
    )

    try:
        kwargs: dict = {**params.as_psycopg_kwargs(), "connect_timeout": 10}
        with psycopg.connect(**kwargs) as conn, conn.cursor() as cursor:
            if engine == SqlEngine.redshift:
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


def _probe_duckdb(params: DuckDbConnectionParams, engine: SqlEngine) -> dict[str, Any]:
    """Open the database file — which *is* the health check — and stop there.

    There is no socket to test and no session catalog to count, so opening the
    file is the whole probe. It is opened exactly as ``db_session`` would,
    ``read_only`` included, because "reachable" has to mean "a db command would
    connect here": probed on duckdb 1.5.5, a database another *process* holds
    raises IOException whichever mode is asked for, and ``:memory:`` cannot be
    opened read-only at all — a target configured that way is broken for every
    command, and a dashboard that opened it some other way would hide that.

    The connection is closed immediately. A health check must not sit on the
    single-writer lock of a file someone is about to run dbt against.
    """
    from dataplat.core.errors import DataplatError
    from dataplat.services.db.connection import (
        ensure_duckdb_database_exists,
        load_duckdb,
    )

    try:
        duckdb = load_duckdb()
        ensure_duckdb_database_exists(params)
    except DataplatError as exc:
        # A missing driver package or a path that is not a database file: local
        # configuration, and the message names the extra or the path.
        return {"reachable": False, "error": str(exc)[:160]}
    try:
        connection = duckdb.connect(database=params.path, read_only=params.read_only)
    except duckdb.Error as exc:
        return {"reachable": False, "error": str(exc)[:160]}
    connection.close()
    return {
        "reachable": True,
        # None, never 0. The count is not "all clear" here, it is unanswerable:
        # a green "no long-running queries" would be this dashboard's most
        # misleading line, since it is exactly what a healthy server prints.
        "long_running": None,
        "long_running_note": _no_sessions_note(engine),
    }


def _no_sessions_note(engine: SqlEngine) -> str:
    """Why ``engine`` has no long-running-query count, in the matrix's words.

    Read off :mod:`dataplat.services.db.capabilities` rather than written here,
    so the dashboard's "not applicable" and ``dp db long-queries``' refusal
    cannot drift into two different explanations of the same fact. Only reached
    for an engine that lacks the capability — the count is what a probe returns
    when it has one.
    """
    caps = capabilities_for(engine)
    return f"not applicable on {caps.label} ({caps.concurrent_sessions.reason})"


def _db_section() -> dict[str, Any]:
    targets = load_targets()
    if not targets:
        return {}

    # The driver import is attempted once, here, rather than N times from N
    # threads. Not `find_spec`, which ruff suggests — the driver being
    # *importable* is the question, and a broken install answers that
    # differently. It is no longer fatal to the section either: a DuckDB target
    # opens a file through its own driver, so psycopg's absence says nothing
    # about it and must not turn it red.
    driverless: dict[str, dict[str, Any]] = {}
    try:
        import psycopg  # noqa: F401
    except ImportError:
        driverless = {
            name: {
                "reachable": False,
                "error": "psycopg not installed (run `dp config sync`)",
            }
            for name, target in targets.items()
            if target.engine in LIBPQ_ENGINES
        }

    # Each probe carries a 10s connect timeout and they used to be spent one
    # after another, so three targets behind a dead host cost 30s of a "one-shot
    # overview". Overlapping the waits is the whole fix; the mapping is rebuilt
    # from `targets` afterwards so the key order stays the configured order.
    pending = {n: t for n, t in targets.items() if n not in driverless}
    probed: dict[str, dict[str, Any]] = {}
    if pending:
        with ThreadPoolExecutor(
            max_workers=min(len(pending), _MAX_TARGET_PROBES),
            thread_name_prefix="dp-status-db",
        ) as pool:
            probes = {
                name: pool.submit(_probe_target, name, target)
                for name, target in pending.items()
            }
        probed = {name: probe.result() for name, probe in probes.items()}
    return {name: driverless.get(name) or probed[name] for name in targets}


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
        if count is None:
            # Reachable — the ✓ says that here exactly as it does for a server —
            # but the count is unanswerable rather than zero, so neither claim
            # the branches below make is available. The reason travels with the
            # gap: an unexplained "not applicable" reads as a tool defect.
            note = info.get("long_running_note") or "not applicable"
            console.print(
                f"  [green]✓[/green] {esc(name)} "
                f"[dim]— long-running queries {esc(note)}[/dim]"
            )
            continue
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
