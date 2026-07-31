"""``dp db schema grant`` / ``revoke`` — schema privileges for users, groups, roles.

Both directions are one implementation with a flag, because they must stay
symmetric: anything grantable has to be revocable in the same vocabulary, or an
operator cannot undo what they did.

Two ways to say who gets what::

    --schemas analytics --to readers --privileges read
    --grant readers:read --grant etl:readwrite

The second form exists because the first cannot express two grantees with
different privileges in one invocation, and running the command twice is how a
half-applied change happens.

Refused on DuckDB, which has no ``GRANT`` statement at all — the keyword does not
parse, because there are no principals to grant to.

Three behaviours worth knowing:

- **``default-*`` privileges require a grantor.** ``ALTER DEFAULT PRIVILEGES``
  without ``FOR ROLE`` / ``FOR USER`` binds to whoever is connected, so tables
  later created by dbt or the schema owner inherit nothing. That is the single
  most common way default privileges silently fail, so this refuses instead of
  emitting a statement that succeeds and does nothing. ``--default-for`` names
  the grantor; each schema's own owner is the default.
- **Any table-level privilege implies ``USAGE``** on the containing schema,
  because an object cannot be reached without it.
- **Grants already in effect are reported, not re-issued** — schema-scoped ones
  only. Across a fan-out like ``ON ALL TABLES`` "held" has no single answer, and
  ``GRANT`` is idempotent, so re-issuing those costs nothing but noise.
"""

from __future__ import annotations

import typer
from rich.console import Console

from dataplat.cli._exit import fail
from dataplat.cli._options import YesOption
from dataplat.cli._prompt import confirm_or_exit
from dataplat.cli._render import esc
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
from dataplat.cli.db._grantees import GranteeKind, parent_kind_for
from dataplat.cli.db._plan import execute_ops, print_ops
from dataplat.cli.db._schema_opts import (
    DefaultForOption,
    PrivilegesOption,
    SchemaLikeOption,
    SchemaSelectOption,
    ToKindOption,
)
from dataplat.core.errors import ValidationError
from dataplat.services.db._like import glob_to_like
from dataplat.services.db.capabilities import Capability, require_capability
from dataplat.services.db.connection import SqlEngine
from dataplat.services.db.role_admin import parse_csv_flag, resolve_grantee_kinds
from dataplat.services.db.role_dialects import dialect_for
from dataplat.services.db.schema_admin import (
    GranteeSpec,
    SchemaPrivilege,
    build_grant_plan,
    parse_grant_spec,
    parse_privileges,
)
from dataplat.services.db.schema_dialects import schema_dialect_for


def _collect_requests(
    *,
    simple_grantees: tuple[str, ...],
    privileges: list[str] | None,
    pairs: tuple[str, ...],
    verb: str,
) -> dict[str, tuple[SchemaPrivilege, ...]]:
    """Merge the ``--to/--from`` form and the ``grantee:privs`` form.

    A grantee named in both accumulates the union rather than having one form win
    silently — the operator asked for both, and dropping either would be a
    surprise the output never mentions.
    """
    requested: dict[str, tuple[SchemaPrivilege, ...]] = {}
    if simple_grantees:
        if not privileges:
            raise ValidationError(
                f"--{verb} needs --privileges. Pass e.g. --privileges read, or "
                "use --grant grantee:privileges to give each grantee its own."
            )
        shared = parse_privileges(privileges)
        for name in simple_grantees:
            requested[name] = shared
    elif privileges and not pairs:
        raise ValidationError(f"--privileges needs --{verb} naming who receives them.")

    for pair in pairs:
        name, privs = parse_grant_spec(pair)
        existing = requested.get(name, ())
        merged = dict.fromkeys((*existing, *privs))
        requested[name] = tuple(merged)

    if not requested:
        raise ValidationError(
            f"nothing to do: pass --{verb} with --privileges, or --grant "
            "grantee:privileges."
        )
    return requested


def _privilege_command(
    *,
    revoke: bool,
    schemas: list[str] | None,
    like: str | None,
    grantees: list[str] | None,
    privileges: list[str] | None,
    grant_pairs: list[str] | None,
    to_kind: GranteeKind | None,
    default_for: str | None,
    cascade: bool,
    dry_run: bool,
    yes: bool,
    conn_cli: ConnCliParams,
) -> None:
    """The shared body of ``schema grant`` and ``schema revoke``."""
    console = Console()
    verb = "from" if revoke else "to"
    try:
        require_capability(
            engine_or_exit(conn_cli),
            Capability.schema_privileges,
            command=f"dp db schema {'revoke' if revoke else 'grant'}",
        )
    except ValidationError as exc:
        fail(exc, console=console)
    conn_params = resolve_params_or_exit(conn_cli)

    named = parse_csv_flag(schemas)
    if named and like:
        raise typer.BadParameter("pass --schemas or --like, not both")
    if not named and not like:
        raise typer.BadParameter("pass --schemas or --like")

    schema_dialect = schema_dialect_for(conn_params.engine)
    role_dialect = dialect_for(conn_params.engine)
    forced = parent_kind_for(to_kind)

    try:
        requested = _collect_requests(
            simple_grantees=parse_csv_flag(grantees),
            privileges=privileges,
            pairs=parse_csv_flag(grant_pairs),
            verb=verb,
        )
    except ValidationError as exc:
        fail(exc, console=console)

    with db_session(conn_params) as conn, conn.cursor() as cursor:
        rows = schema_dialect.list_schemas(
            cursor, like=glob_to_like(like) if like else None
        )
        owners = {row.name: row.owner for row in rows}
        if like:
            targets = [row.name for row in rows]
            if not targets:
                fail(ValidationError(f'no schemas matched "{like}".'), console=console)
        else:
            targets = list(named)
            missing = [n for n in targets if n not in owners]
            if missing:
                fail(
                    ValidationError(f"schema(s) not found: {', '.join(missing)}"),
                    console=console,
                )

        try:
            # allow_public: PUBLIC is a legal grantee for object privileges, and
            # is refused for role membership — see resolve_grantee_kinds.
            kinds = resolve_grantee_kinds(
                role_dialect,
                cursor,
                list(requested),
                forced,
                flag="--to-kind",
                allow_public=True,
            )
            specs = [
                GranteeSpec(name=name, kind=kinds[name], privileges=privs)
                for name, privs in requested.items()
            ]
            # Each schema's own owner is the grantor unless --default-for says
            # otherwise: the owner is who dbt and migrations run as, and so who
            # will actually be creating the future tables.
            grantors = (
                dict.fromkeys(targets, default_for)
                if default_for
                else {name: owners[name] for name in targets if name in owners}
            )
            held = (
                set()
                if revoke
                else schema_dialect.held_schema_privileges(
                    cursor, targets, list(requested), kinds
                )
            )
            plan = build_grant_plan(
                targets,
                specs,
                schema_dialect,
                grantors=grantors,
                revoke=revoke,
                cascade=cascade,
                held=held,
            )
        except ValidationError as exc:
            fail(exc, console=console)

        action = "revoke" if revoke else "grant"
        console.print(
            f"[bold]Plan:[/bold] {len(plan.ops)} {action}(s) across "
            f"{len(targets)} schema(s)"
        )
        for warning in plan.warnings:
            console.print(f"[yellow]Warning: {warning}[/yellow]")
        if plan.already_held:
            console.print(
                f"[dim]Already held ({len(plan.already_held)}), skipped:[/dim]"
            )
            for schema, grantee, privilege in plan.already_held:
                console.print(f"  [dim]{esc(schema)}: {esc(grantee)} {privilege}[/dim]")
        if not plan.ops:
            console.print("\n[dim]Nothing to do.[/dim]")
            return
        print_ops(console, plan.ops, conn)

        if dry_run:
            console.print("\n[yellow]Dry-run; no SQL executed.[/yellow]")
            return
        confirm_or_exit(yes=yes, prompt="\nProceed?", console=console)
        execute_ops(cursor, plan.ops)

    console.print(f"\n[green]Done:[/green] {len(plan.ops)} statement(s)")


def grant_command(
    schemas: list[str] | None = SchemaSelectOption,
    like: str | None = SchemaLikeOption,
    to: list[str] | None = typer.Option(
        None,
        "--to",
        help="Grantees: users, groups, roles, or PUBLIC. Repeatable / comma-separated.",
    ),
    privileges: list[str] | None = PrivilegesOption,
    grant_pairs: list[str] | None = typer.Option(
        None,
        "--grant",
        help="grantee:privileges, for per-grantee privileges "
        "(e.g. --grant readers:read --grant etl:readwrite). Repeatable.",
    ),
    to_kind: GranteeKind | None = ToKindOption,
    default_for: str | None = DefaultForOption,
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print the SQL and exit without executing it."
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
    """Entry point for ``dp db schema grant``."""
    _privilege_command(
        revoke=False,
        schemas=schemas,
        like=like,
        grantees=to,
        privileges=privileges,
        grant_pairs=grant_pairs,
        to_kind=to_kind,
        default_for=default_for,
        cascade=False,
        dry_run=dry_run,
        yes=yes,
        conn_cli=ConnCliParams(
            target=target,
            engine=engine,
            user=user,
            password=password,
            database=database,
            host=host,
            port=port,
            sslmode=sslmode,
            env_prefix=env_prefix,
        ),
    )


def revoke_command(
    schemas: list[str] | None = SchemaSelectOption,
    like: str | None = SchemaLikeOption,
    from_: list[str] | None = typer.Option(
        None,
        "--from",
        help="Principals to revoke from: users, groups, roles, or PUBLIC. "
        "Repeatable / comma-separated.",
    ),
    privileges: list[str] | None = PrivilegesOption,
    grant_pairs: list[str] | None = typer.Option(
        None,
        "--grant",
        help="grantee:privileges, for per-principal privileges. Repeatable.",
    ),
    to_kind: GranteeKind | None = ToKindOption,
    default_for: str | None = DefaultForOption,
    cascade: bool = typer.Option(
        False,
        "--cascade",
        help="Also revoke privileges this principal granted onward to others.",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print the SQL and exit without executing it."
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
    """Entry point for ``dp db schema revoke``."""
    _privilege_command(
        revoke=True,
        schemas=schemas,
        like=like,
        grantees=from_,
        privileges=privileges,
        grant_pairs=grant_pairs,
        to_kind=to_kind,
        default_for=default_for,
        cascade=cascade,
        dry_run=dry_run,
        yes=yes,
        conn_cli=ConnCliParams(
            target=target,
            engine=engine,
            user=user,
            password=password,
            database=database,
            host=host,
            port=port,
            sslmode=sslmode,
            env_prefix=env_prefix,
        ),
    )
