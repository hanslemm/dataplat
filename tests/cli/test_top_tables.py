from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

from typer.testing import CliRunner

from dataplat.cli.db import app as db_app
from dataplat.cli.db.top_tables import _split_prefixes
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
