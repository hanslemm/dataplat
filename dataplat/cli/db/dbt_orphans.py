"""`dp db dbt-orphans` — discover and rename orphan dbt tables."""

from __future__ import annotations

import glob
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import typer
from rich.console import Console

from dataplat.cli._options import YesOption
from dataplat.cli._prompt import confirm_or_exit
from dataplat.cli._render import esc
from dataplat.core.errors import ConfigError, ServiceError, ValidationError
from dataplat.services.db.connection import SqlEngine
from dataplat.services.db.orphans import (
    DEPRECATED_SUFFIX,
    LIVE_STATUSES,
    DropEntry,
    ObjectKind,
    RenameEntry,
    classify_object,
    diff_orphans,
    drop_object,
    excluded_schemas,
    fetch_deprecated_objects,
    fetch_existing_relations,
    fetch_live_model_relations,
    invocation_command,
    node_prefix,
    open_transactional_connection,
    rename_object,
    resolve_orphans_connection_params,
)
from dataplat.services.db.targets import resolve_targets

DEFAULT_WINDOW_DAYS = 7

app = typer.Typer(
    name="dbt-orphans",
    help=(
        "Discover orphan dbt tables (objects in the warehouse that the "
        "latest dbt build no longer produces) and rename them with a "
        "_deprecated suffix."
    ),
    invoke_without_command=True,
    no_args_is_help=False,
)

console = Console()

LOG_DIR = Path.home() / ".config" / "dataplat" / "logs" / "dbt-orphans"
# Older releases wrote logs into ./local — keep reading them for revert/purge.
LEGACY_LOG_DIR = Path("local")
APPLY_LOG_PREFIX = "dbt_orphans"
PURGE_LOG_PREFIX = "dbt_orphans_purge"

# Log entries keep the historical engine labels so old logs stay revertable.
_ENGINE_LABELS: dict[SqlEngine, str] = {
    SqlEngine.postgresql: "postgres",
    SqlEngine.redshift: "redshift",
}

TargetOption = typer.Option(
    "all",
    "--target",
    "-t",
    help="Named DB target from DP_TARGETS, or all.",
)


def _tag(label: str) -> str:
    """The ``[<engine>]`` prefix every progress line carries.

    ``[postgres]`` and ``[redshift]`` are well-formed Rich tags, so unescaped
    the prefix was parsed as a style name and silently dropped — leaving the
    reader unable to tell which cluster a rename or drop line belonged to.
    """
    return esc(f"[{label}]")


def _timestamped_log_path(prefix: str) -> str:
    """Build a unique-per-run log path: ``<log dir>/<prefix>-<UTC ISO>.log.json``."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return str(LOG_DIR / f"{prefix}-{stamp}.log.json")


def _logs_in(directory: Path, prefix: str) -> list[str]:
    """Timestamped logs for ``prefix`` inside ``directory``.

    ``glob.escape`` the directory but not the pattern: only the ``*`` after the
    prefix is meant to be a wildcard. Left unescaped, a bracket anywhere in the
    path — a home directory named ``[work]`` is enough — reads as a character
    class and quietly matches nothing, so ``revert`` would report no history and
    ``purge --older-than`` would treat every object as having no recorded
    rename.
    """
    pattern = os.path.join(glob.escape(str(directory)), f"{prefix}-*.log.json")
    return glob.glob(pattern)


def _matching_logs(prefix: str) -> list[str]:
    """All timestamped logs for ``prefix``, oldest first (by timestamped name)."""
    matches = _logs_in(LOG_DIR, prefix)
    matches += _logs_in(LEGACY_LOG_DIR, prefix)
    return sorted(matches, key=os.path.basename)


def _find_latest_log(prefix: str) -> str | None:
    """Return the newest timestamped log for ``prefix`` or ``None`` if absent."""
    matches = _matching_logs(prefix)
    return matches[-1] if matches else None


def _engines_for_target(name: str) -> list[tuple[str, SqlEngine, str]]:
    """Return ``(label, engine, env_prefix)`` per target; label is the log key."""
    try:
        targets = resolve_targets(name)
    except ValidationError as exc:
        console.print(f"[red]Error: {esc(exc)}[/red]")
        raise typer.Exit(code=1)
    return [(_ENGINE_LABELS[t.engine], t.engine, t.env_prefix) for t in targets]


def _parse_exclusions(
    tokens: list[str], file_path: str | None
) -> tuple[frozenset[str], frozenset[tuple[str, str]]]:
    """Parse --exclude flags and an optional --exclude-file into two sets.

    Returns ``(excluded_schemas, excluded_relations)``. A token with no dot
    excludes a whole schema; a token with exactly one dot excludes a single
    ``schema.name``. Tokens are whitespace-trimmed and matched case-sensitively
    against warehouse objects. Each ``--exclude`` argument may contain one or
    more tokens separated by commas (e.g. ``--exclude public,analytics``).
    Blank lines and ``#`` comments in the file are ignored.
    """
    raw_tokens: list[str] = list(tokens)

    if file_path is not None:
        if not os.path.exists(file_path):
            raise ValidationError(f"--exclude-file not found: {file_path}")
        with open(file_path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                raw_tokens.append(line)

    schemas: set[str] = set()
    relations: set[tuple[str, str]] = set()
    for raw in raw_tokens:
        for piece in raw.split(","):
            token = piece.strip()
            if not token:
                raise ValidationError(f"Empty exclusion token in {raw!r}")
            parts = token.split(".")
            if len(parts) == 1:
                schemas.add(parts[0])
            elif len(parts) == 2:
                relations.add((parts[0], parts[1]))
            else:
                raise ValidationError(
                    f"Invalid exclusion {token!r}: expected 'schema' or 'schema.name'"
                )
    return frozenset(schemas), frozenset(relations)


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    log: str | None = typer.Option(
        None,
        "--log",
        help=(
            "Audit log output path. Defaults to "
            "~/.config/dataplat/logs/dbt-orphans/"
            "dbt_orphans-<UTC timestamp>.log.json (unique per run)."
        ),
    ),
    target: str = TargetOption,
    dry_run: bool = typer.Option(
        True,
        "--dry-run/--no-dry-run",
        help="Preview without changes (default). Pass --no-dry-run to apply.",
    ),
    yes: bool = YesOption,
    exclude: list[str] = typer.Option(
        [],
        "--exclude",
        help="Schema or schema.name to skip (repeatable).",
    ),
    exclude_file: str | None = typer.Option(
        None,
        "--exclude-file",
        help="Path to a file with one exclusion token per line.",
    ),
    window_days: int = typer.Option(
        DEFAULT_WINDOW_DAYS,
        "--window-days",
        help=(
            "Consider a model 'live' if any matching dbt build in the last "
            "N days produced it. Larger windows are more conservative "
            "(fewer false-positive renames)."
        ),
    ),
) -> None:
    """Discover orphan dbt tables and rename them with _deprecated."""
    if ctx.invoked_subcommand is not None:
        return

    if log is None:
        log = _timestamped_log_path(APPLY_LOG_PREFIX)

    if window_days < 1:
        console.print("[red]Error: --window-days must be >= 1[/red]")
        raise typer.Exit(code=1)

    try:
        excluded_user_schemas, excluded_user_relations = _parse_exclusions(
            exclude, exclude_file
        )
    except ValidationError as exc:
        # The message quotes the offending --exclude token back at the user.
        console.print(f"[red]Error: {esc(exc)}[/red]")
        raise typer.Exit(code=1)

    engines = _engines_for_target(target)
    if not dry_run:
        confirm_or_exit(
            yes=yes,
            prompt="Rename every orphaned dbt object with a _deprecated suffix?",
            console=console,
        )

    since = datetime.now(UTC) - timedelta(days=window_days)

    all_entries: list[RenameEntry] = []
    try:
        for label, engine, env_prefix in engines:
            all_entries.extend(
                _run_for_engine(
                    label,
                    engine,
                    env_prefix=env_prefix,
                    excluded_user_schemas=excluded_user_schemas,
                    excluded_user_relations=excluded_user_relations,
                    window_days=window_days,
                    since=since,
                    dry_run=dry_run,
                )
            )
    except ServiceError as exc:
        _write_audit_log(log, all_entries, dry_run=dry_run)
        console.print(f"[red]{esc(exc)}[/red]")
        console.print(
            f"[yellow]Partial audit log written to {esc(log)} "
            f"({len(all_entries)} entries).[/yellow]"
        )
        raise typer.Exit(code=1)

    _write_audit_log(log, all_entries, dry_run=dry_run)

    prefix = "[DRY-RUN] " if dry_run else ""
    console.print(
        f"[green]{prefix}Processed {len(all_entries)} object(s). "
        f"Log written to {esc(log)}.[/green]"
    )
    if dry_run and all_entries:
        console.print("[dim]Re-run with --no-dry-run to apply these renames.[/dim]")


def _write_audit_log(
    log_path: str, entries: list[RenameEntry], *, dry_run: bool
) -> None:
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "dry_run": dry_run,
        "source": "dbt-orphans",
        "renames": entries,
    }
    with open(log_path, "w") as f:
        json.dump(payload, f, indent=4)


def _run_for_engine(
    label: str,
    engine: SqlEngine,
    *,
    env_prefix: str,
    excluded_user_schemas: frozenset[str],
    excluded_user_relations: frozenset[tuple[str, str]],
    window_days: int,
    since: datetime,
    dry_run: bool,
) -> list[RenameEntry]:
    try:
        params = resolve_orphans_connection_params(engine, env_prefix=env_prefix)
        dbt_node_prefix = node_prefix()
    except ConfigError as exc:
        raise ServiceError(f"[{label}] {exc}") from exc
    if params is None:
        console.print(
            f"[yellow]{_tag(label)} Missing connection parameters, skipping.[/yellow]"
        )
        return []

    is_redshift = engine is SqlEngine.redshift
    entries: list[RenameEntry] = []

    with (
        open_transactional_connection(params, dry_run=dry_run) as conn,
        conn.cursor() as cur,
    ):
        live = fetch_live_model_relations(
            cur,
            invocation_command=invocation_command(),
            node_prefix=dbt_node_prefix,
            statuses=LIVE_STATUSES,
            since=since,
        )
        if not live:
            console.print(
                f"[yellow]{_tag(label)} No matching dbt builds in the last "
                f"{window_days} day(s); skipping to avoid diffing "
                f"against empty set.[/yellow]"
            )
            return []

        excluded = excluded_schemas()
        schemas_to_scan = sorted(s for s in live if s not in excluded)
        existing = fetch_existing_relations(
            cur, schemas_to_scan, is_redshift=is_redshift
        )
        orphans = diff_orphans(
            live=live,
            existing=existing,
            excluded_schemas=excluded,
            excluded_user_schemas=excluded_user_schemas,
            excluded_user_relations=excluded_user_relations,
        )

        _print_summary(label, live, existing, orphans)

        for schema, names in orphans.items():
            for name in names:
                entry = _rename_orphan(
                    cur,
                    label,
                    schema,
                    name,
                    is_redshift=is_redshift,
                    dry_run=dry_run,
                )
                if entry is not None:
                    entries.append(entry)

    return entries


def _print_summary(
    label: str,
    live: dict[str, set[str]],
    existing: dict[str, set[str]],
    orphans: dict[str, list[str]],
) -> None:
    live_count = sum(len(v) for v in live.values())
    existing_count = sum(len(v) for v in existing.values())
    orphan_count = sum(len(v) for v in orphans.values())
    per_schema = ", ".join(f"{len(v)} in {esc(s)}" for s, v in sorted(orphans.items()))
    suffix = f" ({per_schema})" if per_schema else ""
    console.print(
        f"[cyan]{_tag(label)} {live_count} live dbt models; {existing_count} "
        f"existing in live schemas; {orphan_count} orphans after "
        f"exclusions{suffix}[/cyan]"
    )


def _rename_orphan(
    cur: Any,
    label: str,
    schema: str,
    name: str,
    *,
    is_redshift: bool,
    dry_run: bool,
) -> RenameEntry | None:
    kind = classify_object(cur, schema, name, is_redshift=is_redshift)
    # Schema and relation names come from the warehouse catalog, so every
    # interpolation below has to be escaped before Rich parses the markup.
    if kind is None:
        console.print(
            f"[yellow]{_tag(label)} {esc(schema)}.{esc(name)} no longer present, "
            f"skipping.[/yellow]"
        )
        return None

    new_name = f"{name}{DEPRECATED_SUFFIX}"
    if classify_object(cur, schema, new_name, is_redshift=is_redshift) is not None:
        console.print(
            f"[yellow]{_tag(label)} {esc(schema)}.{esc(new_name)} already exists, "
            f"skipping rename of {esc(schema)}.{esc(name)}.[/yellow]"
        )
        return None

    action = "[DRY-RUN] Would rename" if dry_run else "Renaming"
    console.print(
        f"[blue]{_tag(label)} {action} {kind} "
        f"{esc(schema)}.{esc(name)} -> {esc(schema)}.{esc(new_name)}[/blue]"
    )

    if not dry_run:
        rename_object(cur, schema, name, new_name, kind, is_redshift=is_redshift)

    return RenameEntry(
        database=label,
        schema=schema,
        old_name=name,
        new_name=new_name,
        kind=kind,
    )


@app.command("revert")
def revert_cmd(
    log: str | None = typer.Option(
        None,
        "--log",
        help=(
            "Audit log input path. Defaults to the newest "
            "dbt_orphans-*.log.json in the log directory."
        ),
    ),
    target: str = TargetOption,
    dry_run: bool = typer.Option(
        True,
        "--dry-run/--no-dry-run",
        help="Preview without changes (default). Pass --no-dry-run to revert.",
    ),
) -> None:
    """Undo a previous dbt-orphans run using the audit log."""
    if log is None:
        log = _find_latest_log(APPLY_LOG_PREFIX)
        if log is None:
            console.print(
                f"[red]Error: no {APPLY_LOG_PREFIX} log found in {esc(LOG_DIR)}/[/red]"
            )
            console.print(
                "[dim]Run `dp db dbt-orphans` first or pass --log explicitly.[/dim]"
            )
            raise typer.Exit(code=1)
        console.print(f"[dim]Using latest log: {esc(log)}[/dim]")
    if not os.path.exists(log):
        console.print(f"[red]Error: log file not found: {esc(log)}[/red]")
        console.print("[dim]Run `dp db dbt-orphans` first to generate it.[/dim]")
        raise typer.Exit(code=1)

    try:
        with open(log) as f:
            payload = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        console.print(f"[red]Error: could not read log {esc(log)}: {esc(exc)}[/red]")
        raise typer.Exit(code=1)
    if not isinstance(payload, dict):
        console.print(f"[red]Error: malformed log {esc(log)}: expected an object[/red]")
        raise typer.Exit(code=1)

    if payload.get("dry_run"):
        console.print(
            "[yellow]Log was generated in dry-run mode; "
            "no renames were actually applied.[/yellow]"
        )

    renames = payload.get("renames") or []
    if not renames:
        console.print("[dim]No renames recorded in the log; nothing to revert.[/dim]")
        return

    total = 0
    try:
        for label, engine, env_prefix in _engines_for_target(target):
            entries = [
                r for r in renames if isinstance(r, dict) and r.get("database") == label
            ]
            if not entries:
                console.print(f"[dim]{_tag(label)} No entries in log.[/dim]")
                continue
            total += _revert_for_engine(
                label, engine, entries, env_prefix=env_prefix, dry_run=dry_run
            )
    except ServiceError as exc:
        console.print(f"[red]{esc(exc)}[/red]")
        console.print(
            f"[yellow]Reverted {total} object(s) before the failure.[/yellow]"
        )
        raise typer.Exit(code=1)

    prefix = "[DRY-RUN] " if dry_run else ""
    console.print(f"[green]{prefix}Reverted {total} object(s).[/green]")
    if dry_run and total:
        console.print("[dim]Re-run with --no-dry-run to revert these renames.[/dim]")


def _revert_for_engine(
    label: str,
    engine: SqlEngine,
    entries: list[dict],
    *,
    env_prefix: str,
    dry_run: bool,
) -> int:
    try:
        params = resolve_orphans_connection_params(engine, env_prefix=env_prefix)
    except ConfigError as exc:
        raise ServiceError(f"[{label}] {exc}") from exc
    if params is None:
        console.print(
            f"[yellow]{_tag(label)} Missing connection parameters, skipping.[/yellow]"
        )
        return 0

    is_redshift = engine is SqlEngine.redshift
    reverted = 0

    with (
        open_transactional_connection(params, dry_run=dry_run) as conn,
        conn.cursor() as cur,
    ):
        for entry in entries:
            if _revert_one(cur, label, entry, is_redshift=is_redshift, dry_run=dry_run):
                reverted += 1

    return reverted


def _revert_one(
    cur: Any,
    label: str,
    entry: dict,
    *,
    is_redshift: bool,
    dry_run: bool,
) -> bool:
    schema = entry.get("schema")
    current_name = entry.get("new_name")
    original_name = entry.get("old_name")
    if not (schema and current_name and original_name):
        console.print(
            f"[yellow]{_tag(label)} Skipping malformed log entry: "
            f"{esc(repr(entry))}[/yellow]"
        )
        return False
    kind: ObjectKind = entry.get("kind", "table")

    if classify_object(cur, schema, current_name, is_redshift=is_redshift) is None:
        console.print(
            f"[dim]{_tag(label)} {esc(schema)}.{esc(current_name)} not found, "
            f"skipping revert.[/dim]"
        )
        return False

    if classify_object(cur, schema, original_name, is_redshift=is_redshift) is not None:
        console.print(
            f"[yellow]{_tag(label)} {esc(schema)}.{esc(original_name)} already "
            f"exists, cannot revert {esc(schema)}.{esc(current_name)}.[/yellow]"
        )
        return False

    action = "[DRY-RUN] Would revert" if dry_run else "Reverting"
    console.print(
        f"[blue]{_tag(label)} {action} {kind} "
        f"{esc(schema)}.{esc(current_name)} -> "
        f"{esc(schema)}.{esc(original_name)}[/blue]"
    )

    if not dry_run:
        rename_object(
            cur, schema, current_name, original_name, kind, is_redshift=is_redshift
        )
    return True


def _renamed_at_index() -> dict[tuple[str, str, str], datetime]:
    """Map ``(database, schema, new_name)`` -> newest rename timestamp.

    Built from the applied (non-dry-run) apply logs so purge can enforce a
    grace period even though the warehouse doesn't track rename times.
    """
    index: dict[tuple[str, str, str], datetime] = {}
    for path in _matching_logs(APPLY_LOG_PREFIX):
        try:
            with open(path) as f:
                payload = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict) or payload.get("dry_run"):
            continue
        try:
            when = datetime.fromisoformat(payload.get("generated_at", ""))
        except ValueError:
            continue
        for r in payload.get("renames") or []:
            if not isinstance(r, dict):
                continue
            database = r.get("database")
            schema = r.get("schema")
            new_name = r.get("new_name")
            if not (database and schema and new_name):
                continue
            key = (str(database), str(schema), str(new_name))
            if key not in index or when > index[key]:
                index[key] = when
    return index


@app.command("purge")
def purge_cmd(
    log: str | None = typer.Option(
        None,
        "--log",
        help=(
            "Purge audit log output path. Defaults to "
            "~/.config/dataplat/logs/dbt-orphans/"
            "dbt_orphans_purge-<UTC timestamp>.log.json (unique per run)."
        ),
    ),
    target: str = TargetOption,
    dry_run: bool = typer.Option(
        True,
        "--dry-run/--no-dry-run",
        help="Preview without changes (default). Pass --no-dry-run to drop.",
    ),
    yes: bool = YesOption,
    older_than: int | None = typer.Option(
        None,
        "--older-than",
        min=0,
        help=(
            "Only drop objects renamed at least N days ago (per the audit "
            "logs). Objects with no recorded rename are skipped unless "
            "--include-unknown."
        ),
    ),
    include_unknown: bool = typer.Option(
        False,
        "--include-unknown",
        help="With --older-than: also drop objects that have no recorded rename.",
    ),
    exclude: list[str] = typer.Option(
        [],
        "--exclude",
        help="Schema or schema.name to skip (repeatable).",
    ),
    exclude_file: str | None = typer.Option(
        None,
        "--exclude-file",
        help="Path to a file with one exclusion token per line.",
    ),
) -> None:
    """Permanently drop every object ending in _deprecated (irreversible)."""
    if log is None:
        log = _timestamped_log_path(PURGE_LOG_PREFIX)

    try:
        excluded_user_schemas, excluded_user_relations = _parse_exclusions(
            exclude, exclude_file
        )
    except ValidationError as exc:
        console.print(f"[red]Error: {esc(exc)}[/red]")
        raise typer.Exit(code=1)

    engines = _engines_for_target(target)
    if not dry_run:
        confirm_or_exit(
            yes=yes,
            prompt=(
                "Permanently DROP every *_deprecated object? This cannot be undone."
            ),
            console=console,
        )

    renamed_at = _renamed_at_index() if older_than is not None else None
    cutoff = (
        datetime.now(UTC) - timedelta(days=older_than)
        if older_than is not None
        else None
    )

    all_drops: list[DropEntry] = []
    try:
        for label, engine, env_prefix in engines:
            all_drops.extend(
                _purge_for_engine(
                    label,
                    engine,
                    env_prefix=env_prefix,
                    excluded_user_schemas=excluded_user_schemas,
                    excluded_user_relations=excluded_user_relations,
                    dry_run=dry_run,
                    renamed_at=renamed_at,
                    cutoff=cutoff,
                    include_unknown=include_unknown,
                )
            )
    except ServiceError as exc:
        _write_purge_log(log, all_drops, dry_run=dry_run)
        console.print(f"[red]{esc(exc)}[/red]")
        console.print(
            f"[yellow]Partial purge log written to {esc(log)} "
            f"({len(all_drops)} entries).[/yellow]"
        )
        raise typer.Exit(code=1)

    _write_purge_log(log, all_drops, dry_run=dry_run)

    prefix = "[DRY-RUN] " if dry_run else ""
    console.print(
        f"[green]{prefix}Dropped {len(all_drops)} object(s). "
        f"Log written to {esc(log)}.[/green]"
    )
    if dry_run and all_drops:
        console.print(
            "[dim]Re-run with --no-dry-run to apply these drops (irreversible).[/dim]"
        )


def _purge_for_engine(
    label: str,
    engine: SqlEngine,
    *,
    env_prefix: str,
    excluded_user_schemas: frozenset[str],
    excluded_user_relations: frozenset[tuple[str, str]],
    dry_run: bool,
    renamed_at: dict[tuple[str, str, str], datetime] | None = None,
    cutoff: datetime | None = None,
    include_unknown: bool = False,
) -> list[DropEntry]:
    try:
        params = resolve_orphans_connection_params(engine, env_prefix=env_prefix)
    except ConfigError as exc:
        raise ServiceError(f"[{label}] {exc}") from exc
    if params is None:
        console.print(
            f"[yellow]{_tag(label)} Missing connection parameters, skipping.[/yellow]"
        )
        return []

    is_redshift = engine is SqlEngine.redshift
    effective_excluded_schemas = excluded_schemas() | excluded_user_schemas
    drops: list[DropEntry] = []

    with (
        open_transactional_connection(params, dry_run=dry_run) as conn,
        conn.cursor() as cur,
    ):
        deprecated = fetch_deprecated_objects(
            cur,
            is_redshift=is_redshift,
            excluded_schemas=effective_excluded_schemas,
        )
        deprecated = [
            (s, n, k) for s, n, k in deprecated if (s, n) not in excluded_user_relations
        ]

        if renamed_at is not None and cutoff is not None:
            deprecated = _apply_age_filter(
                deprecated,
                label=label,
                renamed_at=renamed_at,
                cutoff=cutoff,
                include_unknown=include_unknown,
            )

        console.print(
            f"[cyan]{_tag(label)} {len(deprecated)} deprecated "
            f"object(s) after exclusions.[/cyan]"
        )

        for schema, name, kind in deprecated:
            entry = _drop_one(cur, label, schema, name, kind, dry_run=dry_run)
            if entry is not None:
                drops.append(entry)

    return drops


def _apply_age_filter(
    deprecated: list[tuple[str, str, Any]],
    *,
    label: str,
    renamed_at: dict[tuple[str, str, str], datetime],
    cutoff: datetime,
    include_unknown: bool,
) -> list[tuple[str, str, Any]]:
    """Keep only objects renamed before ``cutoff`` per the audit logs."""
    kept: list[tuple[str, str, Any]] = []
    for schema, name, kind in deprecated:
        when = renamed_at.get((label, schema, name))
        if when is None:
            if include_unknown:
                kept.append((schema, name, kind))
            else:
                console.print(
                    f"[yellow]{_tag(label)} {esc(schema)}.{esc(name)}: no recorded "
                    f"rename; skipping (pass --include-unknown to drop "
                    f"anyway).[/yellow]"
                )
            continue
        if when <= cutoff:
            kept.append((schema, name, kind))
        else:
            console.print(
                f"[dim]{_tag(label)} {esc(schema)}.{esc(name)}: renamed "
                f"{when.date()}, inside the grace period; skipping.[/dim]"
            )
    return kept


def _drop_one(
    cur: Any,
    label: str,
    schema: str,
    name: str,
    kind: ObjectKind,
    *,
    dry_run: bool,
) -> DropEntry | None:
    action = "[DRY-RUN] Would drop" if dry_run else "Dropping"
    console.print(
        f"[blue]{_tag(label)} {action} {kind} {esc(schema)}.{esc(name)}[/blue]"
    )
    if not dry_run:
        drop_object(cur, schema, name, kind)
    return DropEntry(database=label, schema=schema, name=name, kind=kind)


def _write_purge_log(log_path: str, entries: list[DropEntry], *, dry_run: bool) -> None:
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "dry_run": dry_run,
        "source": "dbt-orphans-purge",
        "drops": entries,
    }
    with open(log_path, "w") as f:
        json.dump(payload, f, indent=4)
