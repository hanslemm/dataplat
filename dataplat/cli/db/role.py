"""Rendering for ``dp db role``.

Takes the ``RoleDescription`` produced by the service layer and prints
it with Rich. No SQL or connection handling in this module.
"""

from __future__ import annotations

import dataclasses
import json
import sys

import typer
from rich.console import Console
from rich.text import Text

from dataplat.cli._options import json_option
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
    limit_option,
    resolve_params_or_exit,
)
from dataplat.cli.db._report import (
    SectionCounter as _SectionCounter,
)
from dataplat.cli.db._report import (
    indent as _indent,
)
from dataplat.cli.db._report import (
    print_section_heading as _print_section_heading,
)
from dataplat.cli.db._report import (
    report_table as _report_table,
)
from dataplat.cli.db._report import (
    title_card as _title_card,
)
from dataplat.services.db.connection import SqlEngine
from dataplat.services.db.role import (
    DefaultPrivilege,
    EffectivePrivilege,
    MembershipEdge,
    OwnedObjectsSummary,
    RedshiftProbeLimitError,
    RoleAttributes,
    RoleDescription,
    RoleKind,
    RoleNotFoundError,
    describe_role,
)


def _truncate[T](rows: list[T], max_rows: int | None) -> tuple[list[T], int]:
    """Return ``(visible, hidden_count)``. ``max_rows<=0`` or ``None`` = no cap."""
    if max_rows is None or max_rows <= 0 or len(rows) <= max_rows:
        return rows, 0
    return rows[:max_rows], len(rows) - max_rows


def _more_line(hidden: int) -> str:
    return f"  [dim italic]… and {hidden} more (raise --limit to see all).[/dim italic]"


def _password_set_value(password_set: bool | None, engine: SqlEngine) -> str:
    """Render the tri-state ``password_set`` as markup.

    ``None`` means the server would not say, and the reason travels with the
    word: a bare "unknown" in a security report reads as a tool defect, and
    rendering it as "no" would be a false negative on exactly the field an
    auditor came for. The reason differs by engine, and naming pg_authid on
    Redshift — which has no such relation — would send the reader somewhere
    that does not exist.
    """
    if password_set is None:
        reason = (
            "no readable password catalog on Redshift"
            if engine == SqlEngine.redshift
            else "needs superuser to read pg_authid"
        )
        return f"unknown [dim]({reason})[/dim]"
    return "yes" if password_set else "no"


def _attributes_metadata(attrs: RoleAttributes) -> list[tuple[str, str]]:
    flags: list[str] = []
    if attrs.superuser:
        flags.append("SUPERUSER")
    if attrs.create_db:
        flags.append("CREATEDB")
    if attrs.create_role:
        flags.append("CREATEROLE")
    if attrs.replication:
        flags.append("REPLICATION")
    if attrs.bypass_rls:
        flags.append("BYPASSRLS")
    if not attrs.inherit:
        flags.append("NOINHERIT")

    metadata: list[tuple[str, str]] = []
    metadata.append(("Login", "yes" if attrs.can_login else "no"))
    if flags:
        metadata.append(("Flags", " · ".join(flags)))
    if attrs.connection_limit >= 0:
        metadata.append(("Conn limit", str(attrs.connection_limit)))
    if attrs.valid_until:
        # The title card renders metadata values as markup, and this one is
        # warehouse data (rolvaliduntil rendered by the driver).
        metadata.append(("Valid until", esc(attrs.valid_until)))
    # `is True` on purpose: password_set is tri-state, and the card has room
    # only for a bare word. An unexplained "unknown" chip is worse than no
    # chip, so the unknown case is left to the Attributes table, which has
    # room to say why.
    if attrs.password_set is True:
        metadata.append(("Password", "set"))
    return metadata


def _render_attributes(
    console: Console,
    counter: _SectionCounter,
    attrs: RoleAttributes,
    engine: SqlEngine,
) -> None:
    _print_section_heading(
        console, counter, "Attributes", "Role flags, login, and limits."
    )
    table = _report_table(zebra=False)
    table.add_column("Attribute", style="dim", no_wrap=True)
    table.add_column("Value")
    table.add_row("Superuser", "yes" if attrs.superuser else "no")
    table.add_row("Can login", "yes" if attrs.can_login else "no")
    table.add_row("Create DB", "yes" if attrs.create_db else "no")
    table.add_row("Create role", "yes" if attrs.create_role else "no")
    table.add_row("Inherits", "yes" if attrs.inherit else "no")
    table.add_row("Replication", "yes" if attrs.replication else "no")
    table.add_row("Bypass RLS", "yes" if attrs.bypass_rls else "no")
    table.add_row(
        "Connection limit",
        "unlimited" if attrs.connection_limit < 0 else str(attrs.connection_limit),
    )
    table.add_row("Password set", _password_set_value(attrs.password_set, engine))
    table.add_row("Valid until", cell(attrs.valid_until or "—"))
    console.print(_indent(table))


def _render_memberships(
    console: Console,
    counter: _SectionCounter,
    title: str,
    caption: str,
    edges: list[MembershipEdge],
    *,
    max_rows: int | None,
) -> None:
    if not edges:
        return
    _print_section_heading(console, counter, title, caption)
    visible, hidden = _truncate(edges, max_rows)
    table = _report_table(zebra=len(visible) > 5)
    table.add_column("Role")
    table.add_column("Depth", justify="right", style="dim")
    table.add_column("Inherits")
    table.add_column("Via", style="dim")
    for e in visible:
        table.add_row(
            cell(e.role),
            str(e.depth),
            "yes" if e.inherit else "no",
            cell(e.via or ""),
        )
    console.print(_indent(table))
    if hidden:
        console.print(_more_line(hidden))


def _render_ownership(
    console: Console,
    counter: _SectionCounter,
    owned: OwnedObjectsSummary,
    *,
    max_rows: int | None,
) -> None:
    if not owned.schemas and not owned.relations_by_schema:
        return
    _print_section_heading(
        console, counter, "Ownership", "Schemas and relations owned by this role."
    )
    if owned.schemas:
        visible_schemas, hidden_schemas = _truncate(owned.schemas, max_rows)
        suffix = f" [dim](+{hidden_schemas} more)[/dim]" if hidden_schemas else ""
        names = ", ".join(esc(s) for s in visible_schemas)
        console.print(f"  [dim]Schemas:[/dim] {names}{suffix}")
        console.print()
    if owned.relations_by_schema:
        flat = [
            (schema, kind, count)
            for schema in sorted(owned.relations_by_schema)
            for kind, count in sorted(owned.relations_by_schema[schema].items())
        ]
        visible, hidden = _truncate(flat, max_rows)
        table = _report_table(zebra=len(visible) > 5)
        table.add_column("Schema")
        table.add_column("Kind", style="dim")
        table.add_column("Count", justify="right")
        for schema, kind, count in visible:
            table.add_row(cell(schema), cell(kind), str(count))
        console.print(_indent(table))
        if hidden:
            console.print(_more_line(hidden))
        console.print()
        console.print(f"  [dim]Total relations: {owned.total_relations}[/dim]")


def _render_effective_privileges(
    console: Console,
    counter: _SectionCounter,
    rows: list[EffectivePrivilege],
    *,
    engine: SqlEngine,
    direct_only: bool,
    closure_size: int,
    max_rows: int | None,
    redshift_rbac: bool | None,
) -> None:
    if not rows:
        return
    suffix = " (direct only)" if direct_only else ""
    _print_section_heading(
        console,
        counter,
        f"Effective privileges{suffix}",
        "Grants reachable through this role, grouped by scope.",
    )
    if engine == SqlEngine.redshift and redshift_rbac is False:
        console.print(
            "  [yellow]Redshift RBAC not available — privileges resolved "
            "by probing has_*_privilege(); via = self.[/yellow]"
        )
        console.print()
    elif engine == SqlEngine.redshift and redshift_rbac is True:
        console.print("  [dim]Redshift: resolved via svv_*_privileges.[/dim]")
        console.print()
    elif not direct_only:
        console.print(
            f"  [dim]Closure of {closure_size} roles (self + inherited + public).[/dim]"
        )
        console.print()

    by_scope: dict[str, list[EffectivePrivilege]] = {}
    for r in rows:
        by_scope.setdefault(r.scope, []).append(r)

    for scope in ("schema", "relation", "sequence", "function"):
        scope_rows = by_scope.get(scope, [])
        if not scope_rows:
            continue
        label = {
            "schema": "Schemas",
            "relation": "Relations",
            "sequence": "Sequences",
            "function": "Functions",
        }[scope]
        console.print(f"  [bold]{label}[/bold]")
        visible, hidden = _truncate(scope_rows, max_rows)
        table = _report_table(zebra=len(visible) > 5)
        table.add_column("Name")
        if scope == "relation":
            table.add_column("Kind", style="dim")
        table.add_column("Privilege")
        table.add_column("Via", style="dim")
        table.add_column("Grantor", style="dim")
        table.add_column("With grant")
        for r in visible:
            columns: list[str | Text] = [cell(r.qualified_name)]
            if scope == "relation":
                columns.append(cell(r.kind))
            columns.extend(
                [
                    cell(r.privilege),
                    cell(r.via),
                    cell(r.grantor or ""),
                    "yes" if r.grantable else "",
                ]
            )
            table.add_row(*columns)
        console.print(_indent(table))
        if hidden:
            console.print(_more_line(hidden))
        console.print()


def _render_default_privileges(
    console: Console,
    counter: _SectionCounter,
    rows: list[DefaultPrivilege],
    *,
    max_rows: int | None,
) -> None:
    if not rows:
        return
    _print_section_heading(
        console,
        counter,
        "Default privileges",
        "Privileges granted on future objects created by each owner.",
    )
    visible, hidden = _truncate(rows, max_rows)
    table = _report_table(zebra=len(visible) > 5)
    table.add_column("Owner")
    table.add_column("Schema", style="dim")
    table.add_column("Object type")
    table.add_column("Privilege")
    table.add_column("Via", style="dim")
    table.add_column("With grant")
    for r in visible:
        table.add_row(
            cell(r.owner),
            cell(r.schema or "(any)"),
            cell(r.object_type),
            cell(r.privilege),
            cell(r.via),
            "yes" if r.grantable else "",
        )
    console.print(_indent(table))
    if hidden:
        console.print(_more_line(hidden))


def render_role_description(
    console: Console,
    desc: RoleDescription,
    engine: SqlEngine,
    *,
    max_rows: int | None = 10,
) -> None:
    """Render a role report. ``max_rows=None`` or ``<=0`` disables truncation."""
    subtitle = "User" if desc.ref.kind == RoleKind.user else "Group / role"
    metadata = _attributes_metadata(desc.attributes)
    card = _title_card(
        console, title=esc(desc.ref.name), subtitle=subtitle, metadata=metadata
    )
    console.print(card)
    console.print()
    console.print()

    counter = _SectionCounter()
    _render_attributes(console, counter, desc.attributes, engine)
    parent_caption = (
        "Roles this role is a member of (single-level; Redshift groups)."
        if engine == SqlEngine.redshift
        else "Roles this role is a member of (recursive)."
    )
    child_caption = (
        "Roles that are members of this role (single-level; Redshift groups)."
        if engine == SqlEngine.redshift
        else "Roles that are members of this role (recursive)."
    )
    _render_memberships(
        console,
        counter,
        "Memberships (parents)",
        parent_caption,
        desc.memberships_out,
        max_rows=max_rows,
    )
    _render_memberships(
        console,
        counter,
        "Memberships (children)",
        child_caption,
        desc.memberships_in,
        max_rows=max_rows,
    )
    _render_ownership(console, counter, desc.owned, max_rows=max_rows)
    _render_effective_privileges(
        console,
        counter,
        desc.effective_privileges,
        engine=engine,
        direct_only=desc.direct_only,
        closure_size=len(desc.closure),
        max_rows=max_rows,
        redshift_rbac=desc.redshift_rbac,
    )
    _render_default_privileges(
        console,
        counter,
        desc.default_privileges,
        max_rows=max_rows,
    )


def role_description_to_json(desc: RoleDescription) -> str:
    """Serialize a ``RoleDescription`` to a JSON string (complete, no truncation)."""
    payload = dataclasses.asdict(desc)
    payload["ref"]["kind"] = desc.ref.kind.value
    payload["closure"] = sorted(desc.closure)
    return json.dumps(payload, indent=2, default=str)


def show_command(
    name: str = typer.Argument(..., help="Role / user / group name"),
    target: str | None = TargetOption,
    engine: SqlEngine | None = EngineOption,
    user: str | None = UserOption,
    password: str | None = PasswordOption,
    database: str | None = DatabaseOption,
    host: str | None = HostOption,
    port: int | None = PortOption,
    sslmode: str | None = SslmodeOption,
    env_prefix: str | None = EnvPrefixOption,
    direct_only: bool = typer.Option(
        False,
        "--direct-only",
        help="Ignore inheritance — show only grants made directly to this role.",
    ),
    max_rows: int = limit_option(
        10,
        "Cap listy sections (memberships, privileges, …) at N rows. 0 = show all.",
    ),
    as_json: bool = json_option(
        "Emit the full role description as JSON on stdout (ignores --limit)."
    ),
) -> None:
    """Entry point for ``dp db role show <name>``."""
    console = Console()
    conn_params = resolve_params_or_exit(
        ConnCliParams(
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
    )

    resolved_engine = conn_params.engine
    try:
        with db_session(conn_params) as conn, conn.cursor() as cursor:
            desc = describe_role(
                cursor,
                name,
                engine=resolved_engine,
                direct_only=direct_only,
            )
            if as_json:
                sys.stdout.write(role_description_to_json(desc) + "\n")
            else:
                render_role_description(
                    console,
                    desc,
                    resolved_engine,
                    max_rows=max_rows,
                )
    except (ValueError, RoleNotFoundError, RedshiftProbeLimitError) as exc:
        console.print(f"[red]Error: {esc(exc)}[/red]")
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# Typer subgroup: dp db role {show|create|drop}
# ---------------------------------------------------------------------------

# Imports for create/drop subcommands kept here so the file order reads top-down.
from dataplat.cli.db.role_create import create_command  # noqa: E402
from dataplat.cli.db.role_drop import drop_command  # noqa: E402
from dataplat.cli.db.role_list import list_command  # noqa: E402

app = typer.Typer(
    name="role",
    help="Inspect, list, create, or drop a role / user / group.",
    no_args_is_help=True,
)
app.command(
    "list",
    help="List all roles on the cluster with attributes and membership counts.",
)(list_command)
app.command(
    "show",
    help="Show attributes, memberships, and effective privileges.",
)(show_command)
app.command(
    "create",
    help="Create one or more roles (login by default, --no-login for "
    "passwordless group roles) with explicit privileges.",
)(create_command)
app.command(
    "drop",
    help="Drop one or more roles, reassigning ownership across databases.",
)(drop_command)
