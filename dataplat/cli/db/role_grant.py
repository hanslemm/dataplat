"""``dp db role grant`` — grant existing roles to users/roles in one pass.

``role create`` can wire up membership for a role it is creating; this is the
command for every day after that, when the role already exists and a new
analyst needs it. It takes the cross product of ``--roles`` and ``--to``, so
onboarding three people to two roles is one invocation rather than six.

Three properties are worth knowing before using it:

- **It validates the whole plan before executing any of it.** A typo in the
  last ``--to`` fails before the first user is created.
- **It reports grants that are already in effect** instead of re-issuing them,
  so the plan shows what actually changes. Re-granting is harmless on both
  engines; the point is the operator reading the output.
- **It refuses combinations the engine cannot express** — a Redshift group
  holds login users only, and there is no ``GRANT ROLE ... TO GROUP`` form — by
  name, rather than letting them surface as a raw SQL error mid-batch.

``--create-missing-users`` creates any ``--to`` name that does not exist yet as
a login user with a generated password, written to the same credentials CSV
``role create`` uses (``0600``, under ``~/.config/dataplat/credentials/``).
Creates and grants share one transaction, so a failed grant leaves no
half-onboarded user behind — which is also why the CSV is written after the
commit rather than during it, unlike ``role create``, whose stages commit
separately and so must record each password the moment its role exists.
"""

from __future__ import annotations

import csv
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import typer
from rich.console import Console

from dataplat.cli._exit import fail
from dataplat.cli._options import YesOption
from dataplat.cli._prompt import confirm_or_exit
from dataplat.cli._render import cell, esc
from dataplat.cli.db._common import (
    ConnCliParams,
    DatabaseOption,
    EngineOption,
    EnvPrefixOption,
    HostOption,
    PasswordOption,
    PortOption,
    SslmodeOption,
    TargetOption,
    UserOption,
    db_session,
    engine_or_exit,
    resolve_params_or_exit,
)
from dataplat.cli.db._credentials import (
    credentials_default_path,
    file_mode_secure,
    open_credentials_file,
)
from dataplat.cli.db._grantees import (  # noqa: F401  (re-export)
    GranteeKind,
    parent_kind_for,
)
from dataplat.core.errors import ValidationError
from dataplat.services.db.capabilities import Capability, require_capability
from dataplat.services.db.connection import SqlEngine
from dataplat.services.db.role_admin import (
    GrantPlan,
    build_grant_plan,
    generate_password,
    parse_csv_flag,
    resolve_grantee_kinds,
)
from dataplat.services.db.role_dialects import ParentKind, dialect_for


def _render_plan(console: Console, plan: GrantPlan) -> None:
    """Print what the plan will do. Names are escaped — they come from a catalog."""
    if plan.creates:
        console.print(f"[bold]Create login user(s)[/bold] ({len(plan.creates)}):")
        for name in plan.creates:
            console.print(f"  [green]+[/green] {esc(name)}")
    console.print(f"[bold]Grant(s)[/bold] ({len(plan.grants)}):")
    for pair in plan.grants:
        console.print(
            f"  {esc(pair.role)} [dim]({pair.role_kind.value})[/dim]"
            f" → {esc(pair.target)} [dim]({pair.target_kind.value})[/dim]"
        )
    if plan.already_held:
        console.print(f"[dim]Already held ({len(plan.already_held)}), skipped:[/dim]")
        for pair in plan.already_held:
            console.print(f"  [dim]{esc(pair.role)} → {esc(pair.target)}[/dim]")


def grant_command(
    roles: list[str] = typer.Option(
        ...,
        "--roles",
        help="Roles/groups to grant. Repeatable / comma-separated.",
    ),
    to: list[str] = typer.Option(
        ...,
        "--to",
        help="Users/roles that should receive them. Repeatable / comma-separated.",
    ),
    create_missing_users: bool = typer.Option(
        False,
        "--create-missing-users",
        help="Create any --to name that does not exist yet as a login user "
        "with a generated password.",
    ),
    kind: GranteeKind | None = typer.Option(
        None,
        "--kind",
        help="Disambiguate a --roles name that exists as more than one object.",
    ),
    to_kind: GranteeKind | None = typer.Option(
        None,
        "--to-kind",
        help="Disambiguate a --to name that exists as more than one object.",
    ),
    credentials_out: Path | None = typer.Option(
        None,
        "--credentials-out",
        help="CSV file to append generated credentials to. Default: a timestamped "
        "file under ~/.config/dataplat/credentials/ — not the current "
        "directory, which is usually a checkout.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print the plan and exit without executing it.",
    ),
    yes: bool = YesOption,
    target: str | None = TargetOption,
    engine: SqlEngine | None = EngineOption,
    user: str | None = UserOption,
    password: str | None = PasswordOption,
    database: str | None = DatabaseOption,
    host: str | None = HostOption,
    port: int | None = PortOption,
    sslmode: str | None = SslmodeOption,
    env_prefix: str | None = EnvPrefixOption,
) -> None:
    """Entry point for ``dp db role grant``."""
    console = Console()

    conn_cli = ConnCliParams(
        target=target,
        engine=engine,
        user=user,
        password=password,
        database=database,
        host=host,
        port=port,
        sslmode=sslmode,
        env_prefix=env_prefix,
    )
    # Refuse before resolving or opening anything, so the engine names the
    # reason. See the note in role.py for why each role command spells this out;
    # it matters most on the writing ones, and this command can create a user.
    try:
        require_capability(
            engine_or_exit(conn_cli), Capability.roles, command="dp db role grant"
        )
    except ValidationError as exc:
        fail(exc, console=console)
    conn_params = resolve_params_or_exit(conn_cli)

    role_names = parse_csv_flag(roles)
    target_names = parse_csv_flag(to)
    dialect = dialect_for(conn_params.engine)
    forced = parent_kind_for(kind)
    forced_to = parent_kind_for(to_kind)

    # Resolved lazily, inside the branch that actually needs it: the default
    # path creates ~/.config/dataplat/credentials/ as a side effect, and a
    # --dry-run or a grant to existing users generates no secrets to put there.
    creds_path: Path | None = None
    created: list[tuple[str, str]] = []
    plan: GrantPlan | None = None
    creds_file: Any = None
    creds_is_new = False

    try:
        with db_session(conn_params) as conn, conn.cursor() as cursor:
            role_kinds = resolve_grantee_kinds(
                dialect, cursor, role_names, forced, flag="--kind"
            )
            target_kinds = resolve_grantee_kinds(
                dialect, cursor, target_names, forced_to, flag="--to-kind"
            )
            plan = build_grant_plan(
                roles=role_kinds,
                targets=target_kinds,
                held=dialect.held_grants(cursor, role_names),
                create_missing_users=create_missing_users,
                dialect=dialect,
            )

            _render_plan(console, plan)
            if not plan.grants and not plan.creates:
                console.print("\n[dim]Nothing to do.[/dim]")
                return
            if dry_run:
                console.print("\n[yellow]Dry-run; no SQL executed.[/yellow]")
                return

            confirm_or_exit(yes=yes, prompt="\nProceed?", console=console)

            # Open before executing any DDL: a bad --credentials-out path should
            # fail here, not after users are committed with passwords that were
            # never written anywhere. Same reasoning as role_create.
            if plan.creates:
                creds_path = credentials_out or credentials_default_path()
                creds_file, creds_is_new = open_credentials_file(creds_path)

            try:
                for name in plan.creates:
                    secret = generate_password()
                    cursor.execute(dialect.create_login(name, secret).statement)
                    created.append((name, secret))
                for pair in plan.grants:
                    op = dialect.grant_membership(
                        pair.target,
                        pair.role,
                        pair.role_kind,
                        member_is_role=pair.target_kind is ParentKind.role,
                    )
                    cursor.execute(op.statement)
            except BaseException:
                # Close the handle on the way out — including for typer.Exit,
                # which db_session raises for a psycopg error and which is not
                # an Exception subclass everywhere it matters.
                if creds_file is not None:
                    creds_file.close()
                    creds_file = None
                raise
        # Committed. Only now are the passwords worth recording: every path that
        # reaches here executed cleanly, and every path that does not rolled the
        # CREATEs back, so the CSV can never describe a user that never existed.
    except ValidationError as exc:
        fail(exc, console=console)

    assert plan is not None  # every other path above returned or raised

    # creds_path and creds_file are both set whenever plan.creates was, which is
    # the only way `created` is non-empty; the check keeps that implicit for the
    # type checker rather than asserting it.
    if created and creds_path is not None:
        try:
            writer = csv.writer(creds_file)
            if creds_is_new:
                writer.writerow(["username", "password", "created_at", "databases"])
            created_at = datetime.now(UTC).isoformat(timespec="seconds")
            for name, secret in created:
                # No database column: this command grants cluster-wide role
                # membership and touches no per-database privileges.
                writer.writerow([name, secret, created_at, ""])
        finally:
            if creds_file is not None:
                creds_file.close()
        console.print(f"\n[green]Wrote credentials to[/green] {esc(creds_path)}")
        if not file_mode_secure(creds_path):
            console.print(
                f"[yellow]Warning: {esc(creds_path)} is readable by group/other. "
                f"Run: chmod 600 {esc(creds_path)}[/yellow]"
            )

    console.print(
        f"\n[green]Done:[/green] {len(plan.grants)} grant(s), "
        f"{len(plan.creates)} user(s) created"
        + (f", {len(plan.already_held)} already held" if plan.already_held else "")
    )
    if created:
        console.print(
            "[dim]New users have no privileges beyond the role(s) just "
            f"granted: {cell(', '.join(n for n, _ in created))}[/dim]"
        )
