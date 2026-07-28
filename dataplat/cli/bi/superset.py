"""Superset management commands."""

from __future__ import annotations

import json
from enum import Enum

import typer
from rich import box
from rich.console import Console
from rich.table import Table

from dataplat.cli._exit import exit_code_for, fail
from dataplat.cli._options import JsonOption, YesOption
from dataplat.cli._prompt import confirm_or_exit
from dataplat.cli._render import cell, esc
from dataplat.core.errors import AuthError, ConfigError, ServiceError
from dataplat.services.superset.client import (
    build_client,
    get_auth_config_from_env,
)
from dataplat.services.superset.client import (
    create_user as _create_user,
)
from dataplat.services.superset.client import (
    delete_user as _delete_user,
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
from dataplat.services.superset.client import (
    update_user as _update_user,
)
from dataplat.services.superset.client import (
    user_group_ids as _user_group_ids,
)
from dataplat.services.superset.client import (
    user_role_ids as _user_role_ids,
)

app = typer.Typer(
    name="superset",
    help="Manage Superset resources",
    no_args_is_help=True,
)

users_app = typer.Typer(
    name="users",
    help="Manage Superset users",
    no_args_is_help=True,
)

roles_app = typer.Typer(
    name="roles",
    help="Manage Superset roles",
    no_args_is_help=True,
)

groups_app = typer.Typer(
    name="groups",
    help="Inspect Superset groups",
    no_args_is_help=True,
)

app.add_typer(users_app, name="users")
app.add_typer(roles_app, name="roles")
app.add_typer(groups_app, name="groups")

console = Console()


class RoleOrder(str, Enum):
    """Role list ordering options."""

    by_name = "name"
    by_id = "id"


class RoleOrderDir(str, Enum):
    """Role list ordering direction options."""

    asc = "asc"
    desc = "desc"


class UserRoleMatch(str, Enum):
    """How to match users by role set."""

    exact = "exact"
    subset = "subset"
    any = "any"


def _load_auth_context() -> tuple[str, str, str]:
    try:
        cfg = get_auth_config_from_env()
    except ConfigError as exc:
        fail(exc, console=console)
    return cfg.base_url, cfg.username, cfg.password


@roles_app.command("list")
def list_roles(
    order_by: RoleOrder = typer.Option(
        RoleOrder.by_name,
        "--order",
        help="Order roles by name or id",
    ),
    order_dir: RoleOrderDir = typer.Option(
        RoleOrderDir.asc,
        "--order-dir",
        help="Sort direction (asc or desc)",
    ),
    as_json: bool = JsonOption,
):
    """List available Superset roles."""
    base_url, admin_username, admin_password = _load_auth_context()

    try:
        with build_client() as client:
            access_token = _login(client, base_url, admin_username, admin_password)
            roles = list(_iter_roles(client, base_url, access_token))
    except (AuthError, ServiceError, ConfigError) as exc:
        fail(exc, console=console)

    if as_json:
        typer.echo(json.dumps(roles, indent=2, ensure_ascii=False))
        return

    if not roles:
        console.print("[yellow]No roles found[/yellow]")
        return

    table = Table(
        show_header=True,
        header_style="bold cyan",
        box=box.SIMPLE_HEAVY,
        show_lines=False,
        expand=True,
    )
    table.add_column("ID", style="dim", no_wrap=True)
    table.add_column("Name", style="cyan")

    def _sort_by_id(role: dict) -> int:
        return int(role.get("id") or 0)

    def _sort_by_name(role: dict) -> str:
        return str(role.get("name", ""))

    sort_key = _sort_by_id if order_by == RoleOrder.by_id else _sort_by_name
    reverse = order_dir == RoleOrderDir.desc

    for role in sorted(roles, key=sort_key, reverse=reverse):
        role_id = role.get("id")
        name = role.get("name", "")
        table.add_row(cell(role_id), cell(name))

    console.print(table)


@users_app.command("create")
def create_user(
    username: str = typer.Argument(..., help="Superset username to create"),
    password: str = typer.Option(
        ...,
        "--password",
        help="Password for the new user (omit to be prompted without echo).",
        prompt=True,
        hide_input=True,
        confirmation_prompt=True,
    ),
    email: str = typer.Option(..., "--email", "-e", help="Email for the new user"),
    first_name: str | None = typer.Option(
        None, "--first-name", help="First name for the new user"
    ),
    last_name: str | None = typer.Option(
        None, "--last-name", help="Last name for the new user"
    ),
    role_name: list[str] | None = typer.Option(
        None,
        "--role",
        "-r",
        help="Role name to assign (can be repeated). Defaults to Gamma",
    ),
    group_name: list[str] | None = typer.Option(
        None,
        "--group",
        "-g",
        help="Group name to assign (can be repeated)",
    ),
    active: bool = typer.Option(True, "--active/--inactive", help="User is active"),
):
    """Create a Superset user."""
    base_url, admin_username, admin_password = _load_auth_context()

    resolved_email = email
    resolved_first_name = first_name or username
    resolved_last_name = last_name or "User"
    role_names = role_name if role_name else ["Gamma"]
    group_names = group_name if group_name else []

    try:
        with build_client() as client:
            access_token = _login(client, base_url, admin_username, admin_password)
            role_ids = _resolve_role_ids(client, base_url, access_token, role_names)
            payload = {
                "username": username,
                "first_name": resolved_first_name,
                "last_name": resolved_last_name,
                "email": resolved_email,
                "password": password,
                "active": active,
                "roles": role_ids,
            }
            if group_names:
                group_ids = _resolve_group_ids(
                    client, base_url, access_token, group_names
                )
                payload["groups"] = group_ids

            response = _create_user(client, base_url, access_token, payload)
    except (AuthError, ServiceError, ConfigError) as exc:
        fail(exc, console=console)

    user_id = response.get("id") or response.get("result", {}).get("id")
    console.print(
        "[green]✓ Superset user created[/green] "
        f"[dim](username={esc(username)}, id={esc(user_id)})[/dim]"
    )


@users_app.command("update")
def update_users(
    user_ids: list[int] | None = typer.Option(
        None,
        "--user-id",
        help="Only update specific user id(s) (repeatable)",
    ),
    email: list[str] | None = typer.Option(
        None,
        "--email",
        help="Only update specific user email(s) (repeatable)",
    ),
    filter_role: list[str] | None = typer.Option(
        None,
        "--filter-role",
        help="Only update users with these roles (repeatable)",
    ),
    match: UserRoleMatch = typer.Option(
        UserRoleMatch.exact,
        "--match",
        help="Role match mode for filtering (exact, subset, any)",
    ),
    add_group: list[str] | None = typer.Option(
        None,
        "--add-group",
        help="Group to add (repeatable)",
    ),
    remove_group: list[str] | None = typer.Option(
        None,
        "--remove-group",
        help="Group to remove (repeatable)",
    ),
    set_group: list[str] | None = typer.Option(
        None,
        "--set-group",
        help="Replace groups with this list (repeatable)",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Preview changes without updating users",
    ),
):
    """Update users in bulk based on role filters and group changes."""
    base_url, admin_username, admin_password = _load_auth_context()

    if not any([add_group, remove_group, set_group]):
        console.print("[red]Error: specify at least one group update flag[/red]")
        raise typer.Exit(code=1)

    if set_group and (add_group or remove_group):
        console.print(
            "[red]Error: --set-group cannot be combined with "
            "--add-group or --remove-group[/red]"
        )
        raise typer.Exit(code=1)

    user_id_set = set(user_ids or [])
    email_set = {e.lower() for e in (email or [])}

    try:
        with build_client() as client:
            access_token = _login(client, base_url, admin_username, admin_password)

            filter_role_ids = (
                _resolve_role_ids(client, base_url, access_token, filter_role)
                if filter_role
                else []
            )
            add_group_ids = (
                _resolve_group_ids(client, base_url, access_token, add_group)
                if add_group
                else []
            )
            remove_group_ids = (
                _resolve_group_ids(client, base_url, access_token, remove_group)
                if remove_group
                else []
            )
            set_group_ids = (
                _resolve_group_ids(client, base_url, access_token, set_group)
                if set_group
                else []
            )

            matched = 0
            updated = 0

            for user in _iter_users(client, base_url, access_token):
                user_id = user.get("id")
                if not isinstance(user_id, int):
                    continue

                if user_id_set and user_id not in user_id_set:
                    continue

                if email_set:
                    user_email = user.get("email")
                    if not isinstance(user_email, str):
                        continue
                    if user_email.lower() not in email_set:
                        continue

                user_roles = _user_role_ids(user)
                if filter_role_ids:
                    if not user_roles:
                        continue
                    user_roles_set = set(user_roles)
                    filter_roles_set = set(filter_role_ids)
                    if match == UserRoleMatch.exact:
                        if user_roles_set != filter_roles_set:
                            continue
                    elif match == UserRoleMatch.subset:
                        if not user_roles_set.issubset(filter_roles_set):
                            continue
                    else:
                        if not user_roles_set.intersection(filter_roles_set):
                            continue

                user_groups = _user_group_ids(user)
                updated_groups = user_groups

                if set_group is not None:
                    updated_groups = list(set_group_ids)
                else:
                    updated_groups = list(set(user_groups).union(add_group_ids))
                    if remove_group_ids:
                        updated_groups = [
                            gid for gid in updated_groups if gid not in remove_group_ids
                        ]

                if set(updated_groups) == set(user_groups):
                    continue

                matched += 1
                if dry_run:
                    continue

                payload = {
                    "roles": user_roles,
                    "groups": updated_groups,
                }
                _update_user(client, base_url, access_token, user_id, payload)
                updated += 1
    except (AuthError, ServiceError, ConfigError) as exc:
        fail(exc, console=console)

    if matched == 0:
        console.print("[yellow]No users matched the criteria[/yellow]")
        return

    if dry_run:
        console.print(
            f"[green]Matched {matched} user(s)[/green] (dry run; no updates applied)"
        )
        return

    console.print(f"[green]Updated {updated} user(s)[/green]")


@users_app.command("list")
def list_users(
    filter_role: list[str] | None = typer.Option(
        None,
        "--filter-role",
        help="Only show users holding this role (repeatable).",
    ),
    as_json: bool = JsonOption,
):
    """List Superset users."""
    base_url, admin_username, admin_password = _load_auth_context()

    try:
        with build_client() as client:
            access_token = _login(client, base_url, admin_username, admin_password)
            users = list(_iter_users(client, base_url, access_token))
            role_filter_ids: set[int] = set()
            if filter_role:
                role_filter_ids = set(
                    _resolve_role_ids(client, base_url, access_token, filter_role)
                )
    except (AuthError, ServiceError, ConfigError) as exc:
        fail(exc, console=console)

    if role_filter_ids:
        users = [u for u in users if role_filter_ids & set(_user_role_ids(u))]

    if as_json:
        typer.echo(json.dumps(users, indent=2, ensure_ascii=False))
        return

    if not users:
        console.print("[yellow]No users found[/yellow]")
        return

    table = Table(
        show_header=True,
        header_style="bold cyan",
        box=box.SIMPLE_HEAVY,
        expand=True,
    )
    table.add_column("ID", style="dim", no_wrap=True)
    table.add_column("Username", style="cyan")
    table.add_column("Name")
    table.add_column("Email")
    table.add_column("Active", justify="center")
    table.add_column("Roles", style="dim")

    for user in sorted(users, key=lambda u: str(u.get("username", ""))):
        roles = user.get("roles") or []
        role_names = ", ".join(
            str(r.get("name", r)) if isinstance(r, dict) else str(r) for r in roles
        )
        full_name = " ".join(
            part for part in (user.get("first_name"), user.get("last_name")) if part
        )
        table.add_row(
            cell(user.get("id", "")),
            cell(user.get("username", "")),
            cell(full_name),
            cell(user.get("email", "")),
            "yes" if user.get("active") else "no",
            cell(role_names),
        )

    console.print(table)
    console.print(f"\n[dim]Total: {len(users)} user(s)[/dim]")


@users_app.command("delete")
def delete_users(
    user_ids: list[int] = typer.Argument(..., help="Superset user ID(s) to delete"),
    yes: bool = YesOption,
):
    """Delete Superset user(s) by ID."""
    # Typer parsed the ids as ints, so the prompt needs no markup escaping.
    confirm_or_exit(
        yes=yes,
        prompt=f"Delete Superset user(s) {', '.join(str(u) for u in user_ids)}?",
    )

    base_url, admin_username, admin_password = _load_auth_context()

    # Kept as the exceptions, not a counter: this loop deliberately survives a
    # per-user failure so the other ids still get deleted, which means fail()
    # cannot be used — it exits. Holding the errors lets the exit code still
    # come from them rather than from a literal that would report a 404 from
    # Superset as an unclassified failure.
    failures: list[ServiceError] = []
    try:
        with build_client() as client:
            access_token = _login(client, base_url, admin_username, admin_password)
            for user_id in user_ids:
                try:
                    _delete_user(client, base_url, access_token, user_id)
                    console.print(f"[green]✓ Deleted user {user_id}[/green]")
                except ServiceError as exc:
                    console.print(f"[red]✗ user {user_id}: {esc(exc)}[/red]")
                    failures.append(exc)
    except (AuthError, ConfigError) as exc:
        fail(exc, console=console)

    if failures:
        raise typer.Exit(code=exit_code_for(failures[0]))


@groups_app.command("list")
def list_groups(
    as_json: bool = JsonOption,
):
    """List Superset groups."""
    base_url, admin_username, admin_password = _load_auth_context()

    try:
        with build_client() as client:
            access_token = _login(client, base_url, admin_username, admin_password)
            groups = list(_iter_groups(client, base_url, access_token))
    except (AuthError, ServiceError, ConfigError) as exc:
        fail(exc, console=console)

    if as_json:
        typer.echo(json.dumps(groups, indent=2, ensure_ascii=False))
        return

    if not groups:
        console.print("[yellow]No groups found[/yellow]")
        return

    table = Table(
        show_header=True,
        header_style="bold cyan",
        box=box.SIMPLE_HEAVY,
        expand=True,
    )
    table.add_column("ID", style="dim", no_wrap=True)
    table.add_column("Name", style="cyan")
    table.add_column("Label")
    table.add_column("Description")

    for group in sorted(groups, key=lambda g: str(g.get("name", ""))):
        table.add_row(
            cell(group.get("id", "")),
            cell(group.get("name", "")),
            cell(group.get("label", "") or ""),
            cell(group.get("description", "") or ""),
        )

    console.print(table)
    console.print(f"\n[dim]Total: {len(groups)} group(s)[/dim]")
