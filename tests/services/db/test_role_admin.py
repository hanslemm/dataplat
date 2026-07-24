from __future__ import annotations

import pytest

from dataplat.services.db.role_admin import (
    CreateRoleSpec,
    MissingReassignOwnerError,
    build_create_plan,
    build_drop_plan,
    generate_password,
    list_roles,
    parse_csv_flag,
    resolve_reassign_owner,
)
from dataplat.services.db.role_dialects import (
    OwnedForDrop,
    ParentKind,
    RedshiftDialect,
)


def _render(op_statement, conn=None) -> str:
    """Render a Composed statement using a stub context.

    psycopg.sql.Composed.as_string accepts None for trivial identifier
    escaping when the input is well-formed ASCII, which is fine for tests.
    """
    return op_statement.as_string(conn)


def test_generate_password_length_and_charset() -> None:
    pw = generate_password(32)
    assert len(pw) == 32
    # token_urlsafe uses [A-Za-z0-9_-]
    for ch in pw:
        assert ch.isalnum() or ch in "-_"


def test_generate_password_min_length() -> None:
    with pytest.raises(ValueError):
        generate_password(8)


def test_parse_csv_flag_dedupes_and_trims() -> None:
    out = parse_csv_flag(["raw, staging", "raw,marts ", "  ", ""])
    assert out == ("raw", "staging", "marts")


def test_parse_csv_flag_none() -> None:
    assert parse_csv_flag(None) == ()
    assert parse_csv_flag([]) == ()


def test_build_create_plan_implies_schema_usage_for_table_grants() -> None:
    spec = CreateRoleSpec(
        name="alice",
        password="secret-pw-not-logged",
        table_select=("raw", "staging"),
    )
    plan = build_create_plan(spec, ["demo_pg"])
    db_ops = plan.per_database_ops["demo_pg"]
    rendered = [_render(op.statement) for op in db_ops]
    # USAGE on every schema we'll touch comes first.
    assert any('GRANT USAGE ON SCHEMA "raw" TO "alice"' in r for r in rendered)
    assert any('GRANT USAGE ON SCHEMA "staging" TO "alice"' in r for r in rendered)
    # SELECT on tables follows.
    assert any(
        'GRANT SELECT ON ALL TABLES IN SCHEMA "raw" TO "alice"' in r for r in rendered
    )


def test_build_create_plan_cluster_ops_include_create_role_and_member_of() -> None:
    spec = CreateRoleSpec(
        name="alice",
        password="hunter2_______________________",
        member_of=("analyst", "reader"),
    )
    plan = build_create_plan(spec, ["demo_pg"])
    rendered = [_render(op.statement) for op in plan.cluster_ops]
    assert rendered[0].startswith('CREATE ROLE "alice" LOGIN PASSWORD ')
    assert any('GRANT "analyst" TO "alice"' in r for r in rendered)
    assert any('GRANT "reader" TO "alice"' in r for r in rendered)


def test_build_create_plan_marks_create_role_as_secret() -> None:
    spec = CreateRoleSpec(name="alice", password="hunter2_______________________")
    plan = build_create_plan(spec, ["demo_pg"])
    create_op = plan.cluster_ops[0]
    assert create_op.secret is True
    # The visible description must NOT contain the password.
    assert "hunter2" not in create_op.description


def test_build_create_plan_default_privileges() -> None:
    spec = CreateRoleSpec(
        name="alice",
        password="hunter2_______________________",
        default_table_select=("raw",),
    )
    plan = build_create_plan(spec, ["demo_pg"])
    rendered = [_render(op.statement) for op in plan.per_database_ops["demo_pg"]]
    assert any(
        'ALTER DEFAULT PRIVILEGES IN SCHEMA "raw"' in r
        and 'GRANT SELECT ON TABLES TO "alice"' in r
        for r in rendered
    )


def test_build_create_plan_each_db_gets_same_ops() -> None:
    spec = CreateRoleSpec(
        name="alice",
        password="hunter2_______________________",
        schema_usage=("raw",),
    )
    plan = build_create_plan(spec, ["demo_pg", "demo_rs"])
    rendered_a = [_render(op.statement) for op in plan.per_database_ops["demo_pg"]]
    rendered_b = [_render(op.statement) for op in plan.per_database_ops["demo_rs"]]
    assert rendered_a == rendered_b


def test_build_create_plan_validates() -> None:
    with pytest.raises(ValueError):
        build_create_plan(
            CreateRoleSpec(name="", password="hunter2_______________________"),
            ["demo_pg"],
        )
    with pytest.raises(ValueError):
        build_create_plan(
            CreateRoleSpec(name="alice", password="hunter2_______________________"),
            [],
        )


def test_build_create_plan_nologin_postgres() -> None:
    spec = CreateRoleSpec(name="readers", password=None)
    plan = build_create_plan(spec, ["demo_pg"])
    create_op = plan.cluster_ops[0]
    rendered = _render(create_op.statement)
    assert rendered == 'CREATE ROLE "readers" NOLOGIN'
    assert create_op.secret is False
    assert "PASSWORD" not in rendered


def test_build_create_plan_nologin_grants_and_membership_postgres() -> None:
    spec = CreateRoleSpec(
        name="readers",
        password=None,
        member_of=("analyst",),
        table_select=("raw",),
        default_table_select=("raw",),
    )
    plan = build_create_plan(spec, ["demo_pg"])
    cluster = [_render(op.statement) for op in plan.cluster_ops]
    assert any('GRANT "analyst" TO "readers"' in r for r in cluster)
    per_db = [_render(op.statement) for op in plan.per_database_ops["demo_pg"]]
    assert any('GRANT SELECT ON ALL TABLES IN SCHEMA "raw" TO "readers"' in r
               for r in per_db)
    assert any('ALTER DEFAULT PRIVILEGES IN SCHEMA "raw"' in r for r in per_db)


def test_build_create_plan_grant_to_postgres() -> None:
    spec = CreateRoleSpec(
        name="readers", password=None, grant_to=("alice", "bob"),
    )
    plan = build_create_plan(spec, ["demo_pg"])
    rendered = [_render(op.statement) for op in plan.cluster_ops]
    assert any('GRANT "readers" TO "alice"' in r for r in rendered)
    assert any('GRANT "readers" TO "bob"' in r for r in rendered)


def test_build_create_plan_grant_to_login_role_postgres() -> None:
    # Postgres allows granting a login role to another role.
    spec = CreateRoleSpec(
        name="alice", password="hunter2_______________________",
        grant_to=("admin_group",),
    )
    plan = build_create_plan(spec, ["demo_pg"])
    rendered = [_render(op.statement) for op in plan.cluster_ops]
    assert any('GRANT "alice" TO "admin_group"' in r for r in rendered)


def test_build_create_plan_nologin_redshift_creates_rbac_role() -> None:
    spec = CreateRoleSpec(name="readers", password=None)
    plan = build_create_plan(spec, ["dev"], RedshiftDialect())
    rendered = _render(plan.cluster_ops[0].statement)
    assert rendered == 'CREATE ROLE "readers"'


def test_build_create_plan_nologin_redshift_grants_use_to_role() -> None:
    spec = CreateRoleSpec(
        name="readers", password=None, table_select=("public",),
    )
    plan = build_create_plan(spec, ["dev"], RedshiftDialect())
    per_db = [_render(op.statement) for op in plan.per_database_ops["dev"]]
    assert any(
        'GRANT USAGE ON SCHEMA "public" TO ROLE "readers"' in r for r in per_db
    )
    assert any(
        'GRANT SELECT ON ALL TABLES IN SCHEMA "public" TO ROLE "readers"' in r
        for r in per_db
    )


def test_build_create_plan_nologin_redshift_membership_role_to_role() -> None:
    spec = CreateRoleSpec(name="readers", password=None, member_of=("rbac",))
    plan = build_create_plan(
        spec, ["dev"], RedshiftDialect(),
        parent_kinds={"rbac": ParentKind.role},
    )
    rendered = [_render(op.statement) for op in plan.cluster_ops]
    assert any('GRANT ROLE "rbac" TO ROLE "readers"' in r for r in rendered)


def test_build_create_plan_nologin_redshift_group_parent_rejected() -> None:
    spec = CreateRoleSpec(name="readers", password=None, member_of=("grp",))
    with pytest.raises(ValueError, match="legacy group"):
        build_create_plan(
            spec, ["dev"], RedshiftDialect(),
            parent_kinds={"grp": ParentKind.group},
        )


def test_build_create_plan_grant_to_redshift_user_and_role() -> None:
    spec = CreateRoleSpec(
        name="readers", password=None, grant_to=("alice", "rbac"),
    )
    plan = build_create_plan(
        spec, ["dev"], RedshiftDialect(),
        grantee_kinds={"alice": ParentKind.user, "rbac": ParentKind.role},
    )
    rendered = [_render(op.statement) for op in plan.cluster_ops]
    assert any('GRANT ROLE "readers" TO "alice"' in r for r in rendered)
    assert any('GRANT ROLE "readers" TO ROLE "rbac"' in r for r in rendered)


def test_build_create_plan_grant_to_redshift_group_target_rejected() -> None:
    spec = CreateRoleSpec(name="readers", password=None, grant_to=("grp",))
    with pytest.raises(ValueError, match="legacy group"):
        build_create_plan(
            spec, ["dev"], RedshiftDialect(),
            grantee_kinds={"grp": ParentKind.group},
        )


def test_build_create_plan_grant_to_redshift_login_user_rejected() -> None:
    spec = CreateRoleSpec(
        name="svc", password="hunter2_______________________",
        grant_to=("alice",),
    )
    with pytest.raises(ValueError, match="--no-login"):
        build_create_plan(
            spec, ["dev"], RedshiftDialect(),
            grantee_kinds={"alice": ParentKind.user},
        )


def test_build_create_plan_nologin_redshift_skips_default_privileges() -> None:
    spec = CreateRoleSpec(
        name="readers", password=None,
        default_table_select=("public",), default_table_all=("staging",),
    )
    warnings: list[str] = []
    plan = build_create_plan(spec, ["dev"], RedshiftDialect(), warnings=warnings)
    per_db = [_render(op.statement) for op in plan.per_database_ops["dev"]]
    assert not any("ALTER DEFAULT PRIVILEGES" in r for r in per_db)
    assert any("--default-table-select" in w for w in warnings)
    assert any("--default-table-all" in w for w in warnings)


def test_build_create_plan_login_redshift_default_privileges_kept() -> None:
    spec = CreateRoleSpec(
        name="svc", password="hunter2_______________________",
        default_table_select=("public",),
    )
    plan = build_create_plan(spec, ["dev"], RedshiftDialect())
    per_db = [_render(op.statement) for op in plan.per_database_ops["dev"]]
    assert any('ALTER DEFAULT PRIVILEGES IN SCHEMA "public"' in r for r in per_db)


_OWNERS = {"demo_pg": "demo_pg_root", "demo_rs": "admin"}


def test_resolve_reassign_owner_explicit_wins() -> None:
    assert resolve_reassign_owner("demo_pg", explicit="custom") == "custom"


def test_resolve_reassign_owner_per_db_defaults() -> None:
    assert (
        resolve_reassign_owner("demo_pg", explicit=None, defaults=_OWNERS)
        == "demo_pg_root"
    )
    assert (
        resolve_reassign_owner("demo_rs", explicit=None, defaults=_OWNERS)
        == "admin"
    )


def test_resolve_reassign_owner_unknown_db_raises() -> None:
    with pytest.raises(MissingReassignOwnerError):
        resolve_reassign_owner("unknown_db", explicit=None)


def test_build_drop_plan_uses_per_db_default() -> None:
    plan = build_drop_plan(
        "bd_dputri",
        ["demo_pg", "demo_rs"],
        reassign_to=None,
        no_reassign=False,
        defaults=_OWNERS,
    )
    rendered_demo_pg = [
        _render(op.statement) for op in plan.per_database_ops["demo_pg"]
    ]
    rendered_demo_rs = [
        _render(op.statement) for op in plan.per_database_ops["demo_rs"]
    ]
    assert any(
        'REASSIGN OWNED BY "bd_dputri" TO "demo_pg_root"' in r
        for r in rendered_demo_pg
    )
    assert any(
        'REASSIGN OWNED BY "bd_dputri" TO "admin"' in r for r in rendered_demo_rs
    )
    # DROP OWNED runs in every DB.
    for ops in plan.per_database_ops.values():
        assert any("DROP OWNED" in _render(op.statement) for op in ops)
    # DROP ROLE runs once at cluster scope.
    assert len(plan.cluster_ops) == 1
    assert "DROP ROLE" in _render(plan.cluster_ops[0].statement)


def test_build_drop_plan_no_reassign_skips_reassign() -> None:
    plan = build_drop_plan(
        "alice", ["demo_pg"], reassign_to=None, no_reassign=True,
    )
    rendered = [_render(op.statement) for op in plan.per_database_ops["demo_pg"]]
    assert not any("REASSIGN OWNED" in r for r in rendered)
    assert any("DROP OWNED" in r for r in rendered)


def test_build_drop_plan_explicit_owner_overrides_default() -> None:
    plan = build_drop_plan(
        "alice", ["demo_pg"], reassign_to="postgres", no_reassign=False,
    )
    rendered = [_render(op.statement) for op in plan.per_database_ops["demo_pg"]]
    assert any('REASSIGN OWNED BY "alice" TO "postgres"' in r for r in rendered)


def test_build_drop_plan_unknown_db_without_default_errors() -> None:
    with pytest.raises(MissingReassignOwnerError):
        build_drop_plan(
            "alice", ["new_db"], reassign_to=None, no_reassign=False,
        )


def test_build_drop_plan_mutually_exclusive_flags() -> None:
    with pytest.raises(ValueError):
        build_drop_plan(
            "alice", ["demo_pg"], reassign_to="x", no_reassign=True,
        )


def test_build_drop_plan_grant_membership_prepends_grant() -> None:
    plan = build_drop_plan(
        "bd_dputri",
        ["demo_pg"],
        reassign_to=None,
        no_reassign=False,
        grant_membership_to="demo_pg_root",
        defaults=_OWNERS,
    )
    assert len(plan.pre_cluster_ops) == 1
    rendered = _render(plan.pre_cluster_ops[0].statement)
    assert rendered == 'GRANT "bd_dputri" TO "demo_pg_root"'


def test_build_drop_plan_no_grant_membership_when_omitted() -> None:
    plan = build_drop_plan(
        "alice", ["demo_pg"], reassign_to=None, no_reassign=False,
        defaults=_OWNERS,
    )
    assert plan.pre_cluster_ops == []


class _FakeCursor:
    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows

    def execute(self, sql: str) -> None:
        self._executed = sql

    def fetchall(self) -> list[tuple]:
        return self._rows


def test_list_roles_maps_columns() -> None:
    cursor = _FakeCursor([
        ("alice", True, False, False, False, 2, 0),
        ("readers", False, False, False, False, 0, 5),
    ])
    rows = list_roles(cursor)
    assert [r.name for r in rows] == ["alice", "readers"]
    assert rows[0].can_login is True
    assert rows[0].member_of_count == 2
    assert rows[1].can_login is False
    assert rows[1].members_count == 5


def test_build_create_plan_redshift_uses_create_user() -> None:
    spec = CreateRoleSpec(name="svc", password="hunter2_______________________")
    plan = build_create_plan(spec, ["dev"], RedshiftDialect())
    assert _render(plan.cluster_ops[0].statement).startswith('CREATE USER "svc" PASSWORD ')


def test_build_create_plan_redshift_membership_group_and_role() -> None:
    spec = CreateRoleSpec(
        name="svc",
        password="hunter2_______________________",
        member_of=("grp", "rbac"),
    )
    plan = build_create_plan(
        spec, ["dev"], RedshiftDialect(),
        parent_kinds={"grp": ParentKind.group, "rbac": ParentKind.role},
    )
    rendered = [_render(op.statement) for op in plan.cluster_ops]
    assert any('ALTER GROUP "grp" ADD USER "svc"' in r for r in rendered)
    assert any('GRANT ROLE "rbac" TO "svc"' in r for r in rendered)


def test_build_create_plan_redshift_skips_sequences_with_warning() -> None:
    spec = CreateRoleSpec(
        name="svc",
        password="hunter2_______________________",
        sequence_usage=("public",),
    )
    warnings: list[str] = []
    plan = build_create_plan(
        spec, ["dev"], RedshiftDialect(), warnings=warnings,
    )
    rendered = [_render(op.statement) for op in plan.per_database_ops["dev"]]
    assert not any("SEQUENCES" in r for r in rendered)
    assert any("sequence" in w.lower() for w in warnings)


def test_build_drop_plan_redshift_reassigns_then_drops_user() -> None:
    owned = OwnedForDrop(
        schemas=["analytics"],
        relations=[("public", "orders", "r"), ("public", "orders_v", "v")],
    )
    plan = build_drop_plan(
        "svc", ["dev"], RedshiftDialect(),
        reassign_to="admin", no_reassign=False,
        owned=owned, groups=["reporting"],
    )
    per_db = [_render(op.statement) for op in plan.per_database_ops["dev"]]
    cluster = [_render(op.statement) for op in plan.cluster_ops]
    assert any('ALTER SCHEMA "analytics" OWNER TO "admin"' in r for r in per_db)
    assert any('ALTER TABLE "public"."orders" OWNER TO "admin"' in r for r in per_db)
    assert any('ALTER VIEW "public"."orders_v" OWNER TO "admin"' in r for r in per_db)
    assert any('ALTER GROUP "reporting" DROP USER "svc"' in r for r in cluster)
    assert any('DROP USER "svc"' in r for r in cluster)
    # Redshift never emits REASSIGN OWNED / DROP OWNED.
    assert not any("OWNED" in r for r in per_db + cluster)


def test_build_drop_plan_redshift_no_reassign_skips_owner_transfer() -> None:
    owned = OwnedForDrop(schemas=["analytics"], relations=[])
    plan = build_drop_plan(
        "svc", ["dev"], RedshiftDialect(),
        reassign_to=None, no_reassign=True, owned=owned, groups=[],
    )
    per_db = [_render(op.statement) for op in plan.per_database_ops["dev"]]
    assert not any("OWNER TO" in r for r in per_db)


def test_build_drop_plan_postgres_unchanged() -> None:
    # Regression guard: the default (Postgres) path is byte-for-byte identical.
    plan = build_drop_plan(
        "alice", ["demo_pg"], reassign_to=None, no_reassign=False,
        defaults=_OWNERS,
    )
    rendered = [_render(op.statement) for op in plan.per_database_ops["demo_pg"]]
    assert any('REASSIGN OWNED BY "alice" TO "demo_pg_root"' in r for r in rendered)
    assert "DROP ROLE" in _render(plan.cluster_ops[0].statement)
