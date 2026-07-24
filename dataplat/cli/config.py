"""Configuration CLI commands: init, show, doctor."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import typer
from rich import box
from rich.console import Console
from rich.table import Table

from dataplat.core.envrc import CONFIG_ENVRC, find_envrc, link_envrc

app = typer.Typer(
    name="config",
    help="Configuration commands",
    no_args_is_help=True,
)

console = Console()


@dataclass(frozen=True)
class EnvVarSpec:
    """One environment variable the CLI cares about."""

    name: str
    secret: bool = False
    required: bool = True


def _target_specs(prefix: str) -> list[EnvVarSpec]:
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
        components[f"target: {name}"] = _target_specs(target.env_prefix)
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
        console.print(f"[red]Error: {source} not found[/red]")
        raise typer.Exit(code=1)

    if (
        CONFIG_ENVRC.is_symlink() or CONFIG_ENVRC.exists()
    ) and CONFIG_ENVRC.resolve() == source.resolve():
        console.print(f"Already linked: {CONFIG_ENVRC} -> {source}")
        return

    try:
        link_envrc(source)
    except OSError as exc:
        console.print(f"[red]Failed to link envrc: {exc}[/red]")
        raise typer.Exit(code=1)

    console.print(f"Linked: {CONFIG_ENVRC} -> {source}")


def _mask(value: str | None, secret: bool) -> str:
    if not value:
        return "[dim]unset[/dim]"
    if secret:
        return "[green]set[/green] [dim](hidden)[/dim]"
    if len(value) > 60:
        return value[:57] + "…"
    return value


@app.command("show")
def show() -> None:
    """Show the active .envrc and the state of every known env var."""
    active = find_envrc()
    console.print(
        f"[bold]Active .envrc:[/bold] {active if active else '[red]none found[/red]'}"
    )
    if CONFIG_ENVRC.is_symlink():
        console.print(
            f"[bold]Global link:[/bold] {CONFIG_ENVRC} -> {CONFIG_ENVRC.resolve()}"
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

    for component, specs in component_vars().items():
        for i, spec in enumerate(specs):
            table.add_row(
                component if i == 0 else "",
                spec.name,
                _mask(os.getenv(spec.name), spec.secret),
            )

    console.print(table)


@dataclass
class CheckResult:
    label: str
    ok: bool
    detail: str = ""
    hint: str = ""


def _check(label: str, ok: bool, detail: str = "", hint: str = "") -> CheckResult:
    return CheckResult(label=label, ok=ok, detail=detail, hint=hint)


def _offline_checks() -> list[CheckResult]:
    from dataplat.core.errors import ConfigError
    from dataplat.services.db.targets import load_targets

    results: list[CheckResult] = []

    active = find_envrc()
    results.append(
        _check(
            "envrc found",
            active is not None,
            detail=str(active) if active else "",
            hint="Run `dp config init --envrc /path/to/.envrc`.",
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
            for s in _target_specs(target.env_prefix)
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
    import psycopg

    from dataplat.cli.db._common import ConnCliParams
    from dataplat.core.errors import DataplatError
    from dataplat.services.db.targets import load_targets

    for name in load_targets():
        try:
            params = ConnCliParams(target=name).resolve()
            kwargs: dict = {**params.as_psycopg_kwargs(), "connect_timeout": 10}
            with psycopg.connect(**kwargs) as conn, conn.cursor() as cursor:
                cursor.execute("SELECT 1")
            results.append(_check(f"{name} connection", True, detail="SELECT 1 ok"))
        except (DataplatError, psycopg.Error) as exc:
            results.append(
                _check(f"{name} connection", False, detail=str(exc)[:120])
            )

    # Airbyte (only when configured)
    from dataplat.core.errors import AuthError, ConfigError
    from dataplat.services.airbyte.client import build_authenticated_client

    if os.getenv("AIRBYTE_BASE_URL"):
        try:
            client, _ = build_authenticated_client()
            client.close()
            results.append(_check("Airbyte auth", True, detail="token acquired"))
        except (ConfigError, AuthError) as exc:
            results.append(_check("Airbyte auth", False, detail=str(exc)[:120]))

    # Superset (only when configured)
    if os.getenv("SUPERSET_BASE_URL"):
        import httpx

        from dataplat.core.errors import DataplatError
        from dataplat.services.superset.client import (
            get_auth_config_from_env,
            login,
        )

        try:
            cfg = get_auth_config_from_env()
            with httpx.Client() as client:
                login(client, cfg.base_url, cfg.username, cfg.password)
            results.append(_check("Superset auth", True, detail="login ok"))
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


def _render_checks(results: list[CheckResult]) -> int:
    failures = 0
    for r in results:
        mark = "[green]✓[/green]" if r.ok else "[red]✗[/red]"
        line = f" {mark} {r.label}"
        if r.detail:
            line += f" [dim]— {r.detail}[/dim]"
        console.print(line)
        if not r.ok and r.hint:
            console.print(f"    [yellow]{r.hint}[/yellow]")
        if not r.ok:
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
    failures = _render_checks(_offline_checks())

    if connect:
        console.print()
        console.print("[bold]Connectivity[/bold]")
        failures += _render_checks(_connect_checks())

    console.print()
    if failures:
        console.print(f"[red]{failures} check(s) failed.[/red]")
        raise typer.Exit(code=1)
    console.print("[green]All checks passed.[/green]")
