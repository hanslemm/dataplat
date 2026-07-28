"""Configuration CLI commands: init, show, doctor."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import cast

import typer
from rich import box
from rich.console import Console
from rich.table import Table
from rich.text import Text

from dataplat.cli._exit import fail
from dataplat.cli._options import yes_option
from dataplat.cli._render import cell, esc
from dataplat.core.envrc import (
    CONFIG_ENVRC,
    EnvrcLocation,
    EnvrcSource,
    link_envrc,
    locate_envrc,
    unexpanded_env_refs,
)
from dataplat.services.db.connection import SqlEngine

app = typer.Typer(
    name="config",
    help="Configuration commands",
    no_args_is_help=True,
)

console = Console()

# `show` and `doctor` describe a current-directory .envrc identically, and
# both point at the same two escape hatches.
CWD_ENVRC_DETAIL = "loaded from the current directory, not the global link"
CWD_ENVRC_HINT = (
    "Run `dp config init` to pin a trusted file, or set "
    "DP_ENVRC_ALLOW_CWD=0 to ignore ./.envrc."
)

# `export PGHOST=$DB_HOST` loads the literal "$DB_HOST": dataplat parses .envrc
# itself and does not expand. The failure that follows is a connection error, so
# it reads as a wrong host or a wrong password — which is why both commands say
# so out loud instead of leaving it to core/envrc.py's docstring.
UNEXPANDED_MARK = "! $VAR not expanded"
UNEXPANDED_HINT = (
    "dataplat reads .envrc itself and does not expand shell variables. "
    "Write the value out, or export it from your shell before running dp."
)


def _unexpanded_refs(location: EnvrcLocation | None) -> dict[str, list[str]]:
    """Which loaded vars still hold an unexpanded ``$VAR``, and which they name.

    Re-reads the active file rather than remembering what ``load_envrc`` saw: the
    load happens at import in :mod:`dataplat.main` and keeps nothing, and the
    file is the thing the user is about to edit.
    """
    if location is None:
        return {}
    try:
        content = location.path.read_text()
    except OSError:
        # The same silence as load_envrc, and for the same reason: a file it
        # could not read contributed no values, so there is nothing to warn about.
        return {}
    return unexpanded_env_refs(content)


@dataclass(frozen=True)
class EnvVarSpec:
    """One environment variable the CLI cares about."""

    name: str
    secret: bool = False
    required: bool = True


def _target_specs(prefix: str, engine: SqlEngine | None = None) -> list[EnvVarSpec]:
    """The variables one target needs, which depend on what it connects to.

    A DuckDB target has a file, not a server: asking it for ``_HOST``, ``_USER``
    and ``_PASSWORD`` reported four missing variables and a failed check for a
    configuration that works perfectly, which is worse than saying nothing —
    ``dp config doctor`` exists to be believed.
    """
    if engine is SqlEngine.duckdb:
        return [
            # Either names the database file; connection.py resolves _PATH first
            # and falls back to _DATABASE, so neither alone is missing.
            EnvVarSpec(f"{prefix}_PATH", required=False),
            EnvVarSpec(f"{prefix}_DATABASE", required=False),
            EnvVarSpec(f"{prefix}_ENGINE", required=False),
            EnvVarSpec(f"{prefix}_READ_ONLY", required=False),
        ]
    return [
        EnvVarSpec(f"{prefix}_HOST"),
        EnvVarSpec(f"{prefix}_PORT", required=False),
        EnvVarSpec(f"{prefix}_DATABASE"),
        EnvVarSpec(f"{prefix}_USER"),
        EnvVarSpec(f"{prefix}_PASSWORD", secret=True),
        EnvVarSpec(f"{prefix}_ENGINE", required=False),
        EnvVarSpec(f"{prefix}_REASSIGN_OWNER", required=False),
    ]


def component_vars() -> dict[str, list[EnvVarSpec]]:
    """Component -> variables, built from the configured targets.

    Alternatives (Airbyte cloud vs OSS auth) are handled specially in
    doctor below.
    """
    from dataplat.services.db.targets import load_targets

    components: dict[str, list[EnvVarSpec]] = {
        "Core": [
            EnvVarSpec("DP_TARGETS", required=False),
            EnvVarSpec("DP_DEFAULT_TARGET", required=False),
            EnvVarSpec("DP_ENVRC_PATH", required=False),
        ],
    }
    for name, target in load_targets().items():
        components[f"target: {name}"] = _target_specs(target.env_prefix, target.engine)
    components.update(
        {
            "Airbyte": [
                EnvVarSpec("AIRBYTE_BASE_URL"),
                EnvVarSpec("AIRBYTE_CLIENT_ID", required=False),
                EnvVarSpec("AIRBYTE_CLIENT_SECRET", secret=True, required=False),
                EnvVarSpec("AIRBYTE_EMAIL", required=False),
                EnvVarSpec("AIRBYTE_PASSWORD", secret=True, required=False),
            ],
            "Superset": [
                EnvVarSpec("SUPERSET_BASE_URL"),
                EnvVarSpec("SUPERSET_ADMIN_USERNAME"),
                EnvVarSpec("SUPERSET_ADMIN_PASSWORD", secret=True),
            ],
            "AWS": [
                EnvVarSpec("DP_AWS_PROFILE", required=False),
                EnvVarSpec("DP_AWS_PROFILE_ALIASES", required=False),
                EnvVarSpec("DP_AWS_REGION", required=False),
                EnvVarSpec("DP_RDS_INSTANCE", required=False),
            ],
            "dbt": [
                EnvVarSpec("DP_DBT_PROJECT", required=False),
                EnvVarSpec("DP_DBT_INVOCATION_COMMAND", required=False),
                EnvVarSpec("DP_DBT_ORPHANS_EXCLUDE_SCHEMAS", required=False),
            ],
            "GitHub runner": [
                EnvVarSpec("GHA_APP_ID"),
                EnvVarSpec("GHA_APP_PRIVATE_KEY", secret=True),
                EnvVarSpec("DP_CI_RUNNER_DNS", required=False),
            ],
        }
    )
    return components


@app.command("init")
def init(
    envrc: str | None = typer.Option(
        None,
        "--envrc",
        "-e",
        help="Path to .envrc file to link. Defaults to .envrc in current directory.",
    ),
) -> None:
    """Set up dataplat global envrc link."""
    source = Path(envrc).expanduser().resolve() if envrc else Path.cwd() / ".envrc"
    if not source.is_file():
        console.print(f"[red]Error: {esc(source)} not found[/red]")
        raise typer.Exit(code=1)

    if (
        CONFIG_ENVRC.is_symlink() or CONFIG_ENVRC.exists()
    ) and CONFIG_ENVRC.resolve() == source.resolve():
        console.print(f"Already linked: {esc(CONFIG_ENVRC)} -> {esc(source)}")
        return

    try:
        link_envrc(source)
    except OSError as exc:
        console.print(f"[red]Failed to link envrc: {esc(exc)}[/red]")
        raise typer.Exit(code=1)

    console.print(f"Linked: {esc(CONFIG_ENVRC)} -> {esc(source)}")


@app.command("sync")
def sync(
    yes: bool = yes_option("Install without prompting."),
    check: bool = typer.Option(
        False,
        "--check",
        help="Report only; exit 1 if an enabled area is missing dependencies.",
    ),
) -> None:
    """Install the dependencies for every area your config enables."""
    from dataplat.cli._missing import run_install
    from dataplat.core.deps import AREAS, enabled_areas, missing_modules

    enabled = enabled_areas()
    table = Table(show_header=True, header_style="bold cyan", box=box.SIMPLE)
    table.add_column("Area", style="bold")
    table.add_column("Enabled by")
    table.add_column("Dependencies")

    needed_extras: list[str] = []
    for name, spec in AREAS.items():
        missing = missing_modules(name)
        if name not in enabled:
            state = "[dim]installed (unused)[/dim]" if not missing else "[dim]—[/dim]"
            table.add_row(name, "[dim]not enabled[/dim]", state)
            continue
        if missing:
            needed_extras.append(spec.extra)
            table.add_row(
                name, enabled[name], f"[red]missing: {', '.join(missing)}[/red]"
            )
        else:
            table.add_row(name, enabled[name], "[green]ok[/green]")
    console.print(table)

    if not needed_extras:
        console.print(
            "[green]Every enabled area has its dependencies installed.[/green]"
        )
        return
    if check:
        raise typer.Exit(code=1)
    if not run_install(needed_extras, yes=yes):
        raise typer.Exit(code=1)
    console.print("[green]Dependencies installed — the areas above are ready.[/green]")


def _mask(value: str | None, secret: bool, *, unexpanded: bool = False) -> Text:
    """Render one variable's state as a cell.

    Both branches return :class:`~rich.text.Text` so the caller never has to
    know which is which: the unset/secret markers are markup we author, while
    the value itself is whatever the environment holds and must render
    verbatim — an ``[unclosed`` tag there used to crash the whole table.

    ``unexpanded`` appends the ``$VAR`` marker. A reader who sees ``$DB_HOST``
    in the value column has no reason to suspect it: in their shell that line
    works, so the cell showing it faithfully is exactly what hides the problem.
    The marker also covers a secret, whose value is not shown at all.
    """
    if not value:
        # Nothing was loaded, so there is no literal to mark.
        return Text.from_markup("[dim]unset[/dim]")
    rendered = (
        Text.from_markup("[green]set[/green] [dim](hidden)[/dim]")
        if secret
        else cell(value, max_length=60)
    )
    if unexpanded:
        rendered = rendered + Text.from_markup(f" [yellow]{UNEXPANDED_MARK}[/yellow]")
    return rendered


@app.command("show")
def show() -> None:
    """Show the active .envrc and the state of every known env var."""
    location = locate_envrc()
    if location is None:
        console.print("[bold]Active .envrc:[/bold] [red]none found[/red]")
    else:
        console.print(
            f"[bold]Active .envrc:[/bold] {esc(location.path)} "
            f"[dim]({location.source.value})[/dim]"
        )
        if location.source is EnvrcSource.cwd:
            console.print(f"[yellow]! {CWD_ENVRC_DETAIL}[/yellow]")
            console.print(f"  [dim]{CWD_ENVRC_HINT}[/dim]")
    if CONFIG_ENVRC.is_symlink():
        console.print(
            f"[bold]Global link:[/bold] {esc(CONFIG_ENVRC)} -> "
            f"{esc(CONFIG_ENVRC.resolve())}"
        )
    else:
        console.print(
            "[bold]Global link:[/bold] [dim]not set (run `dp config init`)[/dim]"
        )
    console.print()

    table = Table(show_header=True, header_style="bold cyan", box=box.SIMPLE)
    table.add_column("Component", style="bold")
    table.add_column("Variable")
    table.add_column("Value")

    from dataplat.core.errors import ConfigError

    try:
        components = component_vars()
    except ConfigError as exc:
        # `dp config show` is the command someone runs *because* their config is
        # broken, and a bad DP_TARGETS used to answer with a traceback — hiding
        # the ".envrc active here" header printed just above, which is the one
        # thing already on screen that they need. Exits 3 (CONFIG), not 1.
        fail(exc, console=console)

    unexpanded = _unexpanded_refs(location)
    marked = False
    for component, specs in components.items():
        for i, spec in enumerate(specs):
            # Component and variable names are built from DP_TARGETS, so they
            # are user data too.
            is_literal = spec.name in unexpanded
            marked = marked or is_literal
            table.add_row(
                cell(component if i == 0 else ""),
                cell(spec.name),
                _mask(os.getenv(spec.name), spec.secret, unexpanded=is_literal),
            )

    console.print(table)
    if marked:
        # The footnote exists to explain a marker, so it appears only with one.
        # `unexpanded` can also name vars this table does not list — an arbitrary
        # .envrc key is nobody's spec — which is what `doctor` is for.
        console.print(
            f"[yellow]{UNEXPANDED_MARK}[/yellow] [dim]{UNEXPANDED_HINT}[/dim]"
        )


class CheckStatus(str, Enum):
    """Outcome of one doctor check.

    ``warn`` is deliberately not a failure: doctor's exit code is a contract,
    so surfacing a new advisory must never flip a setup that used to exit 0.
    """

    ok = "ok"
    warn = "warn"
    fail = "fail"


@dataclass
class CheckResult:
    label: str
    status: CheckStatus
    detail: str = ""
    hint: str = ""


def _check(label: str, ok: bool, detail: str = "", hint: str = "") -> CheckResult:
    status = CheckStatus.ok if ok else CheckStatus.fail
    return CheckResult(label=label, status=status, detail=detail, hint=hint)


def _warn(label: str, detail: str = "", hint: str = "") -> CheckResult:
    """A finding worth showing that must not fail the command."""
    return CheckResult(label=label, status=CheckStatus.warn, detail=detail, hint=hint)


def _offline_checks() -> list[CheckResult]:
    from dataplat.core.errors import ConfigError
    from dataplat.services.db.targets import load_targets

    results: list[CheckResult] = []

    location = locate_envrc()
    results.append(
        _check(
            "envrc found",
            location is not None,
            detail=(
                f"{location.path} ({location.source.value})"
                if location is not None
                else ""
            ),
            hint="Run `dp config init --envrc /path/to/.envrc`.",
        )
    )
    if location is not None and location.source is EnvrcSource.cwd:
        results.append(_warn("envrc trust", CWD_ENVRC_DETAIL, CWD_ENVRC_HINT))

    unexpanded = _unexpanded_refs(location)
    if unexpanded:
        # A warning, never a failure: doctor's exit code is a contract, and a
        # value that *might* be wrong must not flip a setup that used to exit 0.
        #
        # The affected keys are named; the variables their values reference are
        # not. The reference is extracted from the value, and a value can be a
        # credential — `PGPASSWORD="p$ssw0rd"` would otherwise print a fragment
        # of the password into whatever log this run lands in. The key is enough
        # to act on: it names the .envrc line to look at, and `dp config show`
        # prints the literal for everything that is not a secret.
        results.append(
            _warn(
                "shell expansion",
                f"loaded literally: {', '.join(unexpanded)}",
                UNEXPANDED_HINT,
            )
        )

    try:
        targets = load_targets()
    except ConfigError as exc:
        results.append(_check("DB targets", False, detail=str(exc)[:120]))
        targets = {}
    if not targets:
        results.append(
            _check(
                "DB targets",
                True,
                detail="none configured (set DP_TARGETS to define targets)",
            )
        )
    for name, target in targets.items():
        missing = [
            s.name
            for s in _target_specs(target.env_prefix, target.engine)
            if s.required and not os.getenv(s.name)
        ]
        results.append(
            _check(
                f"target {name}",
                not missing,
                detail="" if missing else f"{target.engine.value}, all set",
                hint=f"Missing: {', '.join(missing)}" if missing else "",
            )
        )

    # Optional-dependency status for every enabled area.
    from dataplat.core.deps import enabled_areas, missing_modules

    for area, var in enabled_areas().items():
        missing_deps = missing_modules(area)
        results.append(
            _check(
                f"{area} dependencies",
                not missing_deps,
                detail=(
                    f"enabled by {var}"
                    if not missing_deps
                    else f"enabled by {var}; missing {', '.join(missing_deps)}"
                ),
                hint="Run `dp config sync`." if missing_deps else "",
            )
        )

    # Optional components: flag missing vars only when partially configured.
    base = bool(os.getenv("AIRBYTE_BASE_URL"))
    cloud = bool(os.getenv("AIRBYTE_CLIENT_ID") and os.getenv("AIRBYTE_CLIENT_SECRET"))
    oss = bool(os.getenv("AIRBYTE_EMAIL") and os.getenv("AIRBYTE_PASSWORD"))
    if base or cloud or oss:
        results.append(
            _check(
                "Airbyte env",
                base and (cloud or oss),
                detail="cloud creds" if cloud else "OSS creds" if oss else "",
                hint="Set AIRBYTE_BASE_URL plus CLIENT_ID/CLIENT_SECRET "
                "(cloud) or EMAIL/PASSWORD (OSS).",
            )
        )
    else:
        results.append(_check("Airbyte env", True, detail="not configured"))

    superset_specs = [
        "SUPERSET_BASE_URL",
        "SUPERSET_ADMIN_USERNAME",
        "SUPERSET_ADMIN_PASSWORD",
    ]
    superset_set = [v for v in superset_specs if os.getenv(v)]
    if superset_set:
        missing = [v for v in superset_specs if not os.getenv(v)]
        results.append(
            _check(
                "Superset env",
                not missing,
                detail="" if missing else "all set",
                hint=f"Missing: {', '.join(missing)}" if missing else "",
            )
        )
    else:
        results.append(_check("Superset env", True, detail="not configured"))

    runner_specs = ["GHA_APP_ID", "GHA_APP_PRIVATE_KEY"]
    runner_set = [v for v in runner_specs if os.getenv(v)]
    if runner_set:
        missing = [v for v in runner_specs if not os.getenv(v)]
        results.append(
            _check(
                "GitHub runner env",
                not missing,
                detail="" if missing else "all set",
                hint=f"Missing: {', '.join(missing)}" if missing else "",
            )
        )
        results.append(
            _check(
                "docker binary",
                shutil.which("docker") is not None,
                hint="Install Docker (needed for `dp ci github runner`).",
            )
        )
    else:
        results.append(_check("GitHub runner env", True, detail="not configured"))

    return results


def _connect_checks() -> list[CheckResult]:
    results: list[CheckResult] = []

    # Databases
    from dataplat.core.errors import DataplatError
    from dataplat.services.db.targets import load_targets

    db_deps_ready = True
    try:
        import psycopg

        from dataplat.cli.db._common import ConnCliParams, db_session
        from dataplat.services.db.connection import (
            LIBPQ_ENGINES,
            DbConnectionParams,
        )
    except ImportError:
        db_deps_ready = False
        results.append(
            _check(
                "db probes",
                False,
                detail="psycopg not installed",
                hint="Run `dp config sync`.",
            )
        )

    for name, target in (load_targets() if db_deps_ready else {}).items():
        # Not psycopg.connect directly: a DuckDB target has no server to dial, and
        # probing it over libpq reported a failed check for a configuration that
        # works. db_session already knows which backend an engine needs, and for
        # DuckDB opening the file IS the health check.
        try:
            params = ConnCliParams(target=name).resolve_any()
            if target.engine in LIBPQ_ENGINES:
                kwargs: dict = {
                    **cast(DbConnectionParams, params).as_psycopg_kwargs(),
                    "connect_timeout": 10,
                }
                with psycopg.connect(**kwargs) as conn, conn.cursor() as cursor:
                    cursor.execute("SELECT 1")
            else:
                with db_session(params) as session, session.cursor() as cursor:
                    cursor.execute("SELECT 1")
            results.append(_check(f"{name} connection", True, detail="SELECT 1 ok"))
        except (DataplatError, psycopg.Error) as exc:
            results.append(_check(f"{name} connection", False, detail=str(exc)[:120]))
        except typer.Exit as exc:
            # db_session translates driver failures into an exit code; doctor
            # reports every target rather than stopping at the first bad one.
            results.append(
                _check(
                    f"{name} connection",
                    False,
                    detail=f"could not open the database (exit {exc.exit_code})",
                )
            )

    # Airbyte (only when configured)
    from dataplat.core.errors import AuthError, ConfigError

    if os.getenv("AIRBYTE_BASE_URL"):
        try:
            from dataplat.services.airbyte.client import build_authenticated_client

            client, _ = build_authenticated_client()
            client.close()
            results.append(_check("Airbyte auth", True, detail="token acquired"))
        except ImportError:
            results.append(
                _check(
                    "Airbyte auth",
                    False,
                    detail="ingest dependencies not installed",
                    hint="Run `dp config sync`.",
                )
            )
        except (ConfigError, AuthError) as exc:
            results.append(_check("Airbyte auth", False, detail=str(exc)[:120]))

    # Superset (only when configured)
    if os.getenv("SUPERSET_BASE_URL"):
        from dataplat.core.errors import DataplatError

        try:
            import httpx

            from dataplat.services.superset.client import (
                get_auth_config_from_env,
                login,
            )

            cfg = get_auth_config_from_env()
            with httpx.Client() as client:
                login(client, cfg.base_url, cfg.username, cfg.password)
            results.append(_check("Superset auth", True, detail="login ok"))
        except ImportError:
            results.append(
                _check(
                    "Superset auth",
                    False,
                    detail="bi dependencies not installed",
                    hint="Run `dp config sync`.",
                )
            )
        except DataplatError as exc:
            results.append(_check("Superset auth", False, detail=str(exc)[:120]))

    # Docker daemon
    if shutil.which("docker"):
        proc = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
        )
        results.append(
            _check(
                "docker daemon",
                proc.returncode == 0,
                detail=proc.stdout.strip() if proc.returncode == 0 else "",
                hint="Start Docker." if proc.returncode != 0 else "",
            )
        )

    return results


_MARKS = {
    CheckStatus.ok: "[green]✓[/green]",
    CheckStatus.warn: "[yellow]![/yellow]",
    CheckStatus.fail: "[red]✗[/red]",
}


def _render_checks(results: list[CheckResult]) -> int:
    """Print every result and return how many *failed*; warnings never count."""
    failures = 0
    for r in results:
        # Labels, details and hints carry target names, env var names, paths
        # and driver error text — none of it ours, all of it markup-bearing.
        line = f" {_MARKS[r.status]} {esc(r.label)}"
        if r.detail:
            line += f" [dim]— {esc(r.detail)}[/dim]"
        console.print(line)
        if r.status is not CheckStatus.ok and r.hint:
            console.print(f"    [yellow]{esc(r.hint)}[/yellow]")
        if r.status is CheckStatus.fail:
            failures += 1
    return failures


@app.command("doctor")
def doctor(
    connect: bool = typer.Option(
        False,
        "--connect",
        help="Also run live probes: DB connections, Airbyte/Superset auth, "
        "docker daemon.",
    ),
) -> None:
    """Check that the CLI's configuration and dependencies are healthy."""
    console.print("[bold]Configuration[/bold]")
    results = _offline_checks()
    failures = _render_checks(results)

    if connect:
        console.print()
        console.print("[bold]Connectivity[/bold]")
        connect_results = _connect_checks()
        results += connect_results
        failures += _render_checks(connect_results)

    warnings = sum(1 for r in results if r.status is CheckStatus.warn)
    console.print()
    if failures:
        console.print(f"[red]{failures} check(s) failed.[/red]")
        raise typer.Exit(code=1)
    if warnings:
        console.print(
            f"[green]All checks passed[/green] "
            f"[yellow]({warnings} warning(s) above).[/yellow]"
        )
        return
    console.print("[green]All checks passed.[/green]")
