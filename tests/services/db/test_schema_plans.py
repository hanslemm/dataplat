"""The ``dp db schema`` plan builders: what they emit, and what they refuse.

The SQL these produce is executed for real against PostgreSQL 16 in
``tests/integration/test_schema_pg.py``. What lives here is the branching a
server cannot be asked about — engine-specific skips, the validation order, and
the refusals that exist so an impossible request fails before the first statement
rather than partway through.
"""

from __future__ import annotations

import pytest

from dataplat.core.errors import ExitCode, ValidationError
from dataplat.services.db.role_dialects import ParentKind
from dataplat.services.db.schema_admin import (
    CreateSchemaSpec,
    GranteeSpec,
    SchemaPrivilege,
    SchemaSummary,
    build_alter_plan,
    build_create_plan,
    build_drop_plan,
    build_grant_plan,
    parse_grant_spec,
    parse_privileges,
    parse_quota,
)
from dataplat.services.db.schema_dialects import (
    DuckDbSchemaDialect,
    PostgresSchemaDialect,
    RedshiftSchemaDialect,
)

PG = PostgresSchemaDialect()
RS = RedshiftSchemaDialect()
DDB = DuckDbSchemaDialect()


def _sql(plan) -> list[str]:
    return [op.statement.as_string(None) for op in plan.ops]


# ---------------------------------------------------------------------------
# parse_quota — the sanitizer behind an interpolated DDL clause
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("50GB", "50 GB"),
        ("50 gb", "50 GB"),
        ("  1024MB  ", "1024 MB"),
        ("2tb", "2 TB"),
        ("UNLIMITED", "UNLIMITED"),
        ("unlimited", "UNLIMITED"),
    ],
)
def test_quota_normalizes(raw: str, expected: str) -> None:
    assert parse_quota(raw) == expected


def test_quota_is_idempotent() -> None:
    """Which is what lets CreateSchemaSpec re-normalize defensively."""
    assert parse_quota(parse_quota("50gb")) == "50 GB"


@pytest.mark.parametrize(
    "raw",
    [
        "50PB",  # unknown unit
        "GB",  # no amount
        "50",  # no unit
        "0GB",  # zero
        "-5GB",  # negative
        "50GB; DROP",  # the reason this is a regex and not a passthrough
        "50 GB UNLIMITED",
        "",
    ],
)
def test_an_invalid_quota_is_refused(raw: str) -> None:
    with pytest.raises(ValidationError):
        parse_quota(raw)


def test_a_quota_reaching_ddl_contains_nothing_but_digits_and_a_unit() -> None:
    """The whole guarantee: the output is rebuilt from parsed groups.

    A quota is neither an identifier nor a bindable parameter, so the dialect
    interpolates it into statement text. Nothing of the caller's input survives
    verbatim, which is what makes that safe.
    """
    rendered = RS.alter_quota("s", parse_quota("50gb"))
    assert rendered is not None
    assert rendered.statement.as_string(None) == 'ALTER SCHEMA "s" QUOTA 50 GB'


def test_the_spec_normalizes_its_own_quota() -> None:
    assert CreateSchemaSpec("s", quota="50gb").quota == "50 GB"
    with pytest.raises(ValidationError):
        CreateSchemaSpec("s", quota="nonsense")


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


def test_create_emits_one_statement_per_spec_in_order() -> None:
    plan = build_create_plan(
        [CreateSchemaSpec("a"), CreateSchemaSpec("b", if_not_exists=True)], PG
    )

    assert _sql(plan) == [
        'CREATE SCHEMA "a"',
        'CREATE SCHEMA IF NOT EXISTS "b"',
    ]


def test_create_with_an_owner_uses_authorization() -> None:
    plan = build_create_plan([CreateSchemaSpec("a", owner="svc")], PG)

    assert _sql(plan) == ['CREATE SCHEMA "a" AUTHORIZATION "svc"']


def test_redshift_appends_the_quota_clause() -> None:
    plan = build_create_plan([CreateSchemaSpec("a", quota="50GB")], RS)

    assert _sql(plan) == ['CREATE SCHEMA "a" QUOTA 50 GB']
    assert plan.warnings == []


def test_a_quota_off_redshift_warns_once_and_is_dropped() -> None:
    plan = build_create_plan(
        [CreateSchemaSpec("a", quota="50GB"), CreateSchemaSpec("b", quota="10GB")], PG
    )

    assert _sql(plan) == ['CREATE SCHEMA "a"', 'CREATE SCHEMA "b"']
    # Once, not once per schema.
    assert plan.warnings == ["ignoring --quota: only Redshift supports schema quotas"]


def test_an_owner_on_duckdb_is_dropped_rather_than_emitted() -> None:
    """DuckDB's parser rejects AUTHORIZATION, so no caller may build it.

    The CLI refuses --owner outright with the engine's reason; this is the
    service-layer half, so a library caller cannot generate invalid SQL either.
    """
    plan = build_create_plan([CreateSchemaSpec("a", owner="svc")], DDB)

    assert _sql(plan) == ['CREATE SCHEMA "a"']
    assert any("owner" in w for w in plan.warnings)


def test_create_needs_a_name() -> None:
    with pytest.raises(ValidationError):
        build_create_plan([], PG)
    with pytest.raises(ValidationError):
        build_create_plan([CreateSchemaSpec("   ")], PG)


# ---------------------------------------------------------------------------
# drop
# ---------------------------------------------------------------------------


def test_restrict_is_explicit_not_implied() -> None:
    """The plan must state which of the two behaviours will run."""
    assert _sql(build_drop_plan(["a"])) == ['DROP SCHEMA "a" RESTRICT']


def test_cascade_and_if_exists_compose() -> None:
    plan = build_drop_plan(["a"], cascade=True, if_exists=True)

    assert _sql(plan) == ['DROP SCHEMA IF EXISTS "a" CASCADE']


def test_drop_descriptions_read_as_the_statement() -> None:
    (op,) = build_drop_plan(["a"], cascade=True).ops

    assert op.description == "DROP SCHEMA a CASCADE"


def test_drop_needs_a_name() -> None:
    with pytest.raises(ValidationError):
        build_drop_plan([])
    with pytest.raises(ValidationError):
        build_drop_plan([" "])


# ---------------------------------------------------------------------------
# alter
# ---------------------------------------------------------------------------


def test_alter_emits_owner_quota_and_rename() -> None:
    plan = build_alter_plan(["a"], RS, owner="svc", quota="1TB", rename_to="b")

    assert _sql(plan) == [
        'ALTER SCHEMA "a" OWNER TO "svc"',
        'ALTER SCHEMA "a" QUOTA 1 TB',
        'ALTER SCHEMA "a" RENAME TO "b"',
    ]


def test_alter_with_no_change_requested_is_refused() -> None:
    """Otherwise the command reports success having done nothing."""
    with pytest.raises(ValidationError, match="nothing to do"):
        build_alter_plan(["a"], PG)


def test_rename_takes_exactly_one_schema() -> None:
    """Two schemas cannot both become one name."""
    with pytest.raises(ValidationError, match="exactly one"):
        build_alter_plan(["a", "b"], PG, rename_to="c")


def test_quota_alone_off_redshift_is_an_error_not_a_warning() -> None:
    """A warn-and-skip here would leave the command doing nothing at all."""
    with pytest.raises(ValidationError, match="only Redshift"):
        build_alter_plan(["a"], PG, quota="50GB")


def test_quota_alongside_other_work_off_redshift_warns_and_skips() -> None:
    plan = build_alter_plan(["a"], PG, owner="svc", quota="50GB")

    assert _sql(plan) == ['ALTER SCHEMA "a" OWNER TO "svc"']
    assert any("quota" in w for w in plan.warnings)


def test_alter_revalidates_the_quota_it_is_given() -> None:
    with pytest.raises(ValidationError):
        build_alter_plan(["a"], RS, quota="50PB")


def test_duckdb_refuses_to_build_an_alter_at_all() -> None:
    """ALTER SCHEMA is unimplemented there; the CLI refuses before this."""
    with pytest.raises(ValidationError, match="ALTER SCHEMA"):
        build_alter_plan(["a"], DDB, rename_to="b")
    with pytest.raises(ValidationError, match="owner"):
        build_alter_plan(["a"], DDB, owner="svc")


# ---------------------------------------------------------------------------
# privilege parsing
# ---------------------------------------------------------------------------


def test_privileges_are_ordered_not_as_typed() -> None:
    """USAGE must be granted before anything that depends on it."""
    privs = parse_privileges(["select,usage"])

    assert privs == (SchemaPrivilege.usage, SchemaPrivilege.select)


def test_a_table_privilege_implies_usage() -> None:
    """You cannot reach a table without USAGE on its schema."""
    assert SchemaPrivilege.usage in parse_privileges(["select"])
    assert SchemaPrivilege.usage in parse_privileges(["sequence-usage"])
    assert SchemaPrivilege.usage in parse_privileges(["default-all"])


@pytest.mark.parametrize(
    "privilege", [SchemaPrivilege.usage, SchemaPrivilege.create, SchemaPrivilege.all]
)
def test_a_schema_level_privilege_pulls_in_nothing(
    privilege: SchemaPrivilege,
) -> None:
    """USAGE is implied by privileges on objects *inside* a schema, not by
    privileges on the schema itself — CREATE does not need USAGE to be granted
    alongside it, and adding one would hand out access nobody asked for."""
    assert parse_privileges([privilege.value]) == (privilege,)


def test_presets_expand() -> None:
    assert parse_privileges(["read"]) == (
        SchemaPrivilege.usage,
        SchemaPrivilege.select,
        SchemaPrivilege.default_select,
    )
    assert parse_privileges(["readwrite"]) == (
        SchemaPrivilege.usage,
        SchemaPrivilege.create,
        SchemaPrivilege.table_all,
        SchemaPrivilege.default_all,
    )


def test_repeated_and_comma_separated_flags_are_equivalent() -> None:
    assert parse_privileges(["usage,create"]) == parse_privileges(["usage", "create"])


def test_duplicates_collapse() -> None:
    """`read` already implies usage, so naming it too must not double the GRANT."""
    privs = parse_privileges(["usage", "usage", "read"])

    assert privs.count(SchemaPrivilege.usage) == 1


def test_an_unknown_privilege_lists_the_vocabulary() -> None:
    with pytest.raises(ValidationError) as exc:
        parse_privileges(["nonsense"])

    message = str(exc.value)
    assert "nonsense" in message
    assert "table-all" in message  # the vocabulary, so the user can self-correct
    assert "readwrite" in message  # presets included
    assert exc.value.exit_code == ExitCode.INVALID_INPUT


def test_no_privileges_is_refused() -> None:
    with pytest.raises(ValidationError):
        parse_privileges([])
    with pytest.raises(ValidationError):
        parse_privileges(None)


def test_grant_spec_splits_on_the_colon() -> None:
    assert parse_grant_spec("readers:read") == (
        "readers",
        (
            SchemaPrivilege.usage,
            SchemaPrivilege.select,
            SchemaPrivilege.default_select,
        ),
    )


def test_a_grant_spec_without_a_colon_is_refused_not_defaulted() -> None:
    """Guessing a privilege level for a named principal is how people get more
    access than intended."""
    with pytest.raises(ValidationError, match="grantee:privileges"):
        parse_grant_spec("readers")


def test_a_grant_spec_with_an_empty_grantee_is_refused() -> None:
    with pytest.raises(ValidationError, match="empty grantee"):
        parse_grant_spec(":read")


# ---------------------------------------------------------------------------
# grant / revoke
# ---------------------------------------------------------------------------


def _grantee(name: str = "readers", *privs: SchemaPrivilege) -> GranteeSpec:
    return GranteeSpec(
        name=name, kind=ParentKind.role, privileges=privs or (SchemaPrivilege.usage,)
    )


def test_grant_is_the_cross_product_of_schemas_and_grantees() -> None:
    plan = build_grant_plan(["a", "b"], [_grantee("x"), _grantee("y")], PG)

    assert _sql(plan) == [
        'GRANT USAGE ON SCHEMA "a" TO "x"',
        'GRANT USAGE ON SCHEMA "a" TO "y"',
        'GRANT USAGE ON SCHEMA "b" TO "x"',
        'GRANT USAGE ON SCHEMA "b" TO "y"',
    ]


def test_every_privilege_renders_its_own_target() -> None:
    plan = build_grant_plan(
        ["a"],
        [
            _grantee(
                "x",
                SchemaPrivilege.usage,
                SchemaPrivilege.create,
                SchemaPrivilege.select,
                SchemaPrivilege.table_all,
                SchemaPrivilege.sequence_usage,
                SchemaPrivilege.function_execute,
            )
        ],
        PG,
    )

    assert _sql(plan) == [
        'GRANT USAGE ON SCHEMA "a" TO "x"',
        'GRANT CREATE ON SCHEMA "a" TO "x"',
        'GRANT SELECT ON ALL TABLES IN SCHEMA "a" TO "x"',
        'GRANT ALL ON ALL TABLES IN SCHEMA "a" TO "x"',
        'GRANT USAGE ON ALL SEQUENCES IN SCHEMA "a" TO "x"',
        'GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA "a" TO "x"',
    ]


def test_revoke_flips_the_verb_and_the_preposition() -> None:
    plan = build_grant_plan(["a"], [_grantee("x")], PG, revoke=True)

    assert _sql(plan) == ['REVOKE USAGE ON SCHEMA "a" FROM "x"']


def test_cascade_applies_only_to_revoke() -> None:
    """There is no GRANT ... CASCADE; appending one would be a syntax error."""
    granted = build_grant_plan(["a"], [_grantee("x")], PG, cascade=True)
    revoked = build_grant_plan(["a"], [_grantee("x")], PG, revoke=True, cascade=True)

    assert "CASCADE" not in _sql(granted)[0]
    assert _sql(revoked)[0].endswith("CASCADE")


def test_default_privileges_need_a_grantor() -> None:
    """Without FOR ROLE the statement binds to whoever is connected and the
    grant silently does nothing — the most common way this feature fails."""
    with pytest.raises(ValidationError, match="--default-for"):
        build_grant_plan(["a"], [_grantee("x", SchemaPrivilege.default_select)], PG)


def test_default_privileges_name_the_grantor_they_were_given() -> None:
    plan = build_grant_plan(
        ["a"],
        [_grantee("x", SchemaPrivilege.default_all)],
        PG,
        grantors={"a": "svc"},
    )

    assert _sql(plan) == [
        'ALTER DEFAULT PRIVILEGES FOR ROLE "svc" IN SCHEMA "a" '
        'GRANT ALL ON TABLES TO "x"'
    ]


def test_redshift_says_for_user_not_for_role() -> None:
    plan = build_grant_plan(
        ["a"],
        [GranteeSpec("u", ParentKind.user, (SchemaPrivilege.default_select,))],
        RS,
        grantors={"a": "svc"},
    )

    assert "FOR USER" in _sql(plan)[0]


def test_redshift_renders_the_grantee_class() -> None:
    """TO GROUP g / TO ROLE r — Redshift grants to the wrong thing without it."""
    group = build_grant_plan(
        ["a"], [GranteeSpec("g", ParentKind.group, (SchemaPrivilege.usage,))], RS
    )
    role = build_grant_plan(
        ["a"], [GranteeSpec("r", ParentKind.role, (SchemaPrivilege.usage,))], RS
    )
    user = build_grant_plan(
        ["a"], [GranteeSpec("u", ParentKind.user, (SchemaPrivilege.usage,))], RS
    )

    assert _sql(group)[0].endswith('TO GROUP "g"')
    assert _sql(role)[0].endswith('TO ROLE "r"')
    assert _sql(user)[0].endswith('TO "u"')


def test_postgres_names_every_principal_the_same_way() -> None:
    """One namespace, so a class keyword would be a syntax error."""
    for kind in (ParentKind.user, ParentKind.role, ParentKind.group):
        plan = build_grant_plan(
            ["a"], [GranteeSpec("p", kind, (SchemaPrivilege.usage,))], PG
        )
        assert _sql(plan)[0].endswith('TO "p"')


def test_public_is_a_keyword_and_never_quoted() -> None:
    """Quoting it would search for a role actually named `public`."""
    for dialect in (PG, RS):
        plan = build_grant_plan(
            ["a"],
            [GranteeSpec("PUBLIC", ParentKind.role, (SchemaPrivilege.usage,))],
            dialect,
        )
        assert _sql(plan)[0].endswith("TO PUBLIC")


def test_redshift_skips_sequence_usage_with_a_warning() -> None:
    """It has no sequences at all."""
    plan = build_grant_plan(["a"], [_grantee("x", SchemaPrivilege.sequence_usage)], RS)

    assert _sql(plan) == []
    assert any("sequence-usage" in w for w in plan.warnings)


def test_redshift_skips_default_privileges_for_an_rbac_role() -> None:
    """ALTER DEFAULT PRIVILEGES has no TO ROLE form there."""
    plan = build_grant_plan(
        ["a"],
        [GranteeSpec("r", ParentKind.role, (SchemaPrivilege.default_select,))],
        RS,
        grantors={"a": "svc"},
    )

    assert _sql(plan) == []
    assert plan.warnings


def test_redshift_still_grants_default_privileges_to_public() -> None:
    """PUBLIC carries ParentKind.role as a placeholder, and must not be skipped.

    resolve_grantee_kinds assigns it that kind because render_grantee ignores the
    kind for PUBLIC — so the placeholder must not leak into the role skip above.
    Redshift's ALTER DEFAULT PRIVILEGES does support TO PUBLIC.
    """
    plan = build_grant_plan(
        ["a"],
        [GranteeSpec("PUBLIC", ParentKind.role, (SchemaPrivilege.default_select,))],
        RS,
        grantors={"a": "svc"},
    )

    assert len(plan.ops) == 1
    assert _sql(plan)[0].endswith("TO PUBLIC")


def test_a_held_schema_privilege_is_reported_not_reissued() -> None:
    plan = build_grant_plan(
        ["a"],
        [_grantee("x", SchemaPrivilege.usage, SchemaPrivilege.create)],
        PG,
        held={("a", "x", "usage")},
    )

    assert _sql(plan) == ['GRANT CREATE ON SCHEMA "a" TO "x"']
    assert plan.already_held == (("a", "x", "usage"),)


def test_held_does_not_skip_a_fan_out_privilege() -> None:
    """Across ON ALL TABLES "held" has no single answer, and GRANT is idempotent."""
    plan = build_grant_plan(
        ["a"],
        [_grantee("x", SchemaPrivilege.select)],
        PG,
        held={("a", "x", "select"), ("a", "x", "usage")},
    )

    assert _sql(plan) == ['GRANT SELECT ON ALL TABLES IN SCHEMA "a" TO "x"']


def test_held_is_ignored_when_revoking() -> None:
    """A held grant means the revoke is needed, not that it is redundant."""
    plan = build_grant_plan(
        ["a"], [_grantee("x")], PG, revoke=True, held={("a", "x", "usage")}
    )

    assert _sql(plan) == ['REVOKE USAGE ON SCHEMA "a" FROM "x"']
    assert plan.already_held == ()


def test_an_absent_grantee_is_refused_before_anything_is_emitted() -> None:
    with pytest.raises(ValidationError, match="not found"):
        build_grant_plan(
            ["a"],
            [
                _grantee("ok"),
                GranteeSpec("ghost", ParentKind.absent, (SchemaPrivilege.usage,)),
            ],
            PG,
        )


def test_a_grantee_with_no_privileges_is_refused() -> None:
    with pytest.raises(ValidationError, match="no privileges"):
        build_grant_plan(["a"], [GranteeSpec("x", ParentKind.role, ())], PG)


def test_grant_needs_schemas_and_grantees() -> None:
    with pytest.raises(ValidationError, match="schema"):
        build_grant_plan([], [_grantee()], PG)
    with pytest.raises(ValidationError, match="grantee"):
        build_grant_plan(["a"], [], PG)


def test_duckdb_refuses_to_build_a_grant() -> None:
    with pytest.raises(ValidationError, match="GRANT"):
        build_grant_plan(["a"], [_grantee("x")], DDB)


# ---------------------------------------------------------------------------
# SchemaSummary
# ---------------------------------------------------------------------------


def test_object_count_totals_everything_a_cascade_destroys() -> None:
    row = SchemaSummary("a", "o", tables=2, views=3, other=4)

    assert row.object_count == 9


def test_object_count_is_zero_for_an_empty_schema() -> None:
    assert SchemaSummary("a", "o", 0, 0).object_count == 0
