"""Schema inspection and administration, engine-independent.

The dialects in :mod:`dataplat.services.db.schema_dialects` own the SQL; this
module owns the shapes that come back out of it, the argument parsing that goes
in, and the plans that say what a command will do. It opens no connections.

Every builder here validates completely before emitting a single op, so a typo in
the last argument fails before the first statement runs.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

from psycopg import sql

from dataplat.core.errors import ValidationError
from dataplat.services.db.connection import SqlEngine
from dataplat.services.db.role_admin import parse_csv_flag
from dataplat.services.db.role_dialects import ParentKind, SqlOp

if TYPE_CHECKING:
    from dataplat.services.db.schema_dialects import SchemaDialect

__all__ = [
    "DEFAULT_LEVEL",
    "PRIVILEGE_ORDER",
    "PRIVILEGE_PRESETS",
    "SCHEMA_LEVEL",
    "TABLE_LEVEL",
    "CreateSchemaSpec",
    "GranteeSpec",
    "SchemaPlan",
    "SchemaPrivilege",
    "SchemaSummary",
    "build_alter_plan",
    "build_create_plan",
    "build_drop_plan",
    "build_grant_plan",
    "parse_grant_spec",
    "parse_privileges",
    "parse_quota",
    "translate_like_pattern",
]


@dataclass(frozen=True)
class SchemaSummary:
    """One row of ``dp db schema list``.

    ``quota_mb`` / ``used_mb`` are Redshift-only and stay ``None`` everywhere
    else — including on a Redshift cluster whose quota view is unavailable. That
    is why they are ``None`` rather than ``0``: callers must render them as
    *unknown*, because a schema with an unknown quota is not a schema with no
    quota.

    ``other`` counts relations that are neither table-like nor view-like
    (sequences, composite types) — anything that still blocks a ``RESTRICT``
    drop and is still destroyed by ``CASCADE``, but has no more specific bucket.
    It exists so that nothing a drop would destroy goes uncounted; ``list`` does
    not render it, ``drop`` does.
    """

    name: str
    owner: str
    tables: int
    views: int
    quota_mb: int | None = None
    used_mb: int | None = None
    other: int = 0

    @property
    def object_count(self) -> int:
        """Everything a ``CASCADE`` would destroy, for the drop pre-flight."""
        return self.tables + self.views + self.other


def translate_like_pattern(pattern: str) -> str:
    """Accept glob ``*`` as a friendlier spelling of SQL ``LIKE``'s ``%``.

    ``dev_*`` and ``dev_%`` both work, because a filter is the one place an
    operator expects shell habits to apply.

    ``_`` is left alone. It *is* a single-character wildcard in ``LIKE``, so
    ``dev_x`` also matches ``devax`` — but every schema in these warehouses uses
    ``_`` as a literal word separator, and over-matching by one character in a
    read-only filter is harmless. Where over-matching is *not* harmless — a
    pattern that selects schemas to drop — the caller escapes it instead, with
    :func:`~dataplat.services.db._like.like_escape`.
    """
    return pattern.replace("*", "%")


# ---------------------------------------------------------------------------
# Quota
# ---------------------------------------------------------------------------

_QUOTA_RE = re.compile(r"^(\d+)\s*(MB|GB|TB)$", re.IGNORECASE)


def parse_quota(text: str) -> str:
    """Normalize a quota to the form Redshift's ``QUOTA`` clause wants.

    Accepts ``50GB``, ``50 gb``, ``1024MB``, ``UNLIMITED`` (case-insensitive) and
    returns ``"50 GB"`` / ``"UNLIMITED"``.

    This is validation *and* sanitization. A quota is neither an identifier nor a
    bindable parameter in DDL, so the dialect interpolates the result into
    statement text — and this regex is the whole guarantee that what gets
    interpolated contains nothing but digits, one space, and a known unit
    keyword. The returned string is rebuilt from the parsed groups rather than
    passed through, so no part of the caller's input survives verbatim.

    Idempotent, so re-running it on already-normalized input is a no-op — which
    is what lets :class:`CreateSchemaSpec` re-normalize defensively.
    """
    cleaned = text.strip()
    if cleaned.upper() == "UNLIMITED":
        return "UNLIMITED"
    match = _QUOTA_RE.match(cleaned)
    if match is None:
        raise ValidationError(
            f'invalid quota "{text}". Expected <int>MB|GB|TB or UNLIMITED '
            "(e.g. 50GB, 1024MB, UNLIMITED)."
        )
    amount, unit = match.groups()
    if int(amount) <= 0:
        raise ValidationError(f'invalid quota "{text}". Amount must be > 0.')
    return f"{int(amount)} {unit.upper()}"


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CreateSchemaSpec:
    """One schema to create.

    ``quota`` is re-normalized through :func:`parse_quota` in ``__post_init__``,
    so the invariant the dialect trusts — that a value interpolated into DDL text
    contains nothing but digits, one space, and a known unit keyword — holds by
    construction, not merely because the CLI happened to call ``parse_quota``
    first. A library caller constructing this directly gets the same guarantee.
    """

    name: str
    owner: str | None = None
    quota: str | None = None
    if_not_exists: bool = False

    def __post_init__(self) -> None:
        if self.quota is not None:
            # frozen=True forbids plain attribute assignment.
            object.__setattr__(self, "quota", parse_quota(self.quota))


@dataclass(frozen=True)
class SchemaPlan:
    """Ordered ops for one ``dp db schema`` invocation, plus any warnings.

    Flat, unlike :class:`~dataplat.services.db.role_admin.CreatePlan`: schemas
    are per-database objects and every schema subcommand targets exactly one
    database, so there is no cluster / per-database split to model.
    """

    ops: list[SqlOp] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: (schema, grantee, privilege) triples skipped as already in effect.
    already_held: tuple[tuple[str, str, str], ...] = ()


def _warn_once(warnings: list[str], message: str) -> None:
    """Append ``message`` unless it is already there.

    Mirrors ``role_admin._warn_once``: a degradation that applies per schema
    should be reported once, not once per schema.
    """
    if message not in warnings:
        warnings.append(message)


def _require_name(name: str) -> None:
    if not name.strip():
        raise ValidationError("schema name must not be empty")


def build_create_plan(
    specs: Sequence[CreateSchemaSpec], dialect: SchemaDialect
) -> SchemaPlan:
    """``CREATE SCHEMA`` per spec, in the order given."""
    if not specs:
        raise ValidationError("at least one schema name is required")

    ops: list[SqlOp] = []
    warnings: list[str] = []
    for spec in specs:
        _require_name(spec.name)
        quota = spec.quota
        if quota and dialect.engine != SqlEngine.redshift:
            _warn_once(
                warnings, "ignoring --quota: only Redshift supports schema quotas"
            )
        owner = spec.owner
        if owner and not dialect.supports_authorization:
            # The CLI refuses this outright, with the engine's own reason. This
            # is the service-layer half: a library caller must not be able to
            # generate CREATE SCHEMA ... AUTHORIZATION for an engine whose parser
            # rejects the keyword.
            _warn_once(
                warnings,
                f"ignoring --owner: {dialect.engine.value} has no schema owners",
            )
            owner = None
        ops.append(
            dialect.create_schema(
                spec.name,
                owner=owner,
                quota=quota,
                if_not_exists=spec.if_not_exists,
            )
        )
    return SchemaPlan(ops=ops, warnings=warnings)


def build_drop_plan(
    names: Sequence[str], *, cascade: bool = False, if_exists: bool = False
) -> SchemaPlan:
    """``DROP SCHEMA`` per name. Identical syntax on all three engines.

    ``RESTRICT`` is emitted explicitly rather than left to the server default, so
    the rendered plan states exactly what will run — the difference between
    "fails if anything is in it" and "destroys everything in it" is the whole
    question this command turns on.
    """
    if not names:
        raise ValidationError("at least one schema name is required")

    ops: list[SqlOp] = []
    for name in names:
        _require_name(name)
        parts = ["DROP SCHEMA"]
        label = ["DROP SCHEMA"]
        if if_exists:
            parts.append("IF EXISTS")
            label.append("IF EXISTS")
        parts.append("{s}")
        label.append(name)
        tail = "CASCADE" if cascade else "RESTRICT"
        parts.append(tail)
        label.append(tail)
        ops.append(
            SqlOp(
                description=" ".join(label),
                statement=sql.SQL(" ".join(parts)).format(s=sql.Identifier(name)),
            )
        )
    return SchemaPlan(ops=ops)


def build_alter_plan(
    names: Sequence[str],
    dialect: SchemaDialect,
    *,
    owner: str | None = None,
    quota: str | None = None,
    rename_to: str | None = None,
) -> SchemaPlan:
    """``ALTER SCHEMA`` for owner, quota, and rename.

    ``--quota`` off Redshift warns and skips when there is other work to do, but
    is an error when it is the *only* change requested — a warn-and-skip there
    would leave the command doing nothing at all while reporting success.
    """
    if not names:
        raise ValidationError("at least one schema name is required")
    if not any((owner, quota, rename_to)):
        raise ValidationError("nothing to do: pass --owner, --quota, or --rename-to")
    if rename_to and len(names) != 1:
        raise ValidationError("--rename-to takes exactly one schema name")
    if quota is not None:
        # Re-normalize rather than trust the caller: this value is interpolated
        # into DDL text by RedshiftSchemaDialect.alter_quota. Idempotent, so
        # re-running it on already-normalized input is a no-op. Mirrors
        # CreateSchemaSpec.__post_init__, which hardens the same danger.
        quota = parse_quota(quota)

    quota_supported = dialect.engine == SqlEngine.redshift
    if quota and not quota_supported and not owner and not rename_to:
        raise ValidationError(
            "only Redshift supports schema quotas, and --quota is the only "
            "change requested"
        )

    ops: list[SqlOp] = []
    warnings: list[str] = []
    for name in names:
        _require_name(name)
        if owner:
            ops.append(dialect.alter_owner(name, owner))
        if quota:
            op = dialect.alter_quota(name, quota)
            if op is None:
                _warn_once(
                    warnings, "ignoring --quota: only Redshift supports schema quotas"
                )
            else:
                ops.append(op)
        if rename_to:
            ops.append(dialect.rename_schema(name, rename_to))
    return SchemaPlan(ops=ops, warnings=warnings)


# ---------------------------------------------------------------------------
# Privileges
# ---------------------------------------------------------------------------


class SchemaPrivilege(str, Enum):
    """One grantable privilege in the ``dp db schema`` vocabulary.

    Values are the CLI spelling: ``--privileges table-all`` maps to
    ``table_all``.
    """

    # Schema-scoped
    usage = "usage"
    create = "create"
    all = "all"
    # Every existing table in the schema
    select = "select"
    insert = "insert"
    update = "update"
    delete = "delete"
    table_all = "table-all"
    # Future tables (ALTER DEFAULT PRIVILEGES)
    default_select = "default-select"
    default_all = "default-all"
    # Other object classes
    sequence_usage = "sequence-usage"
    function_execute = "function-execute"


SCHEMA_LEVEL = frozenset(
    {SchemaPrivilege.usage, SchemaPrivilege.create, SchemaPrivilege.all}
)
TABLE_LEVEL = frozenset(
    {
        SchemaPrivilege.select,
        SchemaPrivilege.insert,
        SchemaPrivilege.update,
        SchemaPrivilege.delete,
        SchemaPrivilege.table_all,
    }
)
DEFAULT_LEVEL = frozenset({SchemaPrivilege.default_select, SchemaPrivilege.default_all})

#: Emission order. Schema-scoped grants come first so USAGE is in place before
#: anything that depends on it, and default privileges last because they are the
#: only ones describing objects that do not exist yet.
PRIVILEGE_ORDER: tuple[SchemaPrivilege, ...] = (
    SchemaPrivilege.usage,
    SchemaPrivilege.create,
    SchemaPrivilege.all,
    SchemaPrivilege.select,
    SchemaPrivilege.insert,
    SchemaPrivilege.update,
    SchemaPrivilege.delete,
    SchemaPrivilege.table_all,
    SchemaPrivilege.sequence_usage,
    SchemaPrivilege.function_execute,
    SchemaPrivilege.default_select,
    SchemaPrivilege.default_all,
)

PRIVILEGE_PRESETS: dict[str, tuple[SchemaPrivilege, ...]] = {
    "read": (
        SchemaPrivilege.usage,
        SchemaPrivilege.select,
        SchemaPrivilege.default_select,
    ),
    "readwrite": (
        SchemaPrivilege.usage,
        SchemaPrivilege.create,
        SchemaPrivilege.table_all,
        SchemaPrivilege.default_all,
    ),
}

#: Privileges that are meaningless without USAGE on the containing schema.
_IMPLIES_USAGE = (
    TABLE_LEVEL
    | DEFAULT_LEVEL
    | {SchemaPrivilege.sequence_usage, SchemaPrivilege.function_execute}
)


def _vocabulary() -> str:
    tokens = [p.value for p in SchemaPrivilege]
    return ", ".join(tokens + sorted(PRIVILEGE_PRESETS))


def parse_privileges(values: Iterable[str] | None) -> tuple[SchemaPrivilege, ...]:
    """Flatten, expand presets, imply USAGE, dedupe, and order.

    Accepts repeated and comma-separated flags alike; presets expand in place.
    Any table / sequence / function privilege pulls in ``usage`` — the same rule
    ``role_admin._schema_usage_set`` applies — because an object cannot be
    reached without USAGE on its schema, and a grant that silently does nothing
    is worse than one that is refused.
    """
    tokens = parse_csv_flag(values)
    if not tokens:
        raise ValidationError(
            f"at least one privilege is required. Valid: {_vocabulary()}"
        )

    selected: dict[SchemaPrivilege, None] = {}
    for token in tokens:
        lowered = token.lower()
        if lowered in PRIVILEGE_PRESETS:
            for privilege in PRIVILEGE_PRESETS[lowered]:
                selected.setdefault(privilege, None)
            continue
        try:
            selected.setdefault(SchemaPrivilege(lowered), None)
        except ValueError:
            raise ValidationError(
                f'unknown privilege "{token}". Valid: {_vocabulary()}'
            ) from None

    if any(p in _IMPLIES_USAGE for p in selected):
        selected.setdefault(SchemaPrivilege.usage, None)

    return tuple(p for p in PRIVILEGE_ORDER if p in selected)


def parse_grant_spec(value: str) -> tuple[str, tuple[SchemaPrivilege, ...]]:
    """Split ``grantee:priv,priv`` into its two halves.

    A value with no colon is an error rather than a default: silently guessing a
    privilege level for a named principal is exactly the kind of helpfulness that
    hands somebody more access than intended.
    """
    grantee, separator, privileges = value.partition(":")
    if not separator:
        raise ValidationError(
            f'invalid --grant value "{value}". Expected grantee:privileges '
            f"(e.g. analysts:read). Valid: {_vocabulary()}"
        )
    cleaned = grantee.strip()
    if not cleaned:
        raise ValidationError(f'invalid --grant value "{value}": empty grantee.')
    return cleaned, parse_privileges([privileges])


@dataclass(frozen=True)
class GranteeSpec:
    """One principal and the privileges it should gain or lose."""

    name: str
    kind: ParentKind
    privileges: tuple[SchemaPrivilege, ...]


def build_grant_plan(
    schemas: Sequence[str],
    grantees: Sequence[GranteeSpec],
    dialect: SchemaDialect,
    *,
    grantors: Mapping[str, str] | None = None,
    revoke: bool = False,
    cascade: bool = False,
    held: set[tuple[str, str, str]] | None = None,
) -> SchemaPlan:
    """Cross-product plan: every schema × every grantee × every privilege.

    Validates fully before emitting anything, so a typo in the last argument
    fails before the first statement is built.

    ``held`` holds ``(schema, grantee, privilege)`` triples already in effect.
    Only schema-scoped privileges are skipped: across a fan-out like ``ON ALL
    TABLES`` "held" has no single answer, and ``GRANT`` is idempotent, so
    re-issuing costs nothing but noise. ``held`` is ignored entirely when
    revoking — a held grant tells you a revoke is *needed*, not that it is
    redundant.
    """
    if not schemas:
        raise ValidationError("at least one schema is required")
    if not grantees:
        raise ValidationError("at least one grantee is required")

    absent = sorted(g.name for g in grantees if g.kind is ParentKind.absent)
    if absent:
        raise ValidationError(f"grantee(s) not found: {', '.join(absent)}")
    empty = sorted(g.name for g in grantees if not g.privileges)
    if empty:
        raise ValidationError(f"no privileges given for: {', '.join(empty)}")

    grantor_for = grantors or {}
    skip = held or set()
    ops: list[SqlOp] = []
    warnings: list[str] = []
    skipped: list[tuple[str, str, str]] = []

    for schema in schemas:
        _require_name(schema)
        for grantee in grantees:
            for privilege in PRIVILEGE_ORDER:
                if privilege not in grantee.privileges:
                    continue
                triple = (schema, grantee.name, privilege.value)
                if not revoke and privilege in SCHEMA_LEVEL and triple in skip:
                    skipped.append(triple)
                    continue
                op = dialect.privilege_op(
                    privilege,
                    schema,
                    grantee.name,
                    grantee.kind,
                    revoke=revoke,
                    grantor=grantor_for.get(schema),
                    cascade=cascade,
                )
                if op is None:
                    # The dialect returning None can mean either "not supported
                    # for any grantee on this engine" (sequence-usage on
                    # Redshift, which has no sequences at all) or "not supported
                    # for this grantee's kind" (default-* to an RBAC role on
                    # Redshift). This message is phrased to hold for both rather
                    # than asserting a cause the plan builder cannot distinguish.
                    _warn_once(
                        warnings,
                        f"skipping {privilege.value} on {dialect.engine.value}: "
                        "no equivalent grant for this grantee here",
                    )
                    continue
                ops.append(op)

    return SchemaPlan(ops=ops, warnings=warnings, already_held=tuple(skipped))
