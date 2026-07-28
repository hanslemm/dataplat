"""What each SQL engine can be asked, declared once.

dataplat now speaks to three engines, and they are not three sizes of the same
database. PostgreSQL and Redshift are servers with users, sessions and an ACL
system; DuckDB is a database file opened inside this process, where none of
those exist to describe. Several commands therefore *cannot* apply to a DuckDB
target — not yet, but ever — and a few PostgreSQL catalogs are missing on
Redshift too, which this codebase has always worked around inline.

This module is where those facts live, for three reasons:

- **One declaration per engine.** :class:`EngineCapabilities` has no defaults,
  so adding an engine to :class:`~dataplat.services.db.connection.SqlEngine`
  and forgetting what it can do is a construction error here, not a wrong
  answer somewhere downstream.
- **One voice for a refusal.** :func:`require_capability` raises the
  :class:`~dataplat.core.errors.ValidationError` — exit 2, "a combination of
  arguments that cannot work" — so nine commands do not invent nine phrasings
  of the same sentence, and none of them says "not implemented" about something
  that is a property of the engine.
- **Capabilities describe the engine, not the command.** They are named for
  what the database *has* (a role catalog, other sessions, ``aclexplode``), and
  each command asks for what it needs. ``dp db role show`` and ``dp db role
  list`` both need :attr:`Capability.roles`; neither needs an entry here.

Naming rule worth knowing before you add one: a capability is named for the
*catalog or function* when that is what is really missing.
:attr:`Capability.matview_catalog` is not ``materialized_views`` because
Redshift *has* materialized views — what it lacks is a ``pg_matviews`` catalog
listing them, which is the fact ``orphans.py`` actually cares about. Declaring
"Redshift has no materialized views" would be false, and a false capability is
worse than none.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from enum import Enum

from dataplat.core.errors import ValidationError
from dataplat.services.db.connection import SqlEngine

__all__ = [
    "CAPABILITY_FIELDS",
    "Capability",
    "EngineCapabilities",
    "Support",
    "capabilities_for",
    "require_capability",
]


@dataclass(frozen=True)
class Support:
    """Whether an engine has one capability, and — when it does not — why.

    The reason travels with the fact rather than living in the command that
    trips over it. That is what lets the refusal message be specific ("it has
    no users at all") while being written in exactly one place, and it is why
    ``available=False`` without a reason is rejected at import time: a
    capability nobody can explain would produce a refusal nobody can act on.
    """

    available: bool
    # Phrased as the tail of "<command> cannot run against <engine>: ...", in
    # the present tense, with no trailing period.
    reason: str = ""

    def __post_init__(self) -> None:
        if not self.available and not self.reason:
            raise ValueError("an unavailable capability must carry a reason")
        if self.available and self.reason:
            raise ValueError("an available capability needs no reason")

    def __bool__(self) -> bool:
        """So a command can write ``if caps.roles:`` and mean it."""
        return self.available


_HAS = Support(available=True)


def _lacks(reason: str) -> Support:
    return Support(available=False, reason=reason)


class Capability(str, Enum):
    """The engine properties a command can require.

    Values are the field names on :class:`EngineCapabilities`; a test pins the
    two together in both directions, so neither can grow an entry the other is
    missing.
    """

    roles = "roles"
    role_password_store = "role_password_store"
    concurrent_sessions = "concurrent_sessions"
    matview_catalog = "matview_catalog"
    acl_introspection = "acl_introspection"
    relation_size_functions = "relation_size_functions"
    rename_with_dependents = "rename_with_dependents"


@dataclass(frozen=True)
class EngineCapabilities:
    """One engine's answer to every :class:`Capability`.

    No field has a default, deliberately: a new engine must state all of them.
    """

    engine: SqlEngine
    # How the engine is named in a message the user reads.
    label: str

    # A catalog of database users/roles to inspect at all (pg_roles, pg_user).
    roles: Support
    # The real password store behind that catalog (pg_authid). Separate from
    # `roles` because the shared views mask the verifier to '********', so
    # "does this role have a password" is only answerable where this is true.
    role_password_store: Support
    # Other sessions exist, and a catalog lists them (pg_stat_activity,
    # stv_recents) — the premise of listing or cancelling running queries.
    concurrent_sessions: Support
    # A catalog of materialized views (pg_matviews). Named for the catalog: see
    # the module docstring.
    matview_catalog: Support
    # aclexplode()/pg_get_userbyid(), which expand an ACL array into grants.
    acl_introspection: Support
    # pg_total_relation_size()/pg_database_size(): sizes from the catalog
    # rather than from an engine-specific system view.
    relation_size_functions: Support
    # ALTER ... RENAME TO succeeds when a view depends on the relation, which is
    # the premise of quarantining an orphan by renaming it out of the way.
    rename_with_dependents: Support

    def support(self, capability: Capability) -> Support:
        """This engine's :class:`Support` for ``capability``."""
        # getattr over the enum value, not a dict lookup: the fields are real
        # dataclass fields (so mypy checks `caps.roles` at every call site) and
        # the enum is what keeps this getattr from being a stringly-typed hole.
        support = getattr(self, capability.value)
        assert isinstance(support, Support)  # narrow Any from getattr
        return support

    def require(
        self, capability: Capability, *, command: str, detail: str | None = None
    ) -> None:
        """Raise unless this engine supports ``capability``.

        ``command`` is how the user spelled it (``"dp db role show"``).
        ``detail`` is for the case where the engine fact alone does not explain
        why *this* command needs it — ``dp db dbt-orphans`` quarantines by
        renaming, which is not obvious from "renames fail when a view depends
        on the table". It is appended as a second sentence; the frame, the
        engine name and the exit code stay owned by this method.
        """
        support = self.support(capability)
        if support:
            return
        message = f"{command} cannot run against {self.label}: {support.reason}."
        if detail:
            message += f" {detail}"
        # Not "not implemented" and not "unsupported": this is what the engine
        # is, and a user who reads it should stop looking for a flag that turns
        # it on.
        message += f" That is what {self.label} is, not a missing dataplat feature."
        raise ValidationError(message)


_POSTGRESQL = EngineCapabilities(
    engine=SqlEngine.postgresql,
    label="PostgreSQL",
    roles=_HAS,
    role_password_store=_HAS,
    concurrent_sessions=_HAS,
    matview_catalog=_HAS,
    acl_introspection=_HAS,
    relation_size_functions=_HAS,
    rename_with_dependents=_HAS,
)

# Redshift is not "PostgreSQL with everything true". Each False below is a
# workaround this codebase already ships, cited so the declaration can be
# checked against the code rather than against a memory of the docs. Nothing
# here changes behaviour: no existing command consults this module, and every
# True is what the shipped Redshift path already assumes.
_REDSHIFT = EngineCapabilities(
    engine=SqlEngine.redshift,
    label="Redshift",
    # pg_user/pg_group exist and role.py reads them (_ATTRS_SQL_REDSHIFT).
    roles=_HAS,
    # No pg_authid. pg_user.passwd is masked to '********' for every row, so
    # password_set is pinned to unknown there — services/db/role.py.
    role_password_store=_lacks(
        "it has no pg_authid, and pg_user.passwd is masked to '********' for "
        "every row, so whether a password exists cannot be read"
    ),
    # stv_recents/stv_inflight; services/db/long_queries.py fetch_long_queries.
    concurrent_sessions=_HAS,
    # Redshift *has* materialized views but no pg_matviews over them; see
    # services/db/orphans.py, which skips that query when is_redshift.
    matview_catalog=_lacks(
        "it has no pg_matviews catalog listing materialized views (STV_MV_INFO "
        "is the cluster-level equivalent, and dataplat does not read it)"
    ),
    # services/db/describe.py keeps a separate schema-privilege query for
    # Redshift precisely because aclexplode() does not exist there.
    acl_introspection=_lacks(
        "it has no aclexplode(), so an ACL array cannot be expanded into one "
        "row per grant"
    ),
    # svv_table_info.size instead; services/db/top_tables.py.
    relation_size_functions=_lacks(
        "it has no pg_total_relation_size()/pg_database_size(); size comes "
        "from svv_table_info instead"
    ),
    # Evidence class 2 (CONTRIBUTING): orphans.py has always sent ALTER TABLE
    # ... RENAME TO to Redshift (build_rename_statement, is_redshift=True), so
    # declaring this present is what the tool already does. It is not a probed
    # fact — there is no cluster in CI — and if a conformance run refutes it,
    # this is the line to change.
    rename_with_dependents=_HAS,
)

# Everything False here was probed against duckdb 1.5.5 rather than read in a
# doc, and every one of them follows from the same two properties: DuckDB runs
# *inside* this process, and it is single-user.
_DUCKDB = EngineCapabilities(
    engine=SqlEngine.duckdb,
    label="DuckDB",
    roles=_lacks(
        "it has no users or roles at all — pg_roles, pg_authid and pg_user do "
        "not exist, and every connection is the same implicit user, 'duckdb'"
    ),
    role_password_store=_lacks(
        "it has no users, so there is no password store to read"
    ),
    concurrent_sessions=_lacks(
        "it runs inside this process and has no pg_stat_activity: there are no "
        "other sessions to inspect or cancel"
    ),
    matview_catalog=_lacks("it has no materialized views, and no pg_matviews catalog"),
    acl_introspection=_lacks(
        "it has no aclexplode(), no pg_get_userbyid() and no grantees to "
        "resolve — privileges belong to a server with users"
    ),
    relation_size_functions=_lacks(
        "it has no pg_total_relation_size()/pg_database_size(); size comes "
        "from duckdb_tables().estimated_size and pragma_database_size() instead"
    ),
    rename_with_dependents=_lacks(
        "ALTER TABLE ... RENAME TO fails with a DependencyException whenever a "
        "view depends on the table, and it has no CASCADE"
    ),
)

_CAPABILITIES: dict[SqlEngine, EngineCapabilities] = {
    caps.engine: caps for caps in (_POSTGRESQL, _REDSHIFT, _DUCKDB)
}


def capabilities_for(engine: SqlEngine) -> EngineCapabilities:
    """The declaration for ``engine``."""
    caps = _CAPABILITIES.get(engine)
    if caps is None:
        # A new SqlEngine member with no declaration. Not a DataplatError: the
        # user cannot fix it, and a test asserts the mapping is total.
        raise AssertionError(
            f"engine {engine.value!r} has no capability declaration in "
            "dataplat/services/db/capabilities.py"
        )
    return caps


def require_capability(
    engine: SqlEngine,
    capability: Capability,
    *,
    command: str,
    detail: str | None = None,
) -> None:
    """Raise a ValidationError unless ``engine`` supports ``capability``.

    The door most commands use, since they hold an engine rather than a
    declaration::

        require_capability(
            params.engine, Capability.roles, command="dp db role show"
        )

    Call it *before* connecting: the refusal is about the engine, so opening a
    database first only makes the failure slower.
    """
    capabilities_for(engine).require(capability, command=command, detail=detail)


# Two rules this module cannot enforce on itself are enforced by
# tests/services/db/test_capabilities.py: every SqlEngine member has a
# declaration, and Capability names exactly the capability fields — in both
# directions, so neither can grow an entry the other is missing.
CAPABILITY_FIELDS: frozenset[str] = frozenset(
    field.name
    for field in fields(EngineCapabilities)
    if field.name not in {"engine", "label"}
)
