from __future__ import annotations

from unittest.mock import MagicMock, patch

from rich.console import Console
from typer.testing import CliRunner

from dataplat.cli.db import app as db_app
from dataplat.cli.db.describe import render_description
from dataplat.services.db.connection import SqlEngine
from dataplat.services.db.describe import (
    ColumnInfo,
    ConstraintBundle,
    ObjectKind,
    PartitioningInfo,
    PrivilegeGrant,
    RelationHeader,
    SchemaContentItem,
    SchemaDescription,
    SchemaHeader,
    TableDescription,
    TargetRef,
    ViewDefinition,
    ViewDescription,
)


def _blank_table_description() -> TableDescription:
    ref = TargetRef(ObjectKind.table, "public", "users", 42)
    header = RelationHeader(
        "public",
        "users",
        "dbadmin",
        "pg_default",
        "App users",
        100,
        1024,
        512,
        256,
        256,
    )
    return TableDescription(
        ref=ref,
        header=header,
        columns=[ColumnInfo(1, "id", "bigint", False, None, True, None, None, None)],
        constraints=ConstraintBundle(None, [], [], []),
        indexes=[],
        privileges=[PrivilegeGrant("dbadmin", "OWNER", False, "")],
        triggers=[],
        policies=[],
        policies_enabled=False,
        partitioning=PartitioningInfo(None, None, None, []),
        redshift_distribution=None,
        redshift_stats=None,
        definition=None,
    )


def _markup_table_description() -> TableDescription:
    """A table whose every text field carries Rich markup."""
    from dataplat.services.db.describe import (
        ConstraintInfo,
        ForeignKeyInfo,
        IndexInfo,
        PolicyInfo,
        PrimaryKeyInfo,
        TriggerInfo,
    )

    ref = TargetRef(ObjectKind.table, "dev_[/x]", "fact[bold]", 42)
    header = RelationHeader(
        "dev_[/x]",
        "fact[bold]",
        "owner[/x]",
        "ts[bold]",
        "comment with [/issue] and [bold]",
        100,
        1024,
        512,
        256,
        256,
    )
    return TableDescription(
        ref=ref,
        header=header,
        columns=[
            ColumnInfo(
                1,
                "id[/x]",
                "bigint[bold]",
                False,
                "nextval('s[/x]')",
                True,
                "public.orgs[/x]",
                "id[bold]",
                "col comment [/issue]",
                "lzo[/x]",
            ),
        ],
        constraints=ConstraintBundle(
            PrimaryKeyInfo("pk[/x]", ["id[bold]"]),
            [
                ForeignKeyInfo(
                    "fk[/x]",
                    ["id[bold]"],
                    "public.orgs[/x]",
                    ["id"],
                    "CASCADE[/x]",
                    "CASCADE[bold]",
                    True,
                )
            ],
            [ConstraintInfo("uq[/x]", "UNIQUE (id[bold])")],
            [ConstraintInfo("ck[/x]", "CHECK (id > 0) [bold]")],
        ),
        indexes=[
            IndexInfo(
                "idx[/x]",
                ["id[bold]"],
                True,
                False,
                "btree[/x]",
                1024,
                "id > 0 [bold]",
            )
        ],
        privileges=[PrivilegeGrant("analyst[/x]", "SELECT[bold]", True, "dba[/x]")],
        triggers=[TriggerInfo("trg[/x]", "BEFORE[bold]", "INSERT[/x]", "fn[bold]()")],
        policies=[PolicyInfo("pol[/x]", "ALL[bold]", ["r[/x]"], "true [/issue]", None)],
        policies_enabled=True,
        partitioning=PartitioningInfo(
            None, "LIST", "LIST (src[/x])", [("p_[bold]", "FOR VALUES IN ('[/x]')")]
        ),
        redshift_distribution=None,
        redshift_stats=None,
        definition=None,
    )


class TestDescribeMarkupSafety:
    """Regression: catalog text is data, never Rich markup."""

    def _render(self, desc, engine: SqlEngine = SqlEngine.postgresql) -> str:
        console = Console(record=True, width=200)
        render_description(console, desc, engine)
        return console.export_text()

    def test_table_report_renders_every_field_literally(self) -> None:
        # Any of these used to raise MarkupError mid-render (closing tags) or
        # be silently swallowed (real style names).
        out = self._render(_markup_table_description())
        for expected in (
            "dev_[/x].fact[bold]",  # title card
            "owner[/x]",  # header metadata
            "comment with [/issue]",  # header comment
            "id[/x]",  # column name
            "bigint[bold]",  # column type
            "col comment [/issue]",  # column comment
            "public.orgs[/x](id[bold])",  # FK reference
            "pk[/x]",  # columns caption
            "idx[/x]",  # index name
            "id > 0 [bold]",  # index predicate
            "uq[/x]",  # unique constraint
            "CHECK (id > 0) [bold]",  # check constraint body
            "LIST on src[/x]",  # partition key summary
            "p_[bold]",  # partition child
            "'[/x]'",  # partition bounds
            "analyst[/x]",  # privilege grantee
            "trg[/x]",  # trigger name
            "pol[/x]",  # policy name
            "true [/issue]",  # policy USING clause
        ):
            assert expected in out, expected

    def test_view_dependencies_and_definition(self) -> None:
        from dataplat.services.db.describe import DependencyEdge

        desc = ViewDescription(
            ref=TargetRef(ObjectKind.view, "public", "v[/x]", 7),
            header=RelationHeader(
                "public",
                "v[/x]",
                "dba[bold]",
                None,
                "note [/issue]",
                None,
                None,
                None,
                None,
                None,
            ),
            columns=[
                ColumnInfo(1, "c[/x]", "int", True, None, False, None, None, None)
            ],
            definition=ViewDefinition(
                sql="SELECT 'closes [/issue] 42' AS x",
                is_updatable=False,
                check_option="CASCADED[bold]",
            ),
            upstream=[DependencyEdge("public.t[/x]", "table[bold]")],
            downstream=[DependencyEdge("public.d[bold]", "view[/x]")],
            privileges=[],
            triggers=[],
        )
        out = self._render(desc)
        for expected in (
            "public.v[/x]",
            "note [/issue]",
            "CASCADED[bold]",
            "public.t[/x]",
            "table[bold]",
            "public.d[bold]",
        ):
            assert expected in out, expected

    def test_schema_report_renders_names_literally(self) -> None:
        from dataplat.services.db.describe import DefaultPrivilegeGrant

        desc = SchemaDescription(
            header=SchemaHeader("dev_[/x]", "dba[bold]", "note [/issue]"),
            privileges=[PrivilegeGrant("analyst[/x]", "USAGE[bold]", False, "dba")],
            contents=[SchemaContentItem("t[/x]", "table[bold]", "dba[/x]", 100, 1024)],
            default_privileges=[
                DefaultPrivilegeGrant(
                    "app[/x]", "TABLE[bold]", ["SELECT[/x]"], False, "dba[bold]"
                )
            ],
        )
        out = self._render(desc)
        for expected in (
            "dev_[/x]",
            "dba[bold]",
            "note [/issue]",
            "analyst[/x]",
            "t[/x]",
            "table[bold]",
            "app[/x]",
            "SELECT[/x]",
        ):
            assert expected in out, expected

    def test_redshift_extras_render_keys_literally(self) -> None:
        from dataclasses import replace

        from dataplat.services.db.describe import (
            RedshiftDistribution,
            RedshiftTableStats,
        )

        desc = replace(
            _blank_table_description(),
            redshift_distribution=RedshiftDistribution(
                "KEY[/x]", "dk[bold]", "COMPOUND[/x]", ["sk[bold]"]
            ),
            redshift_stats=RedshiftTableStats(1.0, 2.0, False),
        )
        out = self._render(desc, SqlEngine.redshift)
        for expected in ("KEY[/x]", "dk[bold]", "COMPOUND[/x]", "sk[bold]"):
            assert expected in out, expected


def test_describe_unknown_target_error_escapes_name(monkeypatch) -> None:
    """The failing target name is echoed back; it may contain markup."""
    monkeypatch.setenv("DEMO_PG_HOST", "localhost")
    monkeypatch.setenv("DEMO_PG_USER", "dbadmin")
    monkeypatch.setenv("DEMO_PG_PASSWORD", "x")
    monkeypatch.setenv("DEMO_PG_DATABASE", "analytics")

    connect = _make_mock_connect([])  # fetchone() -> None: schema not found
    with patch("dataplat.cli.db._common.psycopg.connect", connect):
        result = CliRunner().invoke(db_app, ["describe", "no[/x]pe"])

    assert result.exit_code == 1
    assert "no[/x]pe" in result.output


def test_render_table_does_not_crash() -> None:
    console = Console(record=True, width=120)
    render_description(console, _blank_table_description(), SqlEngine.postgresql)
    out = console.export_text()
    assert "public.users" in out
    assert "bigint" in out
    assert "dbadmin" in out


def test_render_table_omits_owner_from_privileges() -> None:
    """Ownership shown in header; Privileges section hidden when only OWNER exists."""
    from rich.console import Console

    console = Console(record=True, width=120)
    # _blank_table_description()'s only grant is PrivilegeGrant("dbadmin", "OWNER").
    render_description(console, _blank_table_description(), SqlEngine.postgresql)
    out = console.export_text()
    # Header shows owner
    assert "dbadmin" in out
    # No standalone "Privileges" section title when only owner is present
    # (section title appears nowhere because section is skipped entirely)
    assert "Privileges" not in out


def test_render_table_partition_child_shows_parent_line() -> None:
    from dataclasses import replace

    from rich.console import Console

    desc = _blank_table_description()
    # Replace partitioning info to simulate a child
    desc = replace(
        desc,
        partitioning=PartitioningInfo(
            parent="analytics.fct_events",
            strategy=None,
            partition_key=None,
            children=[],
        ),
    )
    console = Console(record=True, width=120)
    render_description(console, desc, SqlEngine.postgresql)
    out = console.export_text()
    assert "analytics.fct_events" in out  # parent name present


def test_render_table_has_title_card() -> None:
    """Report-style output opens with a panel-bordered title card."""
    console = Console(record=True, width=120)
    render_description(console, _blank_table_description(), SqlEngine.postgresql)
    out = console.export_text()
    # Qualified name appears near the top of the output.
    lines = [ln for ln in out.splitlines() if ln.strip()]
    top = "\n".join(lines[:6])
    assert "public.users" in top
    # The title card is a rounded panel — first visible renderable should
    # include panel corners (╭ / ╰) or at least the ─ rule of the panel.
    assert any(ch in out for ch in ("╭", "╰", "─"))
    # Subtitle "Table" appears inside the card.
    assert "Table" in out


def test_render_partition_bounds_stripped() -> None:
    from dataclasses import replace

    desc = replace(
        _blank_table_description(),
        partitioning=PartitioningInfo(
            parent=None,
            strategy="LIST",
            partition_key="LIST (source)",
            children=[("public.p_admission", "FOR VALUES IN ('admission')")],
        ),
    )
    console = Console(record=True, width=120)
    render_description(console, desc, SqlEngine.postgresql)
    out = console.export_text()
    assert "'admission'" in out
    assert "FOR VALUES IN" not in out  # prefix stripped


def test_render_partition_child_shows_parent_only() -> None:
    from dataclasses import replace

    desc = replace(
        _blank_table_description(),
        partitioning=PartitioningInfo(
            parent="analytics.fct_events",
            strategy=None,
            partition_key=None,
            children=[],
        ),
    )
    console = Console(record=True, width=120)
    render_description(console, desc, SqlEngine.postgresql)
    out = console.export_text()
    assert "Partition of" in out
    assert "analytics.fct_events" in out


def test_render_table_no_fk_column_when_empty() -> None:
    console = Console(record=True, width=120)
    render_description(console, _blank_table_description(), SqlEngine.postgresql)
    out = console.export_text()
    # Default table fixture has no FK, so no arrow character should appear
    # in column output (the constraints section also isn't rendered).
    assert "→" not in out


def test_render_identity_column_label() -> None:
    from dataclasses import replace

    desc = _blank_table_description()
    desc = replace(
        desc,
        columns=[
            ColumnInfo(
                1,
                "id",
                "bigint",
                False,
                "GENERATED BY DEFAULT AS IDENTITY",
                True,
                None,
                None,
                None,
            ),
        ],
    )
    console = Console(record=True, width=120)
    render_description(console, desc, SqlEngine.postgresql)
    out = console.export_text()
    assert "identity (default)" in out
    assert "GENERATED BY DEFAULT" not in out


def test_render_view_does_not_crash() -> None:
    ref = TargetRef(ObjectKind.view, "public", "user_view", 7)
    header = RelationHeader(
        "public",
        "user_view",
        "dbadmin",
        None,
        None,
        None,
        None,
        None,
        None,
        None,
    )
    desc = ViewDescription(
        ref=ref,
        header=header,
        columns=[ColumnInfo(1, "id", "bigint", True, None, False, None, None, None)],
        definition=ViewDefinition(
            sql="SELECT id FROM users;", is_updatable=True, check_option=None
        ),
        upstream=[],
        downstream=[],
        privileges=[PrivilegeGrant("dbadmin", "OWNER", False, "")],
        triggers=[],
    )
    console = Console(record=True, width=120)
    render_description(console, desc, SqlEngine.postgresql)
    out = console.export_text()
    assert "public.user_view" in out
    assert "SELECT id FROM users" in out


def test_render_schema_does_not_crash() -> None:
    console = Console(record=True, width=120)
    desc = SchemaDescription(
        header=SchemaHeader("public", "dbadmin", None),
        privileges=[],
        contents=[SchemaContentItem("users", "table", "dbadmin", 100, 1024)],
    )
    render_description(console, desc, SqlEngine.postgresql)
    out = console.export_text()
    assert "public" in out
    assert "users" in out


def _make_mock_connect(fetch_results: list[object]) -> MagicMock:
    """Build a context-manager-compatible psycopg.connect mock."""
    cursor = MagicMock()
    cursor.description = None
    cursor.__enter__ = lambda self: self
    cursor.__exit__ = lambda self, *a: None
    results = list(fetch_results)

    def _execute(query, params=None):
        cursor.last_query = str(query)

    def _fetchone():
        return results.pop(0) if results else None

    def _fetchall():
        if not results:
            return []
        head = results.pop(0)
        return head if isinstance(head, list) else [head]

    cursor.execute = _execute
    cursor.fetchone = _fetchone
    cursor.fetchall = _fetchall

    conn = MagicMock()
    conn.__enter__ = lambda self: self
    conn.__exit__ = lambda self, *a: None
    conn.cursor = lambda: cursor

    return MagicMock(return_value=conn)


def test_describe_schema_end_to_end(monkeypatch) -> None:
    monkeypatch.setenv("DEMO_PG_HOST", "localhost")
    monkeypatch.setenv("DEMO_PG_USER", "dbadmin")
    monkeypatch.setenv("DEMO_PG_PASSWORD", "x")
    monkeypatch.setenv("DEMO_PG_DATABASE", "analytics")

    fetch_results = [
        (12345,),  # resolve_target -> pg_namespace truthy row
        ("public", "dbadmin", None),  # fetch_schema_header
        [("analyst", "USAGE", False, "dbadmin")],  # fetch_schema_privileges
        [],  # fetch_schema_default_privileges
        [("users", "r", "dbadmin", 100, 1024)],  # fetch_schema_contents
    ]
    connect = _make_mock_connect(fetch_results)
    with patch("dataplat.cli.db._common.psycopg.connect", connect):
        result = CliRunner().invoke(db_app, ["describe", "public"])
    assert result.exit_code == 0, result.output
    assert "public" in result.output
    assert "users" in result.output
    # Schema output surfaces privileges again now that the section is restored.
    assert "analyst" in result.output
    # Contents is still rendered (section number depends on what else rendered).
    assert "Contents" in result.output


def test_render_table_sections_are_numbered() -> None:
    """Body sections appear with `N. Title` numbering."""
    console = Console(record=True, width=120)
    render_description(console, _blank_table_description(), SqlEngine.postgresql)
    out = console.export_text()
    assert "1. Columns" in out


def test_render_view_definition_is_last() -> None:
    """View output places the Definition section after Privileges/Dependencies."""
    ref = TargetRef(ObjectKind.view, "public", "user_view", 7)
    header = RelationHeader(
        "public",
        "user_view",
        "dbadmin",
        None,
        None,
        None,
        None,
        None,
        None,
        None,
    )
    desc = ViewDescription(
        ref=ref,
        header=header,
        columns=[ColumnInfo(1, "id", "bigint", True, None, False, None, None, None)],
        definition=ViewDefinition(
            sql="SELECT id FROM users;", is_updatable=True, check_option=None
        ),
        upstream=[],
        downstream=[],
        privileges=[
            PrivilegeGrant("analyst", "SELECT", False, "dbadmin"),
        ],
        triggers=[],
    )
    console = Console(record=True, width=120)
    render_description(console, desc, SqlEngine.postgresql)
    out = console.export_text()
    idx_priv = out.find("Privileges")
    idx_def = out.find("Definition")
    assert idx_priv != -1 and idx_def != -1
    assert idx_priv < idx_def


def test_render_schema_sections() -> None:
    """Section ordering flexes based on which data is present."""
    # No privileges, contents with size → Highlights + Contents.
    console = Console(record=True, width=120)
    desc = SchemaDescription(
        header=SchemaHeader("public", "dbadmin", None),
        privileges=[],
        contents=[SchemaContentItem("users", "table", "dbadmin", 100, 1024)],
    )
    render_description(console, desc, SqlEngine.postgresql)
    out = console.export_text()
    assert "1. Highlights" in out
    assert "2. Contents" in out
    assert "3. " not in out

    # No privileges, contents with no size → Contents only as section 1.
    console = Console(record=True, width=120)
    desc = SchemaDescription(
        header=SchemaHeader("public", "dbadmin", None),
        privileges=[],
        contents=[SchemaContentItem("v_users", "view", "dbadmin", None, None)],
    )
    render_description(console, desc, SqlEngine.postgresql)
    out = console.export_text()
    assert "1. Contents" in out
    assert "Highlights" not in out
    assert "2. " not in out

    # Privileges + sized contents → all three sections, Privileges first.
    console = Console(record=True, width=120)
    desc = SchemaDescription(
        header=SchemaHeader("public", "dbadmin", None),
        privileges=[
            PrivilegeGrant("dbadmin", "OWNER", False, ""),
            PrivilegeGrant("analyst", "USAGE", False, "dbadmin"),
        ],
        contents=[SchemaContentItem("users", "table", "dbadmin", 100, 1024)],
    )
    render_description(console, desc, SqlEngine.postgresql)
    out = console.export_text()
    assert "1. Privileges" in out
    assert "2. Highlights" in out
    assert "3. Contents" in out


def test_render_schema_title_card_shows_totals() -> None:
    from dataplat.services.db.describe import (
        SchemaContentItem,
        SchemaDescription,
        SchemaHeader,
    )

    desc = SchemaDescription(
        header=SchemaHeader("public", "dbadmin", None),
        privileges=[],
        contents=[
            SchemaContentItem("users", "table", "dbadmin", 100, 1024),
            SchemaContentItem("orders", "table", "dbadmin", 200, 2048),
        ],
    )
    console = Console(record=True, width=120)
    render_description(console, desc, SqlEngine.postgresql)
    out = console.export_text()
    assert "3.0 KiB" in out  # 1024 + 2048
    assert "300" in out  # 100 + 200 rows


def test_render_schema_privileges_section_present_when_grants_exist() -> None:
    from dataplat.services.db.describe import (
        PrivilegeGrant,
        SchemaDescription,
        SchemaHeader,
    )

    desc = SchemaDescription(
        header=SchemaHeader("public", "dbadmin", None),
        privileges=[
            PrivilegeGrant("dbadmin", "OWNER", False, ""),
            PrivilegeGrant("analyst", "USAGE", False, "dbadmin"),
        ],
        contents=[],
    )
    console = Console(record=True, width=120)
    render_description(console, desc, SqlEngine.postgresql)
    out = console.export_text()
    assert "Privileges" in out
    assert "analyst" in out


def test_render_schema_highlights_top_largest() -> None:
    from dataplat.services.db.describe import (
        SchemaContentItem,
        SchemaDescription,
        SchemaHeader,
    )

    # 7 items, different sizes — only top 5 should appear under Highlights
    sizes = [10, 20, 30, 40, 50, 60, 70]
    contents = [
        SchemaContentItem(f"tbl_{s}", "table", "dbadmin", None, s * 1024 * 1024)
        for s in sizes
    ]
    desc = SchemaDescription(
        header=SchemaHeader("public", "dbadmin", None),
        privileges=[],
        contents=contents,
    )
    console = Console(record=True, width=120)
    render_description(console, desc, SqlEngine.postgresql)
    out = console.export_text()
    assert "Highlights" in out
    # Top 5 by size = 70, 60, 50, 40, 30
    assert "tbl_70" in out
    assert "tbl_30" in out
    # Smallest two should still appear in Contents but not in Highlights.
    # We don't have a clean way to assert "not in Highlights specifically"
    # without parsing sections, so at minimum verify tbl_10 appears somewhere
    # (in Contents) — confirming it wasn't lost.
    assert "tbl_10" in out


def test_describe_malformed_target_exits_1(monkeypatch) -> None:
    monkeypatch.setenv("DEMO_PG_HOST", "localhost")
    monkeypatch.setenv("DEMO_PG_USER", "dbadmin")
    monkeypatch.setenv("DEMO_PG_PASSWORD", "x")
    monkeypatch.setenv("DEMO_PG_DATABASE", "analytics")
    connect = _make_mock_connect([])
    with patch("dataplat.cli.db._common.psycopg.connect", connect):
        result = CliRunner().invoke(db_app, ["describe", "a.b.c"])
    assert result.exit_code == 1


def test_render_schema_default_privileges_section() -> None:
    from dataplat.services.db.describe import (
        DefaultPrivilegeGrant,
        SchemaDescription,
        SchemaHeader,
    )

    desc = SchemaDescription(
        header=SchemaHeader("public", "dbadmin", None),
        privileges=[],
        contents=[],
        default_privileges=[
            DefaultPrivilegeGrant(
                "app",
                "TABLE",
                ["SELECT", "INSERT", "UPDATE", "DELETE"],
                False,
                "dbadmin",
            ),
            DefaultPrivilegeGrant("analyst", "TABLE", ["SELECT"], True, "dbadmin"),
        ],
    )
    console = Console(record=True, width=120)
    render_description(console, desc, SqlEngine.postgresql)
    out = console.export_text()
    assert "Default privileges" in out
    assert "app" in out
    assert "TABLE" in out
    assert "SELECT, INSERT, UPDATE, DELETE" in out
    assert "analyst" in out


def test_render_schema_default_privileges_omitted_when_empty() -> None:
    from dataplat.services.db.describe import SchemaDescription, SchemaHeader

    desc = SchemaDescription(
        header=SchemaHeader("public", "dbadmin", None),
        privileges=[],
        contents=[],
        default_privileges=[],
    )
    console = Console(record=True, width=120)
    render_description(console, desc, SqlEngine.postgresql)
    out = console.export_text()
    assert "Default privileges" not in out


def test_describe_comma_separated_targets(monkeypatch) -> None:
    monkeypatch.setenv("DEMO_PG_HOST", "localhost")
    monkeypatch.setenv("DEMO_PG_USER", "dbadmin")
    monkeypatch.setenv("DEMO_PG_PASSWORD", "x")
    monkeypatch.setenv("DEMO_PG_DATABASE", "analytics")

    fetch_results = [
        # describe "public"
        (1,),  # resolve_target schema row
        ("public", "dbadmin", None),  # schema header
        [],  # schema privileges
        [],  # schema default privileges
        [],  # schema contents
        # describe "raw"
        (2,),  # resolve_target schema row
        ("raw", "dbadmin", None),  # schema header
        [],  # schema privileges
        [],  # schema default privileges
        [],  # schema contents
    ]
    connect = _make_mock_connect(fetch_results)
    with patch("dataplat.cli.db._common.psycopg.connect", connect):
        result = CliRunner().invoke(db_app, ["describe", "public, raw"])
    assert result.exit_code == 0, result.output
    assert "public" in result.output
    assert "raw" in result.output


def test_describe_comma_separated_continues_on_failure(monkeypatch) -> None:
    monkeypatch.setenv("DEMO_PG_HOST", "localhost")
    monkeypatch.setenv("DEMO_PG_USER", "dbadmin")
    monkeypatch.setenv("DEMO_PG_PASSWORD", "x")
    monkeypatch.setenv("DEMO_PG_DATABASE", "analytics")

    fetch_results = [
        # describe "missing" — resolve_target returns None (schema not found)
        None,
        # describe "public"
        (1,),
        ("public", "dbadmin", None),
        [],
        [],
        [],
    ]
    connect = _make_mock_connect(fetch_results)
    with patch("dataplat.cli.db._common.psycopg.connect", connect):
        result = CliRunner().invoke(db_app, ["describe", "missing,public"])
    assert result.exit_code == 0, result.output
    assert "Error (missing)" in result.output
    assert "public" in result.output
