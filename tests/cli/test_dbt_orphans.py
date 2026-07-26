from __future__ import annotations

import contextlib
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from dataplat.cli import _prompt
from dataplat.cli.db import dbt_orphans as do
from dataplat.cli.db.dbt_orphans import _parse_exclusions
from dataplat.core.errors import ValidationError
from dataplat.services.db.connection import SqlEngine
from dataplat.services.db.orphans import DEPRECATED_SUFFIX


def test_parse_exclusions_schema_only() -> None:
    schemas, relations = _parse_exclusions(["public"], None)
    assert schemas == frozenset({"public"})
    assert relations == frozenset()


def test_parse_exclusions_schema_dot_name() -> None:
    schemas, relations = _parse_exclusions(["public.foo"], None)
    assert schemas == frozenset()
    assert relations == frozenset({("public", "foo")})


def test_parse_exclusions_mixed_tokens() -> None:
    schemas, relations = _parse_exclusions(["public", "analytics.legacy"], None)
    assert schemas == frozenset({"public"})
    assert relations == frozenset({("analytics", "legacy")})


def test_parse_exclusions_rejects_multi_dot() -> None:
    with pytest.raises(ValidationError):
        _parse_exclusions(["a.b.c"], None)


def test_parse_exclusions_rejects_empty_token_after_strip() -> None:
    with pytest.raises(ValidationError):
        _parse_exclusions(["   "], None)


def test_parse_exclusions_strips_whitespace() -> None:
    schemas, relations = _parse_exclusions(["  public  "], None)
    assert schemas == frozenset({"public"})


def test_parse_exclusions_reads_file(tmp_path: Path) -> None:
    exclude_file = tmp_path / "excludes.txt"
    exclude_file.write_text("# comment line\npublic\n\nanalytics.legacy\n")
    schemas, relations = _parse_exclusions([], str(exclude_file))
    assert schemas == frozenset({"public"})
    assert relations == frozenset({("analytics", "legacy")})


def test_parse_exclusions_merges_cli_and_file(tmp_path: Path) -> None:
    exclude_file = tmp_path / "excludes.txt"
    exclude_file.write_text("analytics.legacy\n")
    schemas, relations = _parse_exclusions(["public"], str(exclude_file))
    assert schemas == frozenset({"public"})
    assert relations == frozenset({("analytics", "legacy")})


def test_parse_exclusions_missing_file_raises(tmp_path: Path) -> None:
    missing = tmp_path / "nope.txt"
    with pytest.raises(ValidationError):
        _parse_exclusions([], str(missing))


def test_parse_exclusions_comma_separated_schemas() -> None:
    schemas, relations = _parse_exclusions(["public,analytics"], None)
    assert schemas == frozenset({"public", "analytics"})
    assert relations == frozenset()


def test_parse_exclusions_comma_separated_mixed() -> None:
    schemas, relations = _parse_exclusions(["public,analytics.legacy", "scratch"], None)
    assert schemas == frozenset({"public", "scratch"})
    assert relations == frozenset({("analytics", "legacy")})


def test_parse_exclusions_comma_strips_whitespace() -> None:
    schemas, _ = _parse_exclusions(["  public , analytics  "], None)
    assert schemas == frozenset({"public", "analytics"})


def test_parse_exclusions_comma_rejects_empty_piece() -> None:
    with pytest.raises(ValidationError):
        _parse_exclusions(["public,,analytics"], None)


def test_parse_exclusions_comma_in_file(tmp_path: Path) -> None:
    exclude_file = tmp_path / "excludes.txt"
    exclude_file.write_text("public,analytics\n# skip me\nscratch.tmp\n")
    schemas, relations = _parse_exclusions([], str(exclude_file))
    assert schemas == frozenset({"public", "analytics"})
    assert relations == frozenset({("scratch", "tmp")})


# =========================================================================
# scan / revert / purge — the confirmation gate and markup safety
#
# These commands rename and drop warehouse objects, so the gate is covered
# on every path (dry-run, accepted, declined, non-interactive, --yes) and
# every echoed schema/relation name is checked with a value that both
# crashes Rich ([/x]) and would be silently eaten by it ([bold]).
# =========================================================================

runner = CliRunner()

SCHEMA = "ana[/x]lytics"
ORPHAN = "orders[bold]"
DEPRECATED = f"{ORPHAN}{DEPRECATED_SUFFIX}"
LABEL = "postgres"


class _Cursor:
    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class _Conn:
    def cursor(self) -> _Cursor:
        return _Cursor()


class _Stdin:
    """Stand-in for ``sys.stdin``: the gate only asks whether it is a TTY."""

    def __init__(self, tty: bool) -> None:
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


@pytest.fixture
def tty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the gate take its interactive branch.

    CliRunner replaces ``sys.stdin`` with a non-TTY pipe *inside* ``invoke``,
    so swapping the module-global ``sys`` is the only way to reach the prompt.
    """
    monkeypatch.setattr(_prompt, "sys", SimpleNamespace(stdin=_Stdin(True)))


@pytest.fixture
def no_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_prompt, "sys", SimpleNamespace(stdin=_Stdin(False)))


@pytest.fixture
def warehouse(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SimpleNamespace:
    """A one-orphan warehouse with every write recorded instead of executed."""
    state = SimpleNamespace(
        renamed=[],
        dropped=[],
        present={SCHEMA: {ORPHAN, "kept"}},
        log_dir=tmp_path / "logs",
    )
    state.log_dir.mkdir()
    # Never let a test write into the developer's real ~/.config.
    monkeypatch.setattr(do, "LOG_DIR", state.log_dir)
    monkeypatch.setattr(
        do,
        "_engines_for_target",
        lambda name: [(LABEL, SqlEngine.postgresql, "DEMO_PG")],
    )
    monkeypatch.setattr(
        do,
        "resolve_orphans_connection_params",
        lambda engine, *, env_prefix: object(),
    )

    @contextlib.contextmanager
    def _open(params: object, *, dry_run: bool) -> Iterator[_Conn]:
        yield _Conn()

    monkeypatch.setattr(do, "open_transactional_connection", _open)
    monkeypatch.setattr(do, "node_prefix", lambda: "model.demo.")
    monkeypatch.setattr(do, "invocation_command", lambda: None)
    monkeypatch.setattr(
        do,
        "fetch_live_model_relations",
        lambda cur, **kw: {SCHEMA: {"kept"}},
    )
    monkeypatch.setattr(
        do,
        "fetch_existing_relations",
        lambda cur, schemas, **kw: {SCHEMA: {ORPHAN, "kept"}},
    )
    monkeypatch.setattr(
        do,
        "fetch_deprecated_objects",
        lambda cur, **kw: [(SCHEMA, DEPRECATED, "table")],
    )

    def _classify(cur: object, schema: str, name: str, **kw: object) -> str | None:
        return "table" if name in state.present.get(schema, set()) else None

    def _rename(
        cur: object, schema: str, old: str, new: str, kind: str, **kw: object
    ) -> None:
        state.renamed.append((schema, old, new))

    def _drop(cur: object, schema: str, name: str, kind: str) -> None:
        state.dropped.append((schema, name))

    monkeypatch.setattr(do, "classify_object", _classify)
    monkeypatch.setattr(do, "rename_object", _rename)
    monkeypatch.setattr(do, "drop_object", _drop)
    return state


def _scan(args: list[str], **kwargs: Any) -> Any:
    return runner.invoke(do.app, args, **kwargs)


# --- scan: the gate ------------------------------------------------------


def test_scan_dry_run_is_the_default_and_writes_nothing(
    warehouse: SimpleNamespace, tmp_path: Path
) -> None:
    log = tmp_path / "scan.log.json"
    result = _scan(["--log", str(log)])
    assert result.exit_code == 0, result.output
    assert warehouse.renamed == []
    assert "Would rename" in result.output
    assert json.loads(log.read_text())["dry_run"] is True


def test_scan_dry_run_never_prompts(
    warehouse: SimpleNamespace, no_tty: None, tmp_path: Path
) -> None:
    result = _scan(["--log", str(tmp_path / "s.json")])
    assert result.exit_code == 0, result.output
    assert "--yes" not in result.output


def test_scan_confirmation_accepted_renames(
    warehouse: SimpleNamespace, tty: None, tmp_path: Path
) -> None:
    result = _scan(["--no-dry-run", "--log", str(tmp_path / "s.json")], input="y\n")
    assert result.exit_code == 0, result.output
    assert warehouse.renamed == [(SCHEMA, ORPHAN, DEPRECATED)]


def test_scan_confirmation_declined_writes_nothing(
    warehouse: SimpleNamespace, tty: None, tmp_path: Path
) -> None:
    log = tmp_path / "s.json"
    result = _scan(["--no-dry-run", "--log", str(log)], input="n\n")
    assert result.exit_code == 1
    assert warehouse.renamed == []
    assert not log.exists()  # the gate fires before any log is written
    assert "Aborted." in result.output


def test_scan_non_interactive_without_yes_names_the_flag(
    warehouse: SimpleNamespace, no_tty: None, tmp_path: Path
) -> None:
    log = tmp_path / "s.json"
    result = _scan(["--no-dry-run", "--log", str(log)])
    assert result.exit_code == 1
    assert warehouse.renamed == []
    assert not log.exists()
    assert "--yes" in result.output


def test_scan_yes_proceeds_without_prompting(
    warehouse: SimpleNamespace, no_tty: None, tmp_path: Path
) -> None:
    result = _scan(["--no-dry-run", "--yes", "--log", str(tmp_path / "s.json")])
    assert result.exit_code == 0, result.output
    assert warehouse.renamed == [(SCHEMA, ORPHAN, DEPRECATED)]


# --- scan: markup safety -------------------------------------------------


def test_scan_preview_shows_hostile_names_verbatim(
    warehouse: SimpleNamespace, tmp_path: Path
) -> None:
    """The regression: ``[/x]`` in a schema name used to raise MarkupError."""
    result = _scan(["--log", str(tmp_path / "s.json")])
    assert result.exit_code == 0, result.output
    assert f"{SCHEMA}.{ORPHAN} -> {SCHEMA}.{DEPRECATED}" in result.output
    assert "[bold]" in result.output  # not consumed as a style


def test_scan_summary_shows_hostile_schema_verbatim(
    warehouse: SimpleNamespace, tmp_path: Path
) -> None:
    result = _scan(["--log", str(tmp_path / "s.json")])
    assert f"1 in {SCHEMA}" in result.output


def test_scan_skips_when_target_name_already_taken(
    warehouse: SimpleNamespace, tmp_path: Path
) -> None:
    warehouse.present[SCHEMA] = {ORPHAN, DEPRECATED}
    result = _scan(["--log", str(tmp_path / "s.json")])
    assert result.exit_code == 0, result.output
    assert warehouse.renamed == []
    assert f"{SCHEMA}.{DEPRECATED} already exists" in result.output


def test_scan_skips_vanished_object(warehouse: SimpleNamespace, tmp_path: Path) -> None:
    warehouse.present[SCHEMA] = set()
    result = _scan(["--log", str(tmp_path / "s.json")])
    assert result.exit_code == 0, result.output
    assert f"{SCHEMA}.{ORPHAN} no longer present" in result.output


def test_scan_echoes_hostile_exclusion_token_verbatim(
    warehouse: SimpleNamespace,
) -> None:
    result = _scan(["--exclude", "a.b[/x].c"])
    assert result.exit_code == 1
    assert "a.b[/x].c" in result.output


def test_scan_rejects_zero_window(warehouse: SimpleNamespace) -> None:
    result = _scan(["--window-days", "0"])
    assert result.exit_code == 1
    assert "--window-days must be >= 1" in result.output


# --- purge: the gate ----------------------------------------------------


def test_purge_dry_run_is_the_default_and_drops_nothing(
    warehouse: SimpleNamespace, tmp_path: Path
) -> None:
    log = tmp_path / "p.json"
    result = _scan(["purge", "--log", str(log)])
    assert result.exit_code == 0, result.output
    assert warehouse.dropped == []
    assert "Would drop" in result.output
    assert json.loads(log.read_text())["dry_run"] is True


def test_purge_confirmation_accepted_drops(
    warehouse: SimpleNamespace, tty: None, tmp_path: Path
) -> None:
    result = _scan(
        ["purge", "--no-dry-run", "--log", str(tmp_path / "p.json")],
        input="y\n",
    )
    assert result.exit_code == 0, result.output
    assert warehouse.dropped == [(SCHEMA, DEPRECATED)]


def test_purge_confirmation_declined_drops_nothing(
    warehouse: SimpleNamespace, tty: None, tmp_path: Path
) -> None:
    log = tmp_path / "p.json"
    result = _scan(["purge", "--no-dry-run", "--log", str(log)], input="n\n")
    assert result.exit_code == 1
    assert warehouse.dropped == []
    assert not log.exists()
    assert "Aborted." in result.output


def test_purge_confirmation_says_it_cannot_be_undone(
    warehouse: SimpleNamespace, tty: None, tmp_path: Path
) -> None:
    result = _scan(
        ["purge", "--no-dry-run", "--log", str(tmp_path / "p.json")],
        input="n\n",
    )
    assert "cannot be undone" in result.output


def test_purge_non_interactive_without_yes_names_the_flag(
    warehouse: SimpleNamespace, no_tty: None, tmp_path: Path
) -> None:
    log = tmp_path / "p.json"
    result = _scan(["purge", "--no-dry-run", "--log", str(log)])
    assert result.exit_code == 1
    assert warehouse.dropped == []
    assert not log.exists()
    assert "--yes" in result.output


def test_purge_yes_proceeds_without_prompting(
    warehouse: SimpleNamespace, no_tty: None, tmp_path: Path
) -> None:
    result = _scan(
        ["purge", "--no-dry-run", "--yes", "--log", str(tmp_path / "p.json")]
    )
    assert result.exit_code == 0, result.output
    assert warehouse.dropped == [(SCHEMA, DEPRECATED)]


# --- purge: markup safety and the age filter ----------------------------


def test_purge_preview_shows_hostile_names_verbatim(
    warehouse: SimpleNamespace, tmp_path: Path
) -> None:
    result = _scan(["purge", "--log", str(tmp_path / "p.json")])
    assert result.exit_code == 0, result.output
    assert f"{SCHEMA}.{DEPRECATED}" in result.output
    assert "[bold]" in result.output


def test_purge_excludes_hostile_relation_token(
    warehouse: SimpleNamespace, tmp_path: Path
) -> None:
    result = _scan(
        [
            "purge",
            "--log",
            str(tmp_path / "p.json"),
            "--exclude",
            f"{SCHEMA}.{DEPRECATED}",
        ]
    )
    assert result.exit_code == 0, result.output
    assert warehouse.dropped == []
    assert "0 deprecated object(s)" in result.output


def test_purge_older_than_skips_objects_with_no_recorded_rename(
    warehouse: SimpleNamespace, no_tty: None, tmp_path: Path
) -> None:
    result = _scan(
        [
            "purge",
            "--no-dry-run",
            "--yes",
            "--older-than",
            "7",
            "--log",
            str(tmp_path / "p.json"),
        ]
    )
    assert result.exit_code == 0, result.output
    assert warehouse.dropped == []
    assert f"{SCHEMA}.{DEPRECATED}: no recorded rename" in result.output


def test_purge_older_than_include_unknown_drops(
    warehouse: SimpleNamespace, no_tty: None, tmp_path: Path
) -> None:
    result = _scan(
        [
            "purge",
            "--no-dry-run",
            "--yes",
            "--older-than",
            "7",
            "--include-unknown",
            "--log",
            str(tmp_path / "p.json"),
        ]
    )
    assert result.exit_code == 0, result.output
    assert warehouse.dropped == [(SCHEMA, DEPRECATED)]


def _write_apply_log(warehouse: SimpleNamespace, *, age_days: int) -> None:
    when = datetime.now(UTC) - timedelta(days=age_days)
    path = warehouse.log_dir / f"{do.APPLY_LOG_PREFIX}-20240101T000000Z.log.json"
    path.write_text(
        json.dumps(
            {
                "generated_at": when.isoformat(),
                "dry_run": False,
                "source": "dbt-orphans",
                "renames": [
                    {
                        "database": LABEL,
                        "schema": SCHEMA,
                        "old_name": ORPHAN,
                        "new_name": DEPRECATED,
                        "kind": "table",
                    }
                ],
            }
        )
    )


def test_purge_older_than_respects_the_grace_period(
    warehouse: SimpleNamespace, no_tty: None, tmp_path: Path
) -> None:
    _write_apply_log(warehouse, age_days=1)
    result = _scan(
        [
            "purge",
            "--no-dry-run",
            "--yes",
            "--older-than",
            "7",
            "--log",
            str(tmp_path / "p.json"),
        ]
    )
    assert result.exit_code == 0, result.output
    assert warehouse.dropped == []
    assert "inside the grace period" in result.output
    assert f"{SCHEMA}.{DEPRECATED}" in result.output


def test_purge_older_than_drops_once_past_the_grace_period(
    warehouse: SimpleNamespace, no_tty: None, tmp_path: Path
) -> None:
    _write_apply_log(warehouse, age_days=30)
    result = _scan(
        [
            "purge",
            "--no-dry-run",
            "--yes",
            "--older-than",
            "7",
            "--log",
            str(tmp_path / "p.json"),
        ]
    )
    assert result.exit_code == 0, result.output
    assert warehouse.dropped == [(SCHEMA, DEPRECATED)]


# --- revert -------------------------------------------------------------


def _revert_log(tmp_path: Path) -> Path:
    path = tmp_path / "revert-me.log.json"
    path.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(UTC).isoformat(),
                "dry_run": False,
                "source": "dbt-orphans",
                "renames": [
                    {
                        "database": LABEL,
                        "schema": SCHEMA,
                        "old_name": ORPHAN,
                        "new_name": DEPRECATED,
                        "kind": "table",
                    }
                ],
            }
        )
    )
    return path


def test_revert_dry_run_is_the_default_and_writes_nothing(
    warehouse: SimpleNamespace, tmp_path: Path
) -> None:
    warehouse.present[SCHEMA] = {DEPRECATED}
    result = _scan(["revert", "--log", str(_revert_log(tmp_path))])
    assert result.exit_code == 0, result.output
    assert warehouse.renamed == []
    assert "Would revert" in result.output


def test_revert_no_dry_run_renames_back(
    warehouse: SimpleNamespace, tmp_path: Path
) -> None:
    warehouse.present[SCHEMA] = {DEPRECATED}
    result = _scan(["revert", "--no-dry-run", "--log", str(_revert_log(tmp_path))])
    assert result.exit_code == 0, result.output
    assert warehouse.renamed == [(SCHEMA, DEPRECATED, ORPHAN)]


def test_revert_shows_hostile_names_verbatim(
    warehouse: SimpleNamespace, tmp_path: Path
) -> None:
    warehouse.present[SCHEMA] = {DEPRECATED}
    result = _scan(["revert", "--log", str(_revert_log(tmp_path))])
    assert f"{SCHEMA}.{DEPRECATED} -> {SCHEMA}.{ORPHAN}" in result.output
    assert "[bold]" in result.output


def test_revert_refuses_when_the_original_name_is_taken(
    warehouse: SimpleNamespace, tmp_path: Path
) -> None:
    warehouse.present[SCHEMA] = {DEPRECATED, ORPHAN}
    result = _scan(["revert", "--log", str(_revert_log(tmp_path))])
    assert result.exit_code == 0, result.output
    assert warehouse.renamed == []
    assert f"cannot revert {SCHEMA}.{DEPRECATED}" in result.output


def test_revert_skips_object_that_is_gone(
    warehouse: SimpleNamespace, tmp_path: Path
) -> None:
    warehouse.present[SCHEMA] = set()
    result = _scan(["revert", "--log", str(_revert_log(tmp_path))])
    assert result.exit_code == 0, result.output
    assert f"{SCHEMA}.{DEPRECATED} not found" in result.output


def test_revert_reports_malformed_entry_verbatim(
    warehouse: SimpleNamespace, tmp_path: Path
) -> None:
    path = tmp_path / "bad-entry.log.json"
    path.write_text(
        json.dumps(
            {
                "dry_run": False,
                "renames": [{"database": LABEL, "schema": SCHEMA}],
            }
        )
    )
    result = _scan(["revert", "--log", str(path)])
    assert result.exit_code == 0, result.output
    assert "Skipping malformed log entry" in result.output
    assert SCHEMA in result.output


def test_revert_missing_log_path_is_escaped(
    warehouse: SimpleNamespace, tmp_path: Path
) -> None:
    missing = tmp_path / "no[bold]such.log.json"
    result = _scan(["revert", "--log", str(missing)])
    assert result.exit_code == 1
    assert f"log file not found: {missing}" in result.output


def _hostile_log_path(tmp_path: Path) -> Path:
    """A log path whose *string* carries both Rich failure modes.

    ``[/x]`` is a stray closing tag: unescaped it raises MarkupError, so the
    error message never reaches the user. ``[bold]`` is a real style name:
    unescaped it is silently eaten and the path is misreported. A ``/`` cannot
    live inside one filename, so ``[/x]`` has to straddle a directory
    boundary — hence the extra parent, which is created here.
    """
    parent = tmp_path / "bro["
    parent.mkdir()
    return parent / "x]ken[bold].log.json"


def test_revert_unreadable_log_error_is_escaped(
    warehouse: SimpleNamespace, tmp_path: Path
) -> None:
    path = _hostile_log_path(tmp_path)
    path.write_text("{not json")
    result = _scan(["revert", "--log", str(path)])
    assert result.exit_code == 1
    assert f"could not read log {path}" in result.output
    assert "[bold]" in result.output  # not consumed as a style


def test_revert_unreadable_log_reports_the_os_error_escaped(
    warehouse: SimpleNamespace, tmp_path: Path
) -> None:
    """The OSError half of the same handler needs escaping too.

    A directory where the log should be makes ``open`` raise
    IsADirectoryError, whose text quotes the hostile path back at us. That is
    the only place the exception itself carries markup on that line, so this
    is what proves the exception is escaped and not just the path.
    """
    path = _hostile_log_path(tmp_path)
    path.mkdir()
    result = _scan(["revert", "--log", str(path)])
    assert result.exit_code == 1
    # Once as the log path we were given, once inside the OS error text.
    assert result.output.count(str(path)) == 2


def test_revert_without_any_log_reports_the_log_dir(
    warehouse: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The advertised directory is interpolated into markup as well, so point
    # it at a hostile path. It is never created: an absent directory globs to
    # nothing, which is exactly the "no log found" case under test.
    log_dir = tmp_path / "lo[" / "x]gs[bold]"
    monkeypatch.setattr(do, "LOG_DIR", log_dir)
    result = _scan(["revert"])
    assert result.exit_code == 1
    assert str(log_dir) in result.output


def test_revert_warns_when_the_log_was_a_dry_run(
    warehouse: SimpleNamespace, tmp_path: Path
) -> None:
    path = tmp_path / "dry.log.json"
    path.write_text(json.dumps({"dry_run": True, "renames": []}))
    result = _scan(["revert", "--log", str(path)])
    assert result.exit_code == 0, result.output
    assert "generated in dry-run mode" in result.output


# --- the [engine] line prefix -------------------------------------------


def test_engine_tag_is_printed_and_not_parsed_as_a_style(
    warehouse: SimpleNamespace, tmp_path: Path
) -> None:
    """``[postgres]`` is a well-formed Rich tag; unescaped it vanished."""
    result = _scan(["--log", str(tmp_path / "s.json")])
    assert result.exit_code == 0, result.output
    assert f"[{LABEL}]" in result.output


def test_engine_tag_survives_on_purge_and_revert(
    warehouse: SimpleNamespace, tmp_path: Path
) -> None:
    purge = _scan(["purge", "--log", str(tmp_path / "p.json")])
    assert f"[{LABEL}]" in purge.output
    warehouse.present[SCHEMA] = {DEPRECATED}
    revert = _scan(["revert", "--log", str(_revert_log(tmp_path))])
    assert f"[{LABEL}]" in revert.output


# --- audit-log discovery -------------------------------------------------


def test_logs_are_found_in_a_directory_containing_brackets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A bracket in the log directory is a glob character class, not a literal.

    Unescaped it matched nothing, so ``revert`` reported no history and
    ``purge --older-than`` saw every object as having no recorded rename — for
    anyone whose path happens to contain brackets.
    """
    log_dir = tmp_path / "lo[" / "x]gs[bold]"
    log_dir.mkdir(parents=True)
    written = log_dir / "dbt_orphans-20240101T000000Z.log.json"
    written.write_text(json.dumps({"dry_run": False, "renames": []}))
    monkeypatch.setattr(do, "LOG_DIR", log_dir)
    monkeypatch.setattr(do, "LEGACY_LOG_DIR", tmp_path / "absent")

    assert do._matching_logs("dbt_orphans") == [str(written)]
    assert do._find_latest_log("dbt_orphans") == str(written)


def test_log_discovery_still_globs_the_timestamp(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Escaping the directory must not escape the wildcard after the prefix."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    older = log_dir / "dbt_orphans-20240101T000000Z.log.json"
    newer = log_dir / "dbt_orphans-20240202T000000Z.log.json"
    for path in (older, newer):
        path.write_text("{}")
    (log_dir / "other-20240303T000000Z.log.json").write_text("{}")
    monkeypatch.setattr(do, "LOG_DIR", log_dir)
    monkeypatch.setattr(do, "LEGACY_LOG_DIR", tmp_path / "absent")

    assert do._matching_logs("dbt_orphans") == [str(older), str(newer)]
    assert do._find_latest_log("dbt_orphans") == str(newer)
