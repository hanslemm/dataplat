from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import duckdb
from typer.testing import CliRunner

from dataplat.cli.db import app as db_app
from dataplat.cli.db.top_tables import _split_prefixes
from dataplat.core.errors import ConfigError, ExitCode
from dataplat.services.db.connection import SqlEngine
from dataplat.services.db.top_tables import TopTableRow, TopTablesResult


def test_split_prefixes_flattens_and_dedupes() -> None:
    assert _split_prefixes(["dev_"]) == ["dev_"]
    assert _split_prefixes(["dev_,sandbox_", " stage_ "]) == [
        "dev_",
        "sandbox_",
        "stage_",
    ]
    assert _split_prefixes(["dev_", "dev_", "sandbox_"]) == ["dev_", "sandbox_"]
    assert _split_prefixes([""]) == []


def _fake_collect(target, prefixes: list[str], limit: int):
    del prefixes, limit
    if target.engine is SqlEngine.postgresql:
        return TopTablesResult(
            rows=[
                TopTableRow(
                    "dev_alice", "big_fact", "r", "alice", 1_000_000, 8 * 1024 * 1024
                ),
                TopTableRow("dev_bob", "tmp", "m", "bob", 42, 2 * 1024 * 1024),
            ],
            matched_bytes=16 * 1024 * 1024,
            matched_count=5,
            disk_bytes=100 * 1024 * 1024,  # 8 MiB/100 MiB = 8.0%; 2/100 = 2.0%
        )
    return TopTablesResult(
        rows=[
            TopTableRow("dev_rs", "events", "r", None, 9_000_000, 2048 * 1024 * 1024),
        ],
        matched_bytes=2048 * 1024 * 1024,
        matched_count=1,
        disk_bytes=4096 * 1024 * 1024,  # 50% of disk
    )


def test_top_tables_text_output_shows_percent_of_disk_and_totals() -> None:
    runner = CliRunner()
    with patch("dataplat.cli.db.top_tables._collect", side_effect=_fake_collect):
        result = runner.invoke(db_app, ["top-tables"])

    assert result.exit_code == 0, result.output
    assert "Postgres" in result.output
    assert "Redshift" in result.output
    assert "% of disk" in result.output
    assert "% of total" not in result.output
    # Postgres rows: 8 MiB / 100 MiB = 8.0%; 2 MiB / 100 MiB = 2.0%
    assert "8.0%" in result.output
    assert "2.0%" in result.output
    # Footer: database disk + matched share + top share
    assert "Database disk" in result.output
    assert "100.0 MiB" in result.output
    assert "16.0%" in result.output  # 16 MiB matched / 100 MiB disk
    assert "10.0%" in result.output  # top-2 shown (10 MiB) / 100 MiB disk
    # Redshift: single row = 50% of disk
    assert "50.0%" in result.output


def test_top_tables_limit_to_single_engine() -> None:
    runner = CliRunner()
    calls: list[SqlEngine] = []

    def spy(target, prefixes: list[str], limit: int):
        calls.append(target.engine)
        return _fake_collect(target, prefixes, limit)

    with patch("dataplat.cli.db.top_tables._collect", side_effect=spy):
        result = runner.invoke(db_app, ["top-tables", "--target", "demo_pg"])

    assert result.exit_code == 0, result.output
    assert calls == [SqlEngine.postgresql]
    assert "Redshift" not in result.output


def test_top_tables_json_includes_totals() -> None:
    runner = CliRunner()
    with patch("dataplat.cli.db.top_tables._collect", side_effect=_fake_collect):
        result = runner.invoke(
            db_app,
            [
                "top-tables",
                "--schema-prefix",
                "dev_,sandbox_",
                "--limit",
                "3",
                "--json",
            ],
        )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["schema_prefixes"] == ["dev_", "sandbox_"]
    assert payload["limit"] == 3
    demo_pg = payload["databases"]["demo_pg"]
    assert demo_pg["matched_bytes"] == 16 * 1024 * 1024
    assert demo_pg["matched_count"] == 5
    assert demo_pg["disk_bytes"] == 100 * 1024 * 1024
    assert demo_pg["rows"][0]["schema"] == "dev_alice"
    assert demo_pg["rows"][0]["kind"] == "r"
    assert demo_pg["error"] is None
    assert payload["databases"]["demo_rs"]["matched_count"] == 1
    assert payload["databases"]["demo_rs"]["disk_bytes"] == 4096 * 1024 * 1024


def test_top_tables_drop_sql_emits_script() -> None:
    runner = CliRunner()
    with patch("dataplat.cli.db.top_tables._collect", side_effect=_fake_collect):
        result = runner.invoke(db_app, ["top-tables", "--drop-sql"])

    assert result.exit_code == 0, result.output
    out = result.stdout
    assert "-- Postgres" in out
    assert "-- Redshift" in out
    assert 'DROP TABLE IF EXISTS "dev_alice"."big_fact";' in out
    assert 'DROP MATERIALIZED VIEW IF EXISTS "dev_bob"."tmp";' in out
    assert 'DROP TABLE IF EXISTS "dev_rs"."events";' in out
    assert "BEGIN;" in out
    assert "COMMIT;" in out
    assert "Run against DEMO_PG_*" in out
    assert "Run against DEMO_RS_*" in out
    # Percentages printed are against disk, not matched subtotal
    assert "Database disk:" in out
    assert "of disk" in out
    assert "8.0% of disk" in out  # 8 MiB / 100 MiB


def test_top_tables_unknown_target_exits_invalid_input() -> None:
    runner = CliRunner()
    result = runner.invoke(db_app, ["top-tables", "-t", "nope"])
    assert result.exit_code == ExitCode.INVALID_INPUT
    assert "Unknown target" in result.output


def test_top_tables_json_and_drop_sql_mutually_exclusive() -> None:
    runner = CliRunner()
    result = runner.invoke(db_app, ["top-tables", "--json", "--drop-sql"])
    assert result.exit_code == 1
    assert "mutually exclusive" in result.output


def test_top_tables_empty_prefix_rejected() -> None:
    runner = CliRunner()
    result = runner.invoke(db_app, ["top-tables", "--schema-prefix", ","])
    assert result.exit_code == 1
    assert "schema-prefix" in result.output


def _markup_collect(target, prefixes: list[str], limit: int):
    del prefixes, limit
    return TopTablesResult(
        rows=[
            TopTableRow("dev_[/x]", "[bold]fact", "r", "alice[/x]", 10, 1024 * 1024),
        ],
        matched_bytes=1024 * 1024,
        matched_count=1,
        disk_bytes=100 * 1024 * 1024,
    )


def test_top_tables_renders_markup_like_names_literally() -> None:
    """Regression: schema/table/owner names are data, not markup."""
    runner = CliRunner()
    with patch("dataplat.cli.db.top_tables._collect", side_effect=_markup_collect):
        result = runner.invoke(db_app, ["top-tables", "-t", "demo_pg"])

    assert result.exit_code == 0, result.output
    assert "dev_[/x]" in result.output
    assert "[bold]fact" in result.output
    assert "alice[/x]" in result.output


def test_top_tables_escapes_schema_prefix_in_header() -> None:
    """--schema-prefix is user input and reaches two markup strings."""
    runner = CliRunner()
    with patch("dataplat.cli.db.top_tables._collect", side_effect=_markup_collect):
        result = runner.invoke(
            db_app, ["top-tables", "-t", "demo_pg", "--schema-prefix", "[/x]dev"]
        )

    assert result.exit_code == 0, result.output
    assert "[/x]dev*" in result.output


def test_top_tables_drop_requires_yes_non_interactive() -> None:
    runner = CliRunner()
    with (
        patch("dataplat.cli.db.top_tables._collect", side_effect=_fake_collect),
        patch("dataplat.cli.db.top_tables._execute_drops") as executed,
    ):
        result = runner.invoke(db_app, ["top-tables", "--drop"])

    assert result.exit_code == 1
    assert "--yes" in result.output
    executed.assert_not_called()


def test_top_tables_drop_prompt_keeps_its_wording(monkeypatch) -> None:
    """The shared gate must ask the question this command always asked."""
    import typer

    from dataplat.cli import _prompt

    prompts: list[str] = []
    # The gate reads sys.stdin.isatty() at call time; CliRunner's stdin is not
    # a TTY, so fake the module's view of it to reach the prompt branch.
    monkeypatch.setattr(
        _prompt, "sys", SimpleNamespace(stdin=SimpleNamespace(isatty=lambda: True))
    )
    monkeypatch.setattr(
        typer, "confirm", lambda text, default=False: prompts.append(text) or False
    )

    runner = CliRunner()
    with (
        patch("dataplat.cli.db.top_tables._collect", side_effect=_fake_collect),
        patch("dataplat.cli.db.top_tables._execute_drops") as executed,
    ):
        result = runner.invoke(db_app, ["top-tables", "--drop"])

    assert result.exit_code == 1
    assert prompts == ["DROP the 3 table(s) listed above? This cannot be undone."]
    executed.assert_not_called()


def test_top_tables_drop_with_yes_executes() -> None:
    runner = CliRunner()
    with (
        patch("dataplat.cli.db.top_tables._collect", side_effect=_fake_collect),
        patch("dataplat.cli.db.top_tables._execute_drops", return_value=2) as executed,
    ):
        result = runner.invoke(db_app, ["top-tables", "--drop", "--yes"])

    assert result.exit_code == 0, result.output
    assert executed.call_count == 2  # one per target


# =========================================================================
# DuckDB. Nothing is patched below: the command opens a real DuckDB file,
# because that is the only way to find out that the numbers it prints are not
# the numbers it printed for a server.
# =========================================================================


def _duckdb_target(monkeypatch, path: str | Path, *, read_only: bool = False) -> None:
    """Declare a third target, `demo_ddb`, alongside the suite's two."""
    monkeypatch.setenv("DP_TARGETS", "demo_pg,demo_rs,demo_ddb")
    monkeypatch.setenv("DEMO_DDB_ENGINE", "duckdb")
    monkeypatch.setenv("DEMO_DDB_PATH", str(path))
    if read_only:
        monkeypatch.setenv("DEMO_DDB_READ_ONLY", "1")


def _make_warehouse(path: Path) -> Path:
    conn = duckdb.connect(database=str(path))
    conn.execute("CREATE SCHEMA dev_alice")
    conn.execute("CREATE SCHEMA dev_bob")
    conn.execute("CREATE SCHEMA prod")
    conn.execute("CREATE TABLE dev_alice.big_fact(id BIGINT)")
    conn.execute("INSERT INTO dev_alice.big_fact SELECT i FROM range(3000) t(i)")
    conn.execute("CREATE TABLE dev_bob.tmp(a INTEGER)")
    conn.execute("CREATE TABLE prod.keepme(a INTEGER)")
    conn.execute("CHECKPOINT")
    conn.close()
    return path


def test_top_tables_duckdb_reports_rows_and_no_size(monkeypatch, tmp_path) -> None:
    """The DuckDB section shows what DuckDB knows, and says what it does not."""
    _duckdb_target(monkeypatch, _make_warehouse(tmp_path / "w.duckdb"))
    runner = CliRunner()

    result = runner.invoke(db_app, ["top-tables", "-t", "demo_ddb"])

    assert result.exit_code == 0, result.output
    out = result.output
    assert "DuckDB" in out
    assert "ranked by estimated rows" in out
    assert "Rows (est.)" in out
    assert "3,000" in out
    # The two byte-valued columns are absent rather than full of dashes, and the
    # footer names the file total instead of implying a share of it.
    assert "% of disk" not in out
    assert "Database file:" in out
    assert "sizes unknown" in out
    # And why, where the reader is already looking.
    assert "estimated_size" in out
    assert "pragma_database_size" in out
    assert "not comparable" in out
    # prod does not match dev_.
    assert "keepme" not in out


def test_top_tables_duckdb_json_marks_the_unknown_sizes(monkeypatch, tmp_path) -> None:
    _duckdb_target(monkeypatch, _make_warehouse(tmp_path / "w.duckdb"))
    runner = CliRunner()

    result = runner.invoke(db_app, ["top-tables", "-t", "demo_ddb", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)["databases"]["demo_ddb"]
    assert payload["engine"] == "duckdb"
    assert payload["ranked_by"] == "row_estimate"
    assert "estimated" in payload["size_basis"]
    assert payload["matched_bytes"] is None
    assert payload["matched_count"] == 2
    assert payload["disk_bytes"] > 0
    # null, not 0: a consumer must be able to tell "unknown" from "empty".
    assert [row["size_bytes"] for row in payload["rows"]] == [None, None]
    assert payload["rows"][0]["row_estimate"] == 3000


def test_top_tables_postgres_json_still_carries_its_own_basis() -> None:
    """The new keys are per-engine facts, so the libpq engines get theirs too."""
    runner = CliRunner()
    with patch("dataplat.cli.db.top_tables._collect", side_effect=_fake_collect):
        result = runner.invoke(db_app, ["top-tables", "--json"])

    assert result.exit_code == 0, result.output
    databases = json.loads(result.stdout)["databases"]
    assert databases["demo_pg"]["ranked_by"] == "size_bytes"
    assert "pg_total_relation_size" in databases["demo_pg"]["size_basis"]
    assert "svv_table_info" in databases["demo_rs"]["size_basis"]


def test_top_tables_duckdb_drop_sql_is_honest_about_dependents(
    monkeypatch, tmp_path
) -> None:
    """The script must be valid DuckDB *and* not promise a guard DuckDB lacks."""
    _duckdb_target(monkeypatch, _make_warehouse(tmp_path / "w.duckdb"))
    runner = CliRunner()

    result = runner.invoke(db_app, ["top-tables", "-t", "demo_ddb", "--drop-sql"])

    assert result.exit_code == 0, result.output
    out = result.stdout
    assert "-- DuckDB" in out
    assert 'DROP TABLE IF EXISTS "dev_alice"."big_fact";' in out
    assert "DROP MATERIALIZED VIEW" not in out  # DuckDB has none, and rejects it
    assert "~3,000 rows" in out
    assert "BEGIN;" in out and "COMMIT;" in out
    # The libpq script promises dependent views will block the drop. On DuckDB
    # they do not (probed on 1.5.5), so printing that line here would tell a
    # reviewer the script is safer than it is.
    assert "dependent views/FKs will block" not in out
    assert "does NOT block a drop on a dependent view" in out
    assert "of disk" not in out


def test_top_tables_duckdb_drop_executes_against_the_file(
    monkeypatch, tmp_path
) -> None:
    database = _make_warehouse(tmp_path / "w.duckdb")
    _duckdb_target(monkeypatch, database)
    runner = CliRunner()

    result = runner.invoke(db_app, ["top-tables", "-t", "demo_ddb", "--drop", "--yes"])

    assert result.exit_code == 0, result.output
    assert "dropped dev_alice.big_fact" in result.output
    conn = duckdb.connect(database=str(database))
    remaining = conn.execute(
        "SELECT table_name FROM duckdb_tables() "
        "WHERE database_name = current_database() ORDER BY 1"
    ).fetchall()
    conn.close()
    assert remaining == [("keepme",)]


def test_top_tables_duckdb_read_only_target_refuses_the_drop(
    monkeypatch, tmp_path
) -> None:
    """<PREFIX>_READ_ONLY is a real guard, and the failure is per target."""
    database = _make_warehouse(tmp_path / "w.duckdb")
    _duckdb_target(monkeypatch, database, read_only=True)
    runner = CliRunner()

    result = runner.invoke(db_app, ["top-tables", "-t", "demo_ddb", "--drop", "--yes"])

    assert result.exit_code == 1
    assert "read-only" in result.output
    conn = duckdb.connect(database=str(database), read_only=True)
    count = conn.execute(
        "SELECT count(*) FROM duckdb_tables() WHERE database_name = current_database()"
    ).fetchone()
    conn.close()
    assert count == (3,)  # DuckDB rolls the transaction back: nothing dropped


def test_top_tables_duckdb_missing_file_is_one_targets_error(
    monkeypatch, tmp_path
) -> None:
    """Regression: a DuckDB target used to abort the run with a traceback.

    ``_collect`` resolved through the libpq-only resolver, which raises
    ValidationError for a DuckDB target — a type this command's loop does not
    catch — so a single DuckDB entry in DP_TARGETS took down `-t all` for every
    other database too. Now it is a line like any other unreachable target.
    """
    _duckdb_target(monkeypatch, tmp_path / "not-there.duckdb")
    runner = CliRunner()

    result = runner.invoke(db_app, ["top-tables", "-t", "demo_ddb"])

    assert result.exit_code == 1
    assert "DuckDB" in result.output
    assert "not found" in result.output
    assert "Traceback" not in result.output


def test_top_tables_all_targets_survives_a_broken_duckdb(monkeypatch, tmp_path) -> None:
    """One bad DuckDB target must not stop the libpq ones from reporting.

    The DuckDB half runs for real (its file is missing, which is the failure
    under test); only the two servers are faked, since there is none to reach.
    """
    from dataplat.cli.db import top_tables as top_tables_mod

    real_collect = top_tables_mod._collect

    def collect(target, prefixes: list[str], limit: int):
        if target.engine is SqlEngine.duckdb:
            return real_collect(target, prefixes, limit)
        return _fake_collect(target, prefixes, limit)

    _duckdb_target(monkeypatch, tmp_path / "not-there.duckdb")
    runner = CliRunner()

    with patch("dataplat.cli.db.top_tables._collect", side_effect=collect):
        result = runner.invoke(db_app, ["top-tables", "-t", "all"])

    assert result.exit_code == 1
    assert "big_fact" in result.output  # the Postgres section still rendered
    assert "not found" in result.output


def test_top_tables_duckdb_without_the_driver_is_a_config_error(
    monkeypatch, tmp_path
) -> None:
    """duckdb is an optional extra, so a target may name an engine we cannot open.

    That is local configuration, not a database failure, and it must land as one
    target's error line — the same shape as an unreachable server — rather than
    taking the run down.
    """
    _duckdb_target(monkeypatch, _make_warehouse(tmp_path / "w.duckdb"))
    runner = CliRunner()

    def missing() -> None:
        raise ConfigError("A duckdb target needs the duckdb package")

    monkeypatch.setattr("dataplat.cli.db.top_tables.load_duckdb", missing)
    result = runner.invoke(db_app, ["top-tables", "-t", "demo_ddb"])

    assert result.exit_code == 1
    assert "needs the duckdb package" in result.output
    assert "Traceback" not in result.output
