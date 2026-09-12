"""Give one person the access an existing colleague already has.

Four manual steps in three systems, done by hand today and each with its own
naming convention. What makes this safe to automate is that it invents nothing:
usernames come from a template each target declares for itself, and grants are
copied from a colleague named by ``--like``. Neither convention nor policy is
written down here, because both belong to the company, not to the tool.
"""

from __future__ import annotations

import csv
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import typer
from rich import box
from rich.console import Console
from rich.table import Table

from dataplat.cli._credentials import (
    credentials_default_path,
    file_mode_secure,
    generate_password,
    open_credentials_file,
)
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
from dataplat.services.db.role_admin import CreateRoleSpec, build_create_plan
from dataplat.services.db.role_dialects import ParentKind, dialect_for
from dataplat.services.db.targets import load_targets
from dataplat.services.people.access import (
    db_membership_holders,
    db_memberships,
    db_user_exists,
    superset_access,
    superset_membership_holders,
)
from dataplat.services.people.identity import (
    PersonIdentity,
    parse_identity,
    render_username,
    username_template,
)
from dataplat.services.people.plan import (
    AreaAccount,
    AreaFacts,
    OnboardPlan,
    build_onboard_plan,
)
from dataplat.services.superset.client import (
    build_client,
    get_auth_config_from_env,
)
from dataplat.services.superset.client import (
    create_user as _create_user,
)
from dataplat.services.superset.client import (
    iter_groups as _iter_groups,
)
from dataplat.services.superset.client import (
    iter_roles as _iter_roles,
)
from dataplat.services.superset.client import (
    iter_users as _iter_users,
)
from dataplat.services.superset.client import (
    login as _login,
)
from dataplat.services.superset.client import (
    resolve_group_ids as _resolve_group_ids,
)
from dataplat.services.superset.client import (
    resolve_role_ids as _resolve_role_ids,
)

console = Console()

SUPERSET_SCOPE = "superset"
# Superset usernames are the email name far more often than not, so this is the
# one area with a working default. A warehouse gets no default on purpose:
# guessing a role name is how an account lands under the wrong convention.
SUPERSET_DEFAULT_TEMPLATE = "{local}"


def _selected_targets(names: list[str] | None) -> dict:
    """The configured targets, narrowed by ``--target`` and never widened."""
    targets = load_targets()
    if not names:
        return targets

    unknown = [n for n in names if n not in targets]
    if unknown:
        available = ", ".join(sorted(targets)) or "none configured"
        raise ValidationError(
            f"Unknown target(s): {', '.join(unknown)}. Available: {available}"
        )
    return {name: targets[name] for name in names}


def _db_facts(target, identity: PersonIdentity, reference: PersonIdentity) -> AreaFacts:
    """Ask one warehouse what it knows, or skip it without connecting."""
    template = username_template(target.env_prefix)
    if template is None:
        # No connection is opened: a target that declares no convention has
        # nothing to say about people, and asking it anyway would make an
        # opt-out cost a round trip and a possible auth failure.
        return AreaFacts(
            scope=target.name,
            template=None,
            reference_username="",
            reference_exists=False,
            reference_memberships=(),
            target_exists=False,
        )

    reference_username = render_username(template, reference)
    new_username = render_username(template, identity)

    params = resolve_params_or_exit(ConnCliParams(target=target.name))
    with db_session(params) as conn:
        cursor = conn.cursor()
        reference_exists = db_user_exists(cursor, params.engine, reference_username)
        memberships = (
            db_memberships(cursor, params.engine, reference_username)
            if reference_exists
            else ()
        )
        target_exists = db_user_exists(cursor, params.engine, new_username)
        holders, population = db_membership_holders(cursor, params.engine)

    return AreaFacts(
        scope=target.name,
        template=template,
        reference_username=reference_username,
        reference_exists=reference_exists,
        reference_memberships=memberships,
        target_exists=target_exists,
        holders=holders,
        population=population,
    )


def _superset_facts(identity: PersonIdentity, reference: PersonIdentity) -> AreaFacts:
    """Ask Superset the same three questions the warehouses were asked."""
    template = username_template("SUPERSET", default=SUPERSET_DEFAULT_TEMPLATE)
    assert template is not None  # a default is always supplied
    reference_username = render_username(template, reference)
    new_username = render_username(template, identity)

    cfg = get_auth_config_from_env()
    with build_client() as client:
        token = _login(client, cfg.base_url, cfg.username, cfg.password)
        users = list(_iter_users(client, cfg.base_url, token))

    access = superset_access(users, reference_username)
    target_exists = superset_access(users, new_username) is not None
    holders, population = superset_membership_holders(users)

    return AreaFacts(
        scope=SUPERSET_SCOPE,
        template=template,
        reference_username=reference_username,
        reference_exists=access is not None,
        reference_memberships=(
            *(access[0] if access else ()),
            *(access[1] if access else ()),
        ),
        target_exists=target_exists,
        holders=holders,
        population=population,
    )


def render_plan(plan: OnboardPlan) -> None:
    """Show the whole intent at once, because nothing else ever will.

    Three systems with no transaction between them: once execution starts, the
    only complete picture of what was meant to happen is this table.
    """
    console.print(
        f"\n[bold]Onboarding[/bold] {cell(plan.identity.email)} "
        f"[dim]copying[/dim] {cell(plan.reference.email)}\n"
    )

    if plan.accounts:
        table = Table(
            show_header=True,
            header_style="bold cyan",
            box=box.SIMPLE_HEAVY,
            expand=True,
        )
        table.add_column("Area", style="cyan")
        table.add_column("Username")
        table.add_column("Copies")
        table.add_column("Status", justify="right")
        for account in plan.accounts:
            table.add_row(
                cell(account.scope),
                cell(account.username),
                cell(", ".join(account.memberships) or "—"),
                "[yellow]exists[/yellow]"
                if account.already_exists
                else "[green]create[/green]",
            )
        console.print(table)

    # Copying a colleague copies their privilege level, and the person running
    # this reads a username and a status -- not a membership list. Only for
    # accounts that would actually be created: warning about an account nobody
    # is touching is noise, and noise is how a warning stops being read.
    for account in plan.to_create:
        if not account.unusual:
            continue
        it = "it" if len(account.unusual) == 1 else "them"
        rare = ", ".join(
            f"{name} ({held} of {account.population} accounts)"
            for name, held in account.unusual
        )
        console.print(
            f"[yellow]![/yellow] {cell(account.scope)}: "
            f"{cell(account.username)} would receive "
            f"[yellow]{cell(rare)}[/yellow] — few accounts here have {it}, "
            f"so check {it} is intended."
        )

    for scope, why in plan.skipped:
        console.print(f"[dim]skipped {cell(scope)}: {cell(why)}[/dim]")

    if plan.nothing_to_do:
        console.print("\n[yellow]Nothing to create[/yellow]")


def _create_db_account(target, account: AreaAccount, password: str) -> None:
    """Create one warehouse account with the memberships the plan copied.

    Composes what `dp db role create` composes -- CreateRoleSpec through
    build_create_plan -- rather than writing SQL here. Onboarding grants only
    memberships, so every statement is cluster-level and commits together: the
    account and its access exist at the same instant, or neither does.
    """
    params = resolve_params_or_exit(ConnCliParams(target=target.name))
    dialect = dialect_for(params.engine)

    with db_session(params) as conn:
        cursor = conn.cursor()
        # The parents came from the reference user on this same target, so they
        # exist; their *kind* is what Redshift needs and only the catalog knows.
        parent_kinds = {}
        for parent in account.memberships:
            kind = dialect.resolve_parent_kind(cursor, parent)
            if kind is not ParentKind.absent:
                parent_kinds[parent] = kind

        spec = CreateRoleSpec(
            name=account.username,
            password=password,
            member_of=tuple(parent_kinds),
        )
        plan = build_create_plan(
            spec,
            databases=[params.dbname],
            dialect=dialect,
            parent_kinds=parent_kinds,
        )
        for op in plan.cluster_ops:
            cursor.execute(op.statement)
        conn.commit()


def _create_superset_account(
    identity: PersonIdentity, account: AreaAccount, password: str
) -> None:
    """Create the Superset user, resolving the copied names to ids.

    The plan carries names because a person reads it; the API takes ids. Which
    names are roles and which are groups is Superset's own business, so it is
    settled here by asking both catalogs rather than by splitting the plan.
    """
    cfg = get_auth_config_from_env()
    with build_client() as client:
        token = _login(client, cfg.base_url, cfg.username, cfg.password)

        role_names = {
            str(r.get("name")) for r in _iter_roles(client, cfg.base_url, token)
        }
        group_names = {
            str(g.get("name")) for g in _iter_groups(client, cfg.base_url, token)
        }
        wanted_roles = [m for m in account.memberships if m in role_names]
        wanted_groups = [
            m for m in account.memberships if m in group_names and m not in role_names
        ]

        payload: dict = {
            "username": account.username,
            "first_name": (identity.first or account.username).title(),
            "last_name": (identity.last or "User").title(),
            "email": identity.email,
            "password": password,
            "active": True,
            "roles": _resolve_role_ids(client, cfg.base_url, token, wanted_roles),
        }
        if wanted_groups:
            payload["groups"] = _resolve_group_ids(
                client, cfg.base_url, token, wanted_groups
            )
        _create_user(client, cfg.base_url, token, payload)


def _execute(plan: OnboardPlan, targets: dict, path: Path) -> list[BaseException]:
    """Create every planned account, recording each credential as it lands.

    No rollback across areas. Three systems with no transaction between them
    means a failure in the third cannot undo the first two, and pretending
    otherwise would leave the operator with a summary that is not true. What
    succeeded is kept, recorded, and reported; the exit code comes from the
    first failure.
    """
    failures: list[BaseException] = []
    creds_file, is_new = open_credentials_file(path)
    try:
        writer = csv.writer(creds_file)
        if is_new:
            writer.writerow(["username", "password", "created_at", "scope"])

        for account in plan.to_create:
            # One password per area: three systems that can be compromised
            # separately should not share a secret.
            password = generate_password()
            try:
                if account.scope == SUPERSET_SCOPE:
                    _create_superset_account(plan.identity, account, password)
                else:
                    _create_db_account(targets[account.scope], account, password)
            except (
                AuthError,
                ConfigError,
                ServiceError,
                ValidationError,
                psycopg.Error,
                RuntimeError,
                ValueError,
            ) as exc:
                console.print(f"[red]✗ {cell(account.scope)}: {cell(exc)}[/red]")
                failures.append(exc)
                continue

            writer.writerow(
                [
                    account.username,
                    password,
                    datetime.now(UTC).isoformat(timespec="seconds"),
                    account.scope,
                ]
            )
            creds_file.flush()
            console.print(
                f"[green]✓ {cell(account.scope)}[/green] "
                f"[dim]{cell(account.username)}[/dim]"
            )
    finally:
        creds_file.close()
    return failures


def onboard(
    email: str = typer.Argument(..., help="Email of the person to onboard"),
    like: str = typer.Option(
        ...,
        "--like",
        "-l",
        help="Email of an existing person whose access to copy, per area.",
    ),
    first_name: str | None = typer.Option(
        None, "--first-name", help="Overrides the name derived from the email"
    ),
    last_name: str | None = typer.Option(
        None, "--last-name", help="Overrides the name derived from the email"
    ),
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
    """Create a person's access across every configured area, copying --like."""
    try:
        identity = parse_identity(email, first_name, last_name)
        reference = parse_identity(like)

        facts: list[AreaFacts] = []
        if not no_db:
            for target in _selected_targets(targets).values():
                facts.append(_db_facts(target, identity, reference))
        if not no_superset:
            facts.append(_superset_facts(identity, reference))

        plan = build_onboard_plan(identity, reference, facts)
    except (ValidationError, ConfigError, AuthError, ServiceError) as exc:
        fail(exc, console=console)

    render_plan(plan)

    if dry_run or plan.nothing_to_do:
        return

    confirm_or_exit(
        yes=yes,
        prompt=f"\nCreate {len(plan.to_create)} account(s)?",
        console=console,
    )

    path = credentials_default_path(prefix="dp-onboard")
    failures = _execute(
        plan, {t.name: t for t in _selected_targets(targets).values()}, path
    )

    console.print(f"\n[dim]Credentials written to {cell(path)}[/dim]")
    if not file_mode_secure(path):
        console.print(
            f"[yellow]![/yellow] [dim]{cell(path)} is readable by others; "
            "chmod 600 it.[/dim]"
        )

    if failures:
        raise typer.Exit(code=exit_code_for(failures[0]))
