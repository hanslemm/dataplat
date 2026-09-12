"""Take a person's access away without taking their work with it.

The default disables and revokes; it drops nothing. A dropped role carries the
ownership of everything it owned into the drop with it -- one real account on
one warehouse owns 459 relations -- and no summary a CLI prints makes that
recoverable. Removal stays where it already works: `dp db role drop`, which
handles ownership reassignment, and `dp bi superset users delete`.
"""

from __future__ import annotations

import typer
from rich import box
from rich.console import Console
from rich.table import Table

from dataplat.cli._exit import exit_code_for, fail
from dataplat.cli._options import YesOption
from dataplat.cli._prompt import confirm_or_exit
from dataplat.cli._render import cell
from dataplat.cli.db._common import ConnCliParams, db_session, resolve_params_or_exit
from dataplat.core.errors import (
    AuthError,
    ConfigError,
    ServiceError,
    ValidationError,
)
from dataplat.services.db.role_dialects import ParentKind, dialect_for
from dataplat.services.db.targets import load_targets
from dataplat.services.people.access import db_memberships, db_user_exists
from dataplat.services.people.identity import (
    PersonIdentity,
    parse_identity,
    render_username,
    username_template,
)
from dataplat.services.people.plan import (
    OffboardAccount,
    OffboardFacts,
    OffboardPlan,
    build_offboard_plan,
)
from dataplat.services.superset.client import (
    build_client,
    get_auth_config_from_env,
)
from dataplat.services.superset.client import (
    iter_users as _iter_users,
)
from dataplat.services.superset.client import (
    login as _login,
)
from dataplat.services.superset.client import (
    update_user as _update_user,
)

console = Console()

SUPERSET_SCOPE = "superset"
SUPERSET_DEFAULT_TEMPLATE = "{local}"


def _db_facts(target, identity: PersonIdentity) -> OffboardFacts:
    template = username_template(target.env_prefix)
    if template is None:
        return OffboardFacts(
            scope=target.name,
            template=None,
            username="",
            exists=False,
            memberships=(),
        )

    username = render_username(template, identity)
    params = resolve_params_or_exit(ConnCliParams(target=target.name))
    with db_session(params) as conn:
        cursor = conn.cursor()
        exists = db_user_exists(cursor, params.engine, username)
        memberships = db_memberships(cursor, params.engine, username) if exists else ()

    return OffboardFacts(
        scope=target.name,
        template=template,
        username=username,
        exists=exists,
        memberships=memberships,
    )


def _superset_facts(identity: PersonIdentity) -> OffboardFacts:
    template = username_template("SUPERSET", default=SUPERSET_DEFAULT_TEMPLATE)
    assert template is not None
    username = render_username(template, identity)

    cfg = get_auth_config_from_env()
    with build_client() as client:
        token = _login(client, cfg.base_url, cfg.username, cfg.password)
        users = list(_iter_users(client, cfg.base_url, token))

    match = next(
        (u for u in users if str(u.get("username", "")).lower() == username.lower()),
        None,
    )
    memberships = tuple(
        str(item.get("name"))
        for key in ("roles", "groups")
        for item in (match.get(key) or [] if match else [])
        if item.get("name")
    )

    return OffboardFacts(
        scope=SUPERSET_SCOPE,
        template=template,
        username=username,
        exists=match is not None,
        memberships=memberships,
    )


def render_plan(plan: OffboardPlan) -> None:
    console.print(f"\n[bold]Offboarding[/bold] {cell(plan.identity.email)}\n")

    if plan.accounts:
        table = Table(
            show_header=True,
            header_style="bold cyan",
            box=box.SIMPLE_HEAVY,
            expand=True,
        )
        table.add_column("Area", style="cyan")
        table.add_column("Username")
        table.add_column("Revokes")
        table.add_column("Action", justify="right")
        for account in plan.accounts:
            table.add_row(
                cell(account.scope),
                cell(account.username),
                cell(", ".join(account.memberships) or "—"),
                "[yellow]disable[/yellow]",
            )
        console.print(table)

    for scope, why in plan.skipped:
        console.print(f"[dim]skipped {cell(scope)}: {cell(why)}[/dim]")

    if plan.nothing_to_do:
        console.print("\n[yellow]Nothing to disable[/yellow]")
        return

    console.print(
        "\n[dim]Nothing is dropped. Everything these accounts own keeps its "
        "owner; use `dp db role drop` or `dp bi superset users delete` to "
        "remove them.[/dim]"
    )


def _disable_db_account(target, account: OffboardAccount) -> None:
    params = resolve_params_or_exit(ConnCliParams(target=target.name))
    dialect = dialect_for(params.engine)

    with db_session(params) as conn:
        cursor = conn.cursor()
        # Memberships first, login last: if the revokes fail halfway, the
        # account is still reachable to finish the job by hand. Disabling first
        # would leave a half-revoked account nobody can log into to inspect.
        for parent in account.memberships:
            kind = dialect.resolve_parent_kind(cursor, parent)
            if kind is ParentKind.absent:
                continue
            cursor.execute(
                dialect.revoke_membership(account.username, parent, kind).statement
            )
        cursor.execute(dialect.disable_login(account.username).statement)
        conn.commit()


def _disable_superset_account(account: OffboardAccount) -> None:
    cfg = get_auth_config_from_env()
    with build_client() as client:
        token = _login(client, cfg.base_url, cfg.username, cfg.password)
        match = next(
            (
                u
                for u in _iter_users(client, cfg.base_url, token)
                if str(u.get("username", "")).lower() == account.username.lower()
            ),
            None,
        )
        if match is None:
            raise ServiceError(f"Superset user {account.username!r} disappeared")
        # Only `active`: the roles stay attached so that re-enabling is one
        # flag rather than a reconstruction of what this person used to have.
        _update_user(client, cfg.base_url, token, int(match["id"]), {"active": False})


def offboard(
    email: str = typer.Argument(..., help="Email of the person to offboard"),
    targets: list[str] | None = typer.Option(
        None, "--target", "-t", help="Limit to these DB targets (repeatable)"
    ),
    no_db: bool = typer.Option(False, "--no-db", help="Skip the warehouse targets"),
    no_superset: bool = typer.Option(False, "--no-superset", help="Skip Superset"),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show the plan and change nothing"
    ),
    yes: bool = YesOption,
):
    """Disable a person's access everywhere, dropping nothing."""
    try:
        identity = parse_identity(email)

        configured = load_targets()
        if targets:
            unknown = [n for n in targets if n not in configured]
            if unknown:
                available = ", ".join(sorted(configured)) or "none configured"
                raise ValidationError(
                    f"Unknown target(s): {', '.join(unknown)}. Available: {available}"
                )
            configured = {name: configured[name] for name in targets}

        facts: list[OffboardFacts] = []
        if not no_db:
            for target in configured.values():
                facts.append(_db_facts(target, identity))
        if not no_superset:
            facts.append(_superset_facts(identity))

        plan = build_offboard_plan(identity, facts)
    except (ValidationError, ConfigError, AuthError, ServiceError) as exc:
        fail(exc, console=console)

    render_plan(plan)

    if dry_run or plan.nothing_to_do:
        return

    confirm_or_exit(
        yes=yes,
        prompt=f"\nDisable {len(plan.accounts)} account(s)?",
        console=console,
    )

    failures: list[BaseException] = []
    for account in plan.accounts:
        try:
            if account.scope == SUPERSET_SCOPE:
                _disable_superset_account(account)
            else:
                _disable_db_account(configured[account.scope], account)
        except (AuthError, ConfigError, ServiceError, ValidationError) as exc:
            console.print(f"[red]✗ {cell(account.scope)}: {cell(exc)}[/red]")
            failures.append(exc)
            continue
        console.print(
            f"[green]✓ {cell(account.scope)}[/green] "
            f"[dim]{cell(account.username)}[/dim]"
        )

    if failures:
        raise typer.Exit(code=exit_code_for(failures[0]))
