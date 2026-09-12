"""Give one person the access an existing colleague already has.

Four manual steps in three systems, done by hand today and each with its own
naming convention. What makes this safe to automate is that it invents nothing:
usernames come from a template each target declares for itself, and grants are
copied from a colleague named by ``--like``. Neither convention nor policy is
written down here, because both belong to the company, not to the tool.
"""

from __future__ import annotations

import typer
from rich import box
from rich.console import Console
from rich.table import Table

from dataplat.cli._exit import fail
from dataplat.cli._options import YesOption
from dataplat.cli._render import cell
from dataplat.cli.db._common import ConnCliParams, db_session, resolve_params_or_exit
from dataplat.core.errors import (
    AuthError,
    ConfigError,
    ServiceError,
    ValidationError,
)
from dataplat.services.db.targets import load_targets
from dataplat.services.people.access import (
    db_memberships,
    db_user_exists,
    superset_access,
)
from dataplat.services.people.identity import (
    PersonIdentity,
    parse_identity,
    render_username,
    username_template,
)
from dataplat.services.people.plan import AreaFacts, OnboardPlan, build_onboard_plan
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

    return AreaFacts(
        scope=target.name,
        template=template,
        reference_username=reference_username,
        reference_exists=reference_exists,
        reference_memberships=memberships,
        target_exists=target_exists,
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

    for scope, why in plan.skipped:
        console.print(f"[dim]skipped {cell(scope)}: {cell(why)}[/dim]")

    if plan.nothing_to_do:
        console.print("\n[yellow]Nothing to create[/yellow]")


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
