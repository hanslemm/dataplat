"""Superset management commands."""

from __future__ import annotations

import csv
import json
from collections.abc import Iterable
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path

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
from dataplat.cli._options import JsonOption, YesOption
from dataplat.cli._prompt import confirm_or_exit
from dataplat.cli._render import cell, esc
from dataplat.core.errors import (
    AuthError,
    ConfigError,
    ServiceError,
    ValidationError,
)
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


def _resolve_identity(
    username: str | None, email: str, derive: bool
) -> tuple[str, str | None, str | None]:
    """The username to create, plus the names ``--email`` implies.

    ``None`` for a name means "no opinion", not "empty": a single-part local
    address says nothing about what the person is called, so the command's own
    defaults stand rather than being overwritten with a guess.
    """
    if derive and username:
        raise ValidationError("Pass USERNAME or --derive-username, not both.")
    if not derive:
        if not username:
            raise ValidationError(
                "Provide USERNAME, or --derive-username to take it from --email."
            )
        return username, None, None

    local = email.rsplit("@", 1)[0].strip().lower() if "@" in email else ""
    if not local:
        raise ValidationError(f"Cannot derive a username from --email {email!r}")

    parts = [part for part in local.split(".") if part]
    if len(parts) < 2:
        return local, None, None
    return local, parts[0].title(), " ".join(part.title() for part in parts[1:])


def _conflicting_user(
    users: Iterable[dict], *, username: str, email: str
) -> tuple[dict, str] | None:
    """The user a create would collide with, and the field that collides.

    Superset holds both unique and reports the breach as a bare 422 naming
    neither the field nor the account already holding it — so the collision is
    found here, where the existing row can be shown.
    """
    for user in users:
        if str(user.get("username", "")).lower() == username.lower():
            return user, "username"
        if str(user.get("email", "")).lower() == email.lower():
            return user, "email"
    return None


def _user_named(users: Iterable[dict], username: str) -> dict | None:
    """The user holding ``username``, matched the way Superset matches it."""
    for user in users:
        if str(user.get("username", "")).lower() == username.lower():
            return user
    return None


def _password_or_prompt(password: str | None, generate: bool) -> tuple[str, bool]:
    """The password to send, and whether this tool invented it.

    Prompting is done here rather than by ``prompt=True`` on the option: an
    option that prompts whenever it is missing would also prompt when
    ``--generate-password`` has already answered the question.
    """
    if generate and password is not None:
        raise ValidationError("Pass --password or --generate-password, not both.")
    if generate:
        return generate_password(), True
    if password is not None:
        return password, False
    return typer.prompt("Password", hide_input=True, confirmation_prompt=True), False


def _record_credential(username: str, password: str, base_url: str) -> Path:
    """Write a generated password to a 0600 file and say where it went.

    A generated password is never printed. It is read once, by whoever hands
    the account over, from a file only they can read — a terminal is shared,
    scrolled back through, and screen-shared, and a password that reached it
    has to be treated as disclosed.
    """
    path = credentials_default_path(prefix="dp-superset-credentials")
    creds_file, is_new = open_credentials_file(path)
    try:
        writer = csv.writer(creds_file)
        if is_new:
            writer.writerow(["username", "password", "created_at", "superset_url"])
        writer.writerow(
            [
                username,
                password,
                datetime.now(UTC).isoformat(timespec="seconds"),
                base_url,
            ]
        )
        creds_file.flush()
    finally:
        creds_file.close()
    return path


def _report_credential(path: Path) -> None:
    console.print(f"[dim]Password written to {esc(path)}[/dim]")
    if not file_mode_secure(path):
        console.print(
            f"[yellow]![/yellow] [dim]{esc(path)} is readable by others; "
            "chmod 600 it.[/dim]"
        )


@users_app.command("set-password")
def set_user_password(
    username: str = typer.Argument(..., help="Superset username whose password to set"),
    password: str | None = typer.Option(
        None,
        "--password",
        help="New password (omit to be prompted without echo).",
    ),
    generate: bool = typer.Option(
        False,
        "--generate-password",
        "-G",
        help=(
            "Generate a strong password and write it to a 0600 file instead of "
            "prompting. It is never printed."
        ),
    ),
):
    """Set an existing Superset user's password."""
    base_url, admin_username, admin_password = _load_auth_context()

    try:
        new_password, generated = _password_or_prompt(password, generate)
    except ValidationError as exc:
        fail(exc, console=console)

    try:
        with build_client() as client:
            access_token = _login(client, base_url, admin_username, admin_password)
            user = _user_named(_iter_users(client, base_url, access_token), username)
            if user is None:
                raise ValidationError(f"There is no Superset user named {username!r}.")
            user_id = user.get("id")
            if not isinstance(user_id, int):
                raise ServiceError(f"Superset user {username!r} has no usable id")
            # Only the password: a PUT carrying fields it was not asked to
            # change is a PUT that can undo someone else's edit.
            _update_user(
                client, base_url, access_token, user_id, {"password": new_password}
            )
    except (AuthError, ServiceError, ConfigError, ValidationError) as exc:
        fail(exc, console=console)

    console.print(
        "[green]✓ Superset password updated[/green] "
        f"[dim](username={esc(user.get('username'))}, id={esc(user_id)})[/dim]"
    )
    if generated:
        _report_credential(
            _record_credential(str(user.get("username")), new_password, base_url)
        )


@users_app.command("create")
def create_user(
    username: str | None = typer.Argument(
        None, help="Superset username to create (omit it when passing -U)"
    ),
    password: str | None = typer.Option(
        None,
        "--password",
        help="Password for the new user (omit to be prompted without echo).",
    ),
    generate: bool = typer.Option(
        False,
        "--generate-password",
        "-G",
        help=(
            "Generate a strong password and write it to a 0600 file instead of "
            "prompting. It is never printed."
        ),
    ),
    email: str = typer.Option(..., "--email", "-e", help="Email for the new user"),
    derive_username: bool = typer.Option(
        False,
        "--derive-username",
        "-U",
        help=(
            "Take the username from the local part of --email "
            "(eva.germeshausen@betterdoc.de becomes eva.germeshausen), and the "
            "names from its dotted segments."
        ),
    ),
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
    # Before the auth context: telling someone their flags contradict each
    # other does not need an environment to be configured first.
    try:
        resolved_username, derived_first, derived_last = _resolve_identity(
            username, email, derive_username
        )
    except ValidationError as exc:
        fail(exc, console=console)

    base_url, admin_username, admin_password = _load_auth_context()

    try:
        password, generated = _password_or_prompt(password, generate)
    except ValidationError as exc:
        fail(exc, console=console)

    resolved_email = email
    resolved_first_name = first_name or derived_first or resolved_username
    resolved_last_name = last_name or derived_last or "User"
    role_names = role_name if role_name else ["Gamma"]
    group_names = group_name if group_name else []

    try:
        with build_client() as client:
            access_token = _login(client, base_url, admin_username, admin_password)
            conflict = _conflicting_user(
                _iter_users(client, base_url, access_token),
                username=resolved_username,
                email=resolved_email,
            )
            if conflict is not None:
                existing, field = conflict
                raise ValidationError(
                    f"Superset already has a user with that {field}: "
                    f"id={existing.get('id')}, username={existing.get('username')}, "
                    f"email={existing.get('email')}"
                )
            role_ids = _resolve_role_ids(client, base_url, access_token, role_names)
            payload = {
                "username": resolved_username,
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
    except (AuthError, ServiceError, ConfigError, ValidationError) as exc:
        fail(exc, console=console)

    user_id = response.get("id") or response.get("result", {}).get("id")
    console.print(
        "[green]✓ Superset user created[/green] "
        f"[dim](username={esc(resolved_username)}, id={esc(user_id)})[/dim]"
    )
    if generated:
        _report_credential(_record_credential(resolved_username, password, base_url))


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
    add_role: list[str] | None = typer.Option(
        None,
        "--add-role",
        help="Role to add (repeatable)",
    ),
    remove_role: list[str] | None = typer.Option(
        None,
        "--remove-role",
        help="Role to remove (repeatable)",
    ),
    set_role: list[str] | None = typer.Option(
        None,
        "--set-role",
        help="Replace roles with this list (repeatable)",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Preview changes without updating users",
    ),
):
    """Update users in bulk based on role filters and group changes."""
    base_url, admin_username, admin_password = _load_auth_context()

    if not any([add_group, remove_group, set_group, add_role, remove_role, set_role]):
        console.print(
            "[red]Error: specify at least one group or role update flag[/red]"
        )
        raise typer.Exit(code=1)

    if set_group and (add_group or remove_group):
        console.print(
            "[red]Error: --set-group cannot be combined with "
            "--add-group or --remove-group[/red]"
        )
        raise typer.Exit(code=1)

    if set_role and (add_role or remove_role):
        console.print(
            "[red]Error: --set-role cannot be combined with "
            "--add-role or --remove-role[/red]"
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
            add_role_ids = (
                _resolve_role_ids(client, base_url, access_token, add_role)
                if add_role
                else []
            )
            remove_role_ids = (
                _resolve_role_ids(client, base_url, access_token, remove_role)
                if remove_role
                else []
            )
            set_role_ids = (
                _resolve_role_ids(client, base_url, access_token, set_role)
                if set_role
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

                updated_roles = user_roles
                if set_role is not None and set_role:
                    updated_roles = list(set_role_ids)
                elif add_role_ids or remove_role_ids:
                    updated_roles = list(set(user_roles).union(add_role_ids))
                    if remove_role_ids:
                        updated_roles = [
                            rid for rid in updated_roles if rid not in remove_role_ids
                        ]

                # Either kind of change counts. This compared groups alone, so
                # a user whose groups stayed put but whose roles changed was
                # skipped as "nothing to do".
                if set(updated_groups) == set(user_groups) and set(
                    updated_roles
                ) == set(user_roles):
                    continue

                matched += 1
                if dry_run:
                    continue

                payload = {
                    "roles": updated_roles,
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
