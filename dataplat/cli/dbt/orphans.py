"""`dp dbt orphans` — discover and rename orphan dbt tables."""

from __future__ import annotations

import glob
import json
import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import typer
from rich.console import Console

from dataplat.cli._exit import exit_code_for, fail
from dataplat.cli._options import YesOption
from dataplat.cli._prompt import confirm_or_exit
from dataplat.cli._render import esc
from dataplat.cli.dbt._common import (
    ProjectOption,
    TargetFilterOption,
    projects_and_targets,
)
from dataplat.core.errors import (
    ConfigError,
    DataplatError,
    ServiceError,
    ValidationError,
)
from dataplat.services.db.capabilities import Capability, require_capability
from dataplat.services.db.connection import DbConnectionParams, SqlEngine
from dataplat.services.db.orphans import (
    DEPRECATED_SUFFIX,
    LIVE_STATUSES,
    BlockedEntry,
    DependentObjectsError,
    DropEntry,
    ObjectKind,
    RenameEntry,
    classify_object,
    diff_orphans,
    drop_object,
    fetch_deprecated_objects,
    fetch_existing_relations,
    fetch_live_model_relations,
    open_transactional_connection,
    rename_object,
    resolve_orphans_connection_params,
)
from dataplat.services.db.targets import ALL_TARGETS, DbTarget, resolve_targets
from dataplat.services.dbt.manifest import is_partition_of, relation_names
from dataplat.services.dbt.projects import DbtProject, default_project_name
from dataplat.services.dbt.settings import (
    excluded_schemas,
    invocation_command,
    node_prefix,
)

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

# Why this command needs `rename_with_dependents`, which the capability's own
# reason ("renames fail when a view depends on the table") does not say. Written
# once and passed to every check, because all three subcommands — apply, revert
# and purge — rest on the same mechanism.
_RENAME_DETAIL = (
    "dbt-orphans quarantines an orphan by renaming it (and revert renames it "
    "back), so a dependent view does not merely complicate the rename, it "
    "refuses the one operation this command is. In a dbt project a view on a "
    "model is the normal case, not the exception. `purge` then drops what was "
    "renamed, and a half-working destructive command is worse than none."
)

# ``database`` values a pre-named-project audit log used: the engine family
# (postgres/redshift), not a target name — see _engines_for_project's
# docstring for why the identity moved to the target's own name. Revert's
# log filter and purge's rename-age index both still have to recognize the
# legacy value: a log written before that migration cannot carry an identity
# that did not exist yet, and refusing to match it at all silently no-ops
# every revert of a pre-upgrade rename while reporting success — the exact
# hazard this migration must not introduce. The write side never produces
# these anymore; this is read-only backward compatibility.
#
# The legacy value is only safe to trust when exactly one target in the
# current invocation has that engine. A legacy log cannot tell two
# same-engine targets apart -- the distinction it would need was never
# recorded -- so matching it against *every* same-engine target reintroduces
# the exact cross-application the target-name identity exists to prevent,
# just for old logs instead of new ones. See _ambiguous_legacy_conflicts:
# when more than one target shares an engine, the legacy fallback for that
# engine is refused outright rather than guessed, and the operator is told
# to rerun scoped to one target with --target.
_LEGACY_ENGINE_LABELS: dict[SqlEngine, str] = {
    SqlEngine.postgresql: "postgres",
    SqlEngine.redshift: "redshift",
}


def _ambiguous_legacy_conflicts(
    present_identities: set[str],
    engines: list[tuple[str, SqlEngine, str, DbtProject | None]],
) -> list[tuple[str, list[str]]]:
    """``(legacy_label, [target names])`` for every legacy identity that
    cannot be safely attributed to one target in this invocation.

    A conflict exists when a legacy engine-family value is actually present
    in the data being read (``present_identities`` — a log's ``database``
    values for revert, or a rename-age index's keys for purge) *and* more
    than one target in ``engines`` shares that legacy value's engine. Two
    different targets on the same engine each independently produce the
    same legacy value, and nothing recorded which of them a given legacy
    entry belongs to.
    """
    engine_targets: dict[SqlEngine, set[str]] = {}
    for label, engine, _env_prefix, _project in engines:
        engine_targets.setdefault(engine, set()).add(label)

    conflicts: list[tuple[str, list[str]]] = []
    for engine, targets in sorted(engine_targets.items(), key=lambda kv: kv[0].value):
        if len(targets) <= 1:
            continue
        legacy_label = _LEGACY_ENGINE_LABELS.get(engine)
        if legacy_label is not None and legacy_label in present_identities:
            conflicts.append((legacy_label, sorted(targets)))
    return conflicts


def _refuse_ambiguous_legacy_entries(
    present_identities: set[str],
    engines: list[tuple[str, SqlEngine, str, DbtProject | None]],
    *,
    context: str,
) -> None:
    """Raise if any legacy identity in ``present_identities`` is ambiguous.

    ``context`` names what was read (``"log"`` for revert, ``"rename-age
    index"`` for purge) so the error says what predates per-target identity.
    """
    conflicts = _ambiguous_legacy_conflicts(present_identities, engines)
    if not conflicts:
        return
    detail = "; ".join(
        f"'{legacy_label}' could mean any of {', '.join(targets)}"
        for legacy_label, targets in conflicts
    )
    raise ValidationError(
        f"Refusing: this {context} predates per-target identity, and it "
        f"cannot be attributed safely in this invocation: {detail}. A "
        "legacy entry recorded only the engine, not which target produced "
        "it, so applying it to every target that shares that engine risks "
        "acting on one target using another's history. Rerun scoped to a "
        "single target with --target to make the attribution unambiguous."
    )


def _tag(label: str) -> str:
    """The ``[<target>]`` prefix every progress line carries.

    ``label`` is the target's own name (``demo_pg``, not ``postgres``) — see
    ``_engines_for_project`` for why — and a target name is exactly as
    user-chosen as a schema or relation name. A target happening to be named
    after a real Rich style (``red``, ``bold``) is a well-formed tag that,
    left unescaped, would be parsed as a style and silently dropped, leaving
    the reader unable to tell which target a rename or drop line belonged to.
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


def _connection_identity(tgt: DbTarget) -> tuple[str, int, str] | None:
    """Best-effort ``(host, port, dbname)`` for ``tgt``, or ``None``.

    ``resolve_orphans_connection_params`` only reads environment variables —
    no connection is opened — so this is safe to call for every resolved
    target before any capability check or connection attempt. ``None`` covers
    every case this cannot compare: missing connection settings, a
    non-libpq shape (DuckDB resolves to a different params type entirely),
    or a config problem in the settings themselves (e.g. a non-integer
    port). A target that cannot be read this way is excluded from the
    overlap check rather than treated as either a collision or a clearance —
    "cannot tell" is not the same claim as "distinct".
    """
    try:
        params = resolve_orphans_connection_params(
            tgt.engine, env_prefix=tgt.env_prefix
        )
    except ConfigError:
        return None
    if not isinstance(params, DbConnectionParams):
        return None
    return (params.host, params.port, params.dbname)


def _refuse_overlapping_targets(
    pairs: list[tuple[DbtProject | None, list[DbTarget]]],
) -> None:
    """Refuse a fan-out where two or more resolved projects share a warehouse.

    Each project's live dbt model set is scoped to that project alone (see
    ``_engines_for_project``), so scanning the same physical warehouse once
    per project that declares it does not fail safe: each pass would see
    every *other* project's live tables as its own orphans, because the live
    sets are never unioned across the fan-out. With ``--no-dry-run`` that is
    two projects' production tables quarantined in one invocation.

    Two checks, because "the same warehouse" shows up here two ways. Two
    projects can declare the identical target *name* — caught by comparing
    names. Two projects can also each declare their *own*, differently-named
    target that happens to resolve to the same host, port and database:
    separate credentials per project pointed at one shared cluster is an
    ordinary setup, not a misconfiguration, and the name-based check alone
    lets it straight through. The second check (see
    ``_connection_identity``) is best-effort by nature: it can only compare
    targets whose connection settings actually resolve, and even then a DNS
    alias, or the same host written two different ways, still slips past it.
    It narrows this hole; it does not close it.

    Unioning the live sets instead of refusing was considered and rejected:
    a fan-out whose safety depends on a set union being exactly right, with
    no test surface today, is not something a command that renames and drops
    things should carry. Refusing outright is boring and obviously correct —
    the operator reruns the overlapping projects one at a time.
    """
    by_name: dict[str, list[str]] = {}
    by_connection: dict[tuple[str, int, str], list[tuple[str, str]]] = {}
    for proj, targets in pairs:
        if proj is None:
            continue
        for tgt in targets:
            by_name.setdefault(tgt.name, []).append(proj.name)
            identity = _connection_identity(tgt)
            if identity is not None:
                by_connection.setdefault(identity, []).append((proj.name, tgt.name))

    details: list[str] = [
        f"{name} (built into by {', '.join(projs)})"
        for name, projs in sorted(by_name.items())
        if len(projs) > 1
    ]
    for (host, port, dbname), entries in sorted(by_connection.items()):
        distinct_projects = {proj for proj, _ in entries}
        if len(distinct_projects) <= 1:
            continue
        who = ", ".join(f"{proj} ({tgt})" for proj, tgt in sorted(set(entries)))
        details.append(
            f"{host}:{port}/{dbname} (built into by {who}, under different "
            "target names)"
        )

    if not details:
        return
    raise ValidationError(
        "Refusing: more than one project builds into the same warehouse in "
        f"this invocation: {'; '.join(details)}. Each project's live dbt "
        "model set is its own, so scanning a shared warehouse once per "
        "project would rename one project's live tables as another's "
        "orphans. (The differently-named-target check is best-effort, "
        "matched on host, port and database name — a DNS alias or an "
        "address written differently can still slip through.) Run the "
        "overlapping projects one at a time (--project <name>) instead."
    )


def _engines_for_project(
    project: str | None, target: str | None
) -> list[tuple[str, SqlEngine, str, DbtProject | None]]:
    """Return ``(label, engine, env_prefix, project)`` per target of the named project.

    ``label`` is the target's own name, not its engine family: the summary,
    the audit log's ``database`` field and revert's log filter all key on it,
    and two targets that share an engine (two Postgres clusters, say) would
    be indistinguishable from each other if it were the engine name instead —
    revert could then rename one cluster's objects back on the strength of
    another cluster's log entries.

    Still the one gate every subcommand passes through, which is why the
    capability check stays here. An engine that cannot rename a relation a view
    depends on refuses the whole invocation, including a fan-out where other
    targets would have worked: this command renames and drops, so "did some of
    it" is the outcome worth avoiding most.

    No ``--project`` given and no project configured at all — ``DP_DBT_PROJECTS``
    unset, nothing to default to — falls back to resolving ``DP_TARGETS``
    directly instead of refusing to run: that is the legacy, pre-named-project
    shape this command has always supported, and ``project=None`` rides along
    so ``node_prefix``/``excluded_schemas``/``invocation_command`` read the
    legacy single-project env vars the way they always did. Passing an
    explicit ``--project`` still resolves (and can still fail) normally even
    when nothing is configured — the operator asked for a specific project by
    name, so silently falling back to "all targets" would run the command
    against warehouses that project may never touch.

    The ``project`` carried alongside each target (rather than resolved once
    for the whole run) is what makes ``--project all`` correct: each project
    fanned out by :func:`projects_and_targets` has its own node prefix and
    exclusion settings. See ``_refuse_overlapping_targets`` for what happens
    when two of them declare the same target.
    """
    try:
        if project is None and default_project_name() is None:
            targets = resolve_targets(target or ALL_TARGETS)
            pairs: list[tuple[DbtProject | None, list[DbTarget]]] = [(None, targets)]
        else:
            pairs = [
                (proj, tgts) for proj, tgts in projects_and_targets(project, target)
            ]
        _refuse_overlapping_targets(pairs)
    except DataplatError as exc:
        fail(exc, console=console)

    resolved: list[tuple[str, SqlEngine, str, DbtProject | None]] = []
    for proj, targets in pairs:
        for tgt in targets:
            try:
                require_capability(
                    tgt.engine,
                    Capability.rename_with_dependents,
                    command="dp dbt orphans",
                    detail=_RENAME_DETAIL,
                )
            except ValidationError as exc:
                # Prefixed with the target name, like every other per-target error
                # in this module: with a multi-target fan-out the engine's reason
                # alone would not say which target brought the command to a stop.
                # fail() escapes the brackets before Rich sees them.
                fail(ValidationError(f"[{tgt.name}] {exc}"), console=console)
            resolved.append((tgt.name, tgt.engine, tgt.env_prefix, proj))
    return resolved


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
    project: str | None = ProjectOption,
    target: str | None = TargetFilterOption,
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
        # The message quotes the offending --exclude token back at the user, so
        # fail() escaping it is what keeps a hostile token from crashing Rich.
        fail(exc, console=console)

    engines = _engines_for_project(project, target)
    if not dry_run:
        confirm_or_exit(
            yes=yes,
            prompt="Rename every orphaned dbt object with a _deprecated suffix?",
            console=console,
        )

    since = datetime.now(UTC) - timedelta(days=window_days)

    all_entries: list[RenameEntry] = []
    try:
        for label, engine, env_prefix, proj in engines:
            all_entries.extend(
                _run_for_engine(
                    label,
                    engine,
                    env_prefix=env_prefix,
                    project=proj,
                    excluded_user_schemas=excluded_user_schemas,
                    excluded_user_relations=excluded_user_relations,
                    window_days=window_days,
                    since=since,
                    dry_run=dry_run,
                )
            )
    except DataplatError as exc:
        _write_audit_log(log, all_entries, dry_run=dry_run)
        console.print(f"[red]{esc(exc)}[/red]")
        console.print(
            f"[yellow]Partial audit log written to {esc(log)} "
            f"({len(all_entries)} entries).[/yellow]"
        )
        # exit_code_for, not fail(): the log path is the actionable half of this
        # failure and has to be printed *after* the error, which fail() cannot do
        # because it exits. The code still comes from the exception, which is
        # also why the handler catches DataplatError rather than ServiceError —
        # a ConfigError from a per-engine step keeps its own 3.
        raise typer.Exit(code=exit_code_for(exc))

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


def _produced_relations(project: DbtProject | None) -> set[str] | None:
    """Relation names ``project``'s manifest says it produces, or ``None``.

    ``None`` is the legacy, pre-named-project path (see
    ``dataplat.services.dbt.settings``'s own module docstring): there is no
    manifest to read there, so this reads nothing and the manifest-based
    checks in ``_run_for_engine`` (produced-set exclusion, partition sparing)
    simply do not run for it -- the exact behaviour that path has always had,
    not a new refusal.

    For a configured project, a manifest that cannot be read at all (never
    compiled, or corrupt) and a manifest that reports zero produced relations
    both refuse rather than let the scan continue. The second case matters as
    much as the first: an empty produced set is never a reason to think a
    project produces nothing (a genuinely empty project has no warehouse
    tables to scan in the first place), only a reason to think the manifest
    is wrong -- and diffing against an empty produced set would make every
    object already in scope look orphaned.
    """
    if project is None:
        return None
    try:
        produced = relation_names(project)
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        raise ConfigError(str(exc)) from exc
    if not produced:
        raise ConfigError(
            f"{project.name}'s manifest ({project.path / 'target' / 'manifest.json'}) "
            "reports zero produced relations. Refusing: diffing against an "
            "empty produced set would treat every object already in scope as "
            "an orphan. Run `dbt compile` (or `dbt docs generate`) for a "
            "build that actually produces something, then re-run."
        )
    return produced


def _produced_predicate(produced: set[str] | None) -> Callable[[str], bool] | None:
    """The ``is_produced`` callable ``diff_orphans`` consults, or ``None``.

    ``None`` when there is no manifest to consult (see ``_produced_relations``)
    -- ``diff_orphans`` treats that the same as never having been passed the
    argument at all, which is what keeps the legacy path's behaviour
    unchanged.

    Lowercases the candidate before comparing. ``relation_names`` already
    lowercases everything it returns, and ``is_partition_of`` lowercases its
    own ``relation`` argument too, but the plain membership check right here
    does not get that for free -- a warehouse catalog can report a
    quoted, mixed-case relation name, and comparing it against the
    always-lowercase produced set without lowercasing it first would quietly
    stop matching for exactly that project.
    """
    if produced is None:
        return None

    def _is_produced(name: str) -> bool:
        lowered = name.lower()
        return lowered in produced or is_partition_of(lowered, produced)

    return _is_produced


def _run_for_engine(
    label: str,
    engine: SqlEngine,
    *,
    env_prefix: str,
    project: DbtProject | None = None,
    excluded_user_schemas: frozenset[str],
    excluded_user_relations: frozenset[tuple[str, str]],
    window_days: int,
    since: datetime,
    dry_run: bool,
) -> list[RenameEntry]:
    try:
        params = resolve_orphans_connection_params(engine, env_prefix=env_prefix)
        dbt_node_prefix = node_prefix(project)
        produced = _produced_relations(project)
    except ConfigError as exc:
        # Re-raised as the same class, not widened to ServiceError: "set
        # DP_DBT_PROJECT" is something the operator fixes (exit 3), and a CI job
        # told the warehouse failed (5) would retry it forever. The label is the
        # only reason this is re-raised at all.
        raise ConfigError(f"[{label}] {exc}") from exc
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
            invocation_command=invocation_command(project),
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

        excluded = excluded_schemas(project)
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
            is_produced=_produced_predicate(produced),
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
    project: str | None = ProjectOption,
    target: str | None = TargetFilterOption,
    dry_run: bool = typer.Option(
        True,
        "--dry-run/--no-dry-run",
        help="Preview without changes (default). Pass --no-dry-run to revert.",
    ),
) -> None:
    """Undo a previous dbt-orphans run using the audit log."""
    # Resolved before the log is even located, so an engine that cannot rename
    # is refused whether or not a previous run left a log behind. The other
    # order answers "no dbt_orphans log found" to a target where there could
    # never have been one, sending the reader to look for a missing file
    # instead of at the engine.
    engines = _engines_for_project(project, target)
    if log is None:
        log = _find_latest_log(APPLY_LOG_PREFIX)
        if log is None:
            console.print(
                f"[red]Error: no {APPLY_LOG_PREFIX} log found in {esc(LOG_DIR)}/[/red]"
            )
            console.print(
                "[dim]Run `dp dbt orphans` first or pass --log explicitly.[/dim]"
            )
            raise typer.Exit(code=1)
        console.print(f"[dim]Using latest log: {esc(log)}[/dim]")
    if not os.path.exists(log):
        console.print(f"[red]Error: log file not found: {esc(log)}[/red]")
        console.print("[dim]Run `dp dbt orphans` first to generate it.[/dim]")
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

    try:
        _refuse_ambiguous_legacy_entries(
            {
                r.get("database")
                for r in renames
                if isinstance(r, dict) and r.get("database") is not None
            },
            engines,
            context="log",
        )
    except DataplatError as exc:
        fail(exc, console=console)

    total = 0
    try:
        for label, engine, env_prefix, _project in engines:
            # A legacy log's entries carry the old engine-family value (see
            # _LEGACY_ENGINE_LABELS), not this target's name; matching only
            # `label` would silently revert nothing from any log written
            # before this migration.
            wanted = {label, _LEGACY_ENGINE_LABELS.get(engine)}
            entries = [
                r
                for r in renames
                if isinstance(r, dict) and r.get("database") in wanted
            ]
            if not entries:
                console.print(f"[dim]{_tag(label)} No entries in log.[/dim]")
                continue
            total += _revert_for_engine(
                label, engine, entries, env_prefix=env_prefix, dry_run=dry_run
            )
    except DataplatError as exc:
        console.print(f"[red]{esc(exc)}[/red]")
        console.print(
            f"[yellow]Reverted {total} object(s) before the failure.[/yellow]"
        )
        # See the note in `main`: the count printed after the error is why this
        # takes the code from the exception instead of calling fail().
        raise typer.Exit(code=exit_code_for(exc))

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
        # Re-raised as the same class, not widened to ServiceError: "set
        # DP_DBT_PROJECT" is something the operator fixes (exit 3), and a CI job
        # told the warehouse failed (5) would retry it forever. The label is the
        # only reason this is re-raised at all.
        raise ConfigError(f"[{label}] {exc}") from exc
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
    project: str | None = ProjectOption,
    target: str | None = TargetFilterOption,
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
    """Permanently drop every object ending in _deprecated (irreversible).

    All-or-nothing per target, by design: every drop for one warehouse runs
    inside a single transaction, so if the warehouse refuses one of them
    nothing is dropped for that target and the error names the blocking
    relation. Keeping the drops that already succeeded would need either a
    SAVEPOINT per object (Redshift has none) or a commit per object (which
    would break the guarantee that --dry-run writes nothing) — and for a
    destructive batch, stopping on the first surprise is the safer default. No
    CASCADE is issued either, so a live view or foreign key still pointing at a
    _deprecated relation is a real signal: resolve the dependents the error
    lists, then re-run.
    """
    if log is None:
        log = _timestamped_log_path(PURGE_LOG_PREFIX)

    try:
        excluded_user_schemas, excluded_user_relations = _parse_exclusions(
            exclude, exclude_file
        )
    except ValidationError as exc:
        fail(exc, console=console)

    engines = _engines_for_project(project, target)
    if not dry_run:
        confirm_or_exit(
            yes=yes,
            prompt=(
                "Permanently DROP every *_deprecated object? This cannot be undone."
            ),
            console=console,
        )

    renamed_at = _renamed_at_index() if older_than is not None else None
    if renamed_at is not None:
        try:
            _refuse_ambiguous_legacy_entries(
                {key[0] for key in renamed_at},
                engines,
                context="rename-age index",
            )
        except DataplatError as exc:
            fail(exc, console=console)
    cutoff = (
        datetime.now(UTC) - timedelta(days=older_than)
        if older_than is not None
        else None
    )

    all_drops: list[DropEntry] = []
    blocked: list[BlockedEntry] = []
    try:
        for label, engine, env_prefix, proj in engines:
            try:
                all_drops.extend(
                    _purge_for_engine(
                        label,
                        engine,
                        env_prefix=env_prefix,
                        project=proj,
                        excluded_user_schemas=excluded_user_schemas,
                        excluded_user_relations=excluded_user_relations,
                        dry_run=dry_run,
                        renamed_at=renamed_at,
                        cutoff=cutoff,
                        include_unknown=include_unknown,
                    )
                )
            except DependentObjectsError as exc:
                # The engine's transaction is already rolled back, so the drops
                # it had made are (correctly) absent from all_drops. Record the
                # refused attempt so the audit log shows why this target did
                # nothing, then re-raise with the [<engine>] prefix every other
                # error from this command carries.
                blocked.append(
                    BlockedEntry(
                        database=label,
                        schema=exc.schema,
                        name=exc.name,
                        kind=exc.kind,
                        dependents=exc.dependents,
                    )
                )
                raise ServiceError(f"[{label}] {exc}") from exc
    except DataplatError as exc:
        _write_purge_log(log, all_drops, dry_run=dry_run, blocked=blocked)
        console.print(f"[red]{esc(exc)}[/red]")
        if blocked:
            console.print(
                "[yellow]Nothing was dropped for that target: the purge is one "
                "transaction, so a refused drop rolls the whole batch "
                "back.[/yellow]"
            )
        console.print(
            f"[yellow]Partial purge log written to {esc(log)} "
            f"({len(all_drops)} entries).[/yellow]"
        )
        # See the note in `main`. DependentObjectsError is a ServiceError too, so
        # a refused drop and an unreachable warehouse both exit 5 — which is
        # right: in both cases the warehouse, not the invocation, said no.
        raise typer.Exit(code=exit_code_for(exc))

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
    project: DbtProject | None = None,
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
        # Re-raised as the same class, not widened to ServiceError: "set
        # DP_DBT_PROJECT" is something the operator fixes (exit 3), and a CI job
        # told the warehouse failed (5) would retry it forever. The label is the
        # only reason this is re-raised at all.
        raise ConfigError(f"[{label}] {exc}") from exc
    if params is None:
        console.print(
            f"[yellow]{_tag(label)} Missing connection parameters, skipping.[/yellow]"
        )
        return []

    is_redshift = engine is SqlEngine.redshift
    effective_excluded_schemas = excluded_schemas(project) | excluded_user_schemas
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
                legacy_label=_LEGACY_ENGINE_LABELS.get(engine),
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
    legacy_label: str | None,
    renamed_at: dict[tuple[str, str, str], datetime],
    cutoff: datetime,
    include_unknown: bool,
) -> list[tuple[str, str, Any]]:
    """Keep only objects renamed before ``cutoff`` per the audit logs.

    ``renamed_at`` is built from every apply log on disk (see
    ``_renamed_at_index``), old and new alike, so a rename an old log
    recorded under the legacy engine-family value has to be found under
    ``legacy_label`` too — otherwise every pre-upgrade rename looks like it
    has no recorded rename at all, which ``--older-than`` (without
    ``--include-unknown``) then skips outright.
    """
    kept: list[tuple[str, str, Any]] = []
    for schema, name, kind in deprecated:
        when = renamed_at.get((label, schema, name))
        if when is None and legacy_label is not None:
            when = renamed_at.get((legacy_label, schema, name))
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


def _write_purge_log(
    log_path: str,
    entries: list[DropEntry],
    *,
    dry_run: bool,
    blocked: list[BlockedEntry] | None = None,
) -> None:
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "dry_run": dry_run,
        "source": "dbt-orphans-purge",
        "drops": entries,
        # A refused drop rolls its target's transaction back, so "drops" cannot
        # hold the attempt; recorded separately or the log would show a purge
        # that raised as having done nothing at all.
        "blocked": blocked or [],
    }
    with open(log_path, "w") as f:
        json.dump(payload, f, indent=4)
