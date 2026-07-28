"""The capability matrix, and the two rules that keep it honest.

The matrix is a promise to nine commands: ask it, and you get a refusal that
names the engine and says why. These tests cover the promise (the message and
its exit code), the completeness of the declarations (a new engine cannot ship
without one), and the specific Redshift entries — which are workarounds this
codebase already ships, not guesses.
"""

from __future__ import annotations

from dataclasses import fields

import pytest

from dataplat.core.errors import ExitCode, ValidationError
from dataplat.services.db import capabilities as caps_module
from dataplat.services.db.capabilities import (
    CAPABILITY_FIELDS,
    Capability,
    EngineCapabilities,
    Support,
    capabilities_for,
    require_capability,
)
from dataplat.services.db.connection import SqlEngine

# =========================================================================
# Completeness. A new engine or a new capability must not be able to arrive
# half-declared: that is the whole reason this is a dataclass and an enum
# rather than a dict of strings.
# =========================================================================


def test_every_engine_has_a_declaration() -> None:
    for engine in SqlEngine:
        assert capabilities_for(engine).engine is engine


def test_capability_enum_and_fields_agree_in_both_directions() -> None:
    assert {c.value for c in Capability} == CAPABILITY_FIELDS


def test_every_capability_field_is_a_support_on_every_engine() -> None:
    declared = {f.name for f in fields(EngineCapabilities)}
    assert declared == CAPABILITY_FIELDS | {"engine", "label"}
    for engine in SqlEngine:
        engine_caps = capabilities_for(engine)
        for capability in Capability:
            assert isinstance(engine_caps.support(capability), Support)


def test_an_engine_without_a_declaration_is_a_programming_error(monkeypatch) -> None:
    """The defensive branch in capabilities_for, made reachable.

    A new SqlEngine member with no entry cannot be fixed by the user, so it
    raises AssertionError rather than a DataplatError with an exit code.
    """
    monkeypatch.setattr(caps_module, "_CAPABILITIES", {})

    with pytest.raises(AssertionError, match="no capability declaration"):
        capabilities_for(SqlEngine.duckdb)


def test_support_rejects_an_unexplained_absence() -> None:
    """An unavailable capability with no reason would refuse without saying why."""
    with pytest.raises(ValueError, match="must carry a reason"):
        Support(available=False)


def test_support_rejects_a_reason_for_something_present() -> None:
    with pytest.raises(ValueError, match="needs no reason"):
        Support(available=True, reason="unused")


def test_support_is_truthy_exactly_when_available() -> None:
    """Commands branch on `if caps.roles:`, so __bool__ is part of the contract."""
    assert bool(Support(available=True))
    assert not bool(Support(available=False, reason="because"))


# =========================================================================
# The refusal. Exit 2, the engine named, the reason given — and never
# "not implemented", because none of these is a gap.
# =========================================================================


def test_refusal_names_the_engine_and_the_reason() -> None:
    with pytest.raises(ValidationError) as excinfo:
        require_capability(
            SqlEngine.duckdb, Capability.roles, command="dp db role show"
        )

    message = str(excinfo.value)
    assert "dp db role show" in message
    assert "DuckDB" in message
    assert "no users or roles" in message


def test_refusal_exits_invalid_input() -> None:
    """A ValidationError is exit 2: a combination of arguments that cannot work."""
    with pytest.raises(ValidationError) as excinfo:
        require_capability(
            SqlEngine.duckdb,
            Capability.concurrent_sessions,
            command="dp db long-queries",
        )

    assert excinfo.value.exit_code == ExitCode.INVALID_INPUT


@pytest.mark.parametrize(
    "capability",
    [
        Capability.roles,
        Capability.role_password_store,
        Capability.concurrent_sessions,
        Capability.matview_catalog,
        Capability.acl_introspection,
        Capability.relation_size_functions,
        Capability.rename_with_dependents,
    ],
)
def test_no_refusal_ever_says_not_implemented(capability: Capability) -> None:
    """These are properties of the engine. A user must not go looking for a flag."""
    for engine in SqlEngine:
        try:
            require_capability(engine, capability, command="dp db thing")
        except ValidationError as exc:
            lowered = str(exc).lower()
            assert "not implemented" not in lowered
            assert "unsupported" not in lowered
            assert "not supported" not in lowered
            assert "not a missing dataplat feature" in lowered


def test_detail_is_appended_for_a_command_specific_reason() -> None:
    """dbt-orphans needs to say *why* it needs renames, without owning the frame."""
    with pytest.raises(ValidationError) as excinfo:
        require_capability(
            SqlEngine.duckdb,
            Capability.rename_with_dependents,
            command="dp db dbt-orphans",
            detail="Orphans are quarantined by renaming them out of the way.",
        )

    message = str(excinfo.value)
    assert "DependencyException" in message
    assert "quarantined by renaming" in message


def test_a_supported_capability_raises_nothing() -> None:
    require_capability(
        SqlEngine.postgresql, Capability.roles, command="dp db role show"
    )
    capabilities_for(SqlEngine.postgresql).require(
        Capability.acl_introspection, command="dp db describe"
    )


# =========================================================================
# The matrix itself, engine by engine. Written out rather than derived, so a
# change to a declaration has to be a change to a test as well.
# =========================================================================


def test_postgresql_has_everything() -> None:
    postgres = capabilities_for(SqlEngine.postgresql)
    for capability in Capability:
        assert postgres.support(capability), capability.value


def test_duckdb_refuses_the_four_commands_in_the_matrix() -> None:
    """role*, long-queries, kill and dbt-orphans, by the capability each needs."""
    duckdb = capabilities_for(SqlEngine.duckdb)

    assert not duckdb.roles  # dp db role show/list/create/drop
    assert not duckdb.concurrent_sessions  # dp db long-queries, dp db kill
    assert not duckdb.rename_with_dependents  # dp db dbt-orphans


def test_duckdb_lacks_the_catalogs_the_probe_found_absent() -> None:
    duckdb = capabilities_for(SqlEngine.duckdb)

    assert not duckdb.role_password_store
    assert not duckdb.matview_catalog
    assert not duckdb.acl_introspection
    assert not duckdb.relation_size_functions


def test_redshift_reflects_the_workarounds_this_codebase_already_ships() -> None:
    """Not "everything true": each False is cited in the declaration.

    - no pg_authid → services/db/role.py pins password_set to unknown
    - no pg_matviews → services/db/orphans.py skips that query
    - no aclexplode → services/db/describe.py keeps a separate Redshift query
    - no pg_total_relation_size → services/db/top_tables.py reads svv_table_info
    """
    redshift = capabilities_for(SqlEngine.redshift)

    assert not redshift.role_password_store
    assert not redshift.matview_catalog
    assert not redshift.acl_introspection
    assert not redshift.relation_size_functions


def test_redshift_keeps_the_capabilities_its_shipped_commands_use() -> None:
    """Turning any of these off would start refusing a command that works today.

    ``roles`` backs `dp db role *` (pg_user/pg_group), ``concurrent_sessions``
    backs `dp db long-queries` and `dp db kill` (stv_recents), and
    ``rename_with_dependents`` backs `dp db dbt-orphans`, which has always sent
    ALTER TABLE ... RENAME TO to Redshift (orphans.build_rename_statement).
    """
    redshift = capabilities_for(SqlEngine.redshift)

    assert redshift.roles
    assert redshift.concurrent_sessions
    assert redshift.rename_with_dependents


def test_labels_are_how_a_user_spells_the_engine() -> None:
    assert capabilities_for(SqlEngine.postgresql).label == "PostgreSQL"
    assert capabilities_for(SqlEngine.redshift).label == "Redshift"
    assert capabilities_for(SqlEngine.duckdb).label == "DuckDB"
