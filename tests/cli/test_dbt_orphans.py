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
from dataplat.cli.dbt import orphans as do
from dataplat.cli.dbt.orphans import _parse_exclusions
from dataplat.core.errors import ConfigError, ExitCode, ValidationError
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
# A target name, not an engine family: this is what production actually
# produces as the identity threaded through the summary, the audit log's
# `database` field, and revert's log filter (see _engines_for_project's
# docstring). It used to be "postgres" here, which no real invocation can
# write anymore -- a label the fixture alone could still produce hid the
# Critical where revert's post-fix filter could no longer match any log
# written before that fix (see _LEGACY_ENGINE_LABELS for the backward
# compatibility that now covers that case for real logs).
LABEL = "demo_pg"


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
        "_engines_for_project",
        lambda project, target: [(LABEL, SqlEngine.postgresql, "DEMO_PG", None)],
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
    monkeypatch.setattr(do, "node_prefix", lambda project=None: "model.demo.")
    monkeypatch.setattr(do, "invocation_command", lambda project=None: None)
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
    # A malformed --exclude is a ValidationError, so it exits 2 like every other
    # rejected argument -- Click's own usage errors included.
    assert result.exit_code == ExitCode.INVALID_INPUT
    assert "a.b[/x].c" in result.output


def test_scan_rejects_zero_window(warehouse: SimpleNamespace) -> None:
    result = _scan(["--window-days", "0"])
    assert result.exit_code == 1
    assert "--window-days must be >= 1" in result.output


def test_scan_unknown_target_exits_invalid_input(tmp_path: Path) -> None:
    """No `warehouse` fixture: that one stubs out project/target resolution
    entirely. ``nope`` isn't declared by the default project (``demo_project``),
    so it is rejected the same way a target outside the project always is.
    """
    result = _scan(["--target", "nope", "--log", str(tmp_path / "s.json")])
    assert result.exit_code == ExitCode.INVALID_INPUT
    assert "does not build into" in result.output


def test_engines_for_project_falls_back_to_legacy_targets_when_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Critical regression: DP_DBT_PROJECTS unset must not make this command
    refuse to run. An installation that has never adopted named projects has
    to keep working exactly as it always did -- resolving DP_TARGETS
    directly, with project=None riding along so the legacy env-var readers in
    dataplat.services.dbt.settings take over.

    tests/conftest.py sets DP_DBT_PROJECTS for the whole suite, so it has to
    be explicitly unset here -- otherwise this test exercises the named-
    project path, not the legacy fallback, and would pass for the wrong
    reason. That gap is exactly how this regression shipped once already:
    every test in this file goes through the `warehouse` fixture, which
    stubs `_engines_for_project` outright and so never touches either branch
    for real.
    """
    monkeypatch.delenv("DP_DBT_PROJECTS", raising=False)
    monkeypatch.delenv("DP_DBT_DEFAULT_PROJECT", raising=False)

    engines = do._engines_for_project(None, None)

    assert {(label, proj) for label, _engine, _env_prefix, proj in engines} == {
        ("demo_pg", None),
        ("demo_pg2", None),
        ("demo_rs", None),
    }


def test_engines_for_project_with_explicit_project_ignores_the_legacy_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The legacy fallback only fires when no ``--project`` was given at all.
    An operator naming a project explicitly while DP_DBT_PROJECTS happens to
    be unset asked for that project by name -- silently resolving every
    DP_TARGETS target instead would scan warehouses that project may never
    touch, which is a worse failure mode than the honest error below.
    """
    monkeypatch.delenv("DP_DBT_PROJECTS", raising=False)
    monkeypatch.delenv("DP_DBT_DEFAULT_PROJECT", raising=False)

    result = _scan(["--project", "demo_project"])

    assert result.exit_code == ExitCode.INVALID_INPUT
    assert "No dbt projects configured" in result.output


def test_scan_config_error_from_project_resolution_exits_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``default_project_name()`` can raise ``ConfigError`` (a
    DP_DBT_DEFAULT_PROJECT that names a project DP_DBT_PROJECTS does not
    declare), not just ``ValidationError`` -- the gate has to catch
    ``DataplatError``, or this escapes as a raw traceback instead of the
    documented exit 3. No `warehouse` fixture: this fails during resolution,
    before any connection is even attempted.
    """
    monkeypatch.setenv("DP_DBT_DEFAULT_PROJECT", "not_a_real_project")

    result = _scan(["--log", str(tmp_path / "s.json")])

    assert result.exit_code == ExitCode.CONFIG, result.output
    assert "not_a_real_project" in result.output


def test_scan_resolves_a_real_named_project(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Every other test in this file goes through the ``warehouse`` fixture,
    which stubs ``_engines_for_project`` to always hand back ``project=None``
    -- the legacy-fallback shape, not what a real ``DP_DBT_PROJECTS``
    installation (conftest's own demo_project/demo_other) actually produces.
    That gap is exactly how the legacy-fallback regression above once passed
    a green suite: every test here exercised the one path production could no
    longer reach. This one runs the real resolution --
    ``_engines_for_project``, ``node_prefix``, ``invocation_command``,
    ``excluded_schemas`` all resolve against the genuine ``demo_project``
    fixture -- and only stubs the warehouse layer below it (connection,
    cursor, catalog queries).
    """
    state = SimpleNamespace(present={SCHEMA: {ORPHAN, "kept"}})
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    monkeypatch.setattr(do, "LOG_DIR", log_dir)
    monkeypatch.setattr(
        do,
        "resolve_orphans_connection_params",
        lambda engine, *, env_prefix: object(),
    )

    @contextlib.contextmanager
    def _open(params: object, *, dry_run: bool) -> Iterator[_Conn]:
        yield _Conn()

    monkeypatch.setattr(do, "open_transactional_connection", _open)
    monkeypatch.setattr(
        do, "fetch_live_model_relations", lambda cur, **kw: {SCHEMA: {"kept"}}
    )
    monkeypatch.setattr(
        do,
        "fetch_existing_relations",
        lambda cur, schemas, **kw: {SCHEMA: {ORPHAN, "kept"}},
    )

    def _classify(cur: object, schema: str, name: str, **kw: object) -> str | None:
        return "table" if name in state.present.get(schema, set()) else None

    monkeypatch.setattr(do, "classify_object", _classify)

    result = _scan(
        ["-p", "demo_project", "-t", "demo_pg", "--log", str(tmp_path / "s.json")]
    )

    assert result.exit_code == 0, result.output
    # The identity threaded through the summary and the log is the target's
    # own name, not its engine family -- "demo_pg", not "postgres" -- so two
    # same-engine targets in one fan-out can never be confused with each
    # other (see _engines_for_project's docstring).
    assert "[demo_pg]" in result.output
    logged = json.loads((tmp_path / "s.json").read_text())
    assert logged["dry_run"] is True
    assert logged["renames"][0]["database"] == "demo_pg"


def test_summary_distinguishes_two_same_engine_targets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Important Finding 4's motivating case: two targets that share an
    engine (demo_pg, demo_pg2 -- both postgresql, see tests/conftest.py) must
    produce two distinguishable summary blocks, not two identical-looking
    ``[postgres]`` ones. Before the fix, the label threaded through
    ``_print_summary`` was ``_ENGINE_LABELS[tgt.engine]`` -- the engine
    family, not the target -- so this exact scenario would have printed the
    same tag twice with no way to tell which block belonged to which cluster.
    """
    monkeypatch.setenv("DP_TARGETS", "demo_pg,demo_pg2,demo_rs")
    monkeypatch.setenv("DEMO_PROJECT_DBT_TARGETS", "demo_pg,demo_pg2")
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    monkeypatch.setattr(do, "LOG_DIR", log_dir)
    monkeypatch.setattr(
        do,
        "resolve_orphans_connection_params",
        lambda engine, *, env_prefix: object(),
    )

    @contextlib.contextmanager
    def _open(params: object, *, dry_run: bool) -> Iterator[_Conn]:
        yield _Conn()

    monkeypatch.setattr(do, "open_transactional_connection", _open)
    monkeypatch.setattr(
        do, "fetch_live_model_relations", lambda cur, **kw: {SCHEMA: {"kept"}}
    )
    monkeypatch.setattr(
        do,
        "fetch_existing_relations",
        lambda cur, schemas, **kw: {SCHEMA: {ORPHAN, "kept"}},
    )
    monkeypatch.setattr(do, "classify_object", lambda cur, schema, name, **kw: "table")

    result = _scan(["-p", "demo_project", "--log", str(tmp_path / "s.json")])

    assert result.exit_code == 0, result.output
    assert "[demo_pg]" in result.output
    assert "[demo_pg2]" in result.output
    # Two distinct summary blocks, not the same tag printed twice.
    assert result.output.count("live dbt models") == 2


def test_revert_filters_log_entries_by_target_not_by_engine(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The data-correctness case Important Finding 4 exists to prevent: two
    targets sharing an engine (demo_pg, demo_pg2) must never have their audit
    log entries cross-applied during revert. Before the fix, the audit log's
    ``database`` field held the *engine* label, so demo_pg's and demo_pg2's
    entries would have been indistinguishable from each other's -- revert
    could restore one cluster's objects using the other cluster's history.

    Each ``rename_object`` call here records which target's connection was
    open when it happened (via the stubbed ``resolve_orphans_connection_params``
    / ``open_transactional_connection``, which thread the env_prefix through
    as a stand-in for "params"), so cross-contamination would show up as a
    rename recorded under the wrong target's connection, not merely as an
    entry being skipped.
    """
    monkeypatch.setenv("DP_TARGETS", "demo_pg,demo_pg2,demo_rs")
    monkeypatch.setenv("DEMO_PROJECT_DBT_TARGETS", "demo_pg,demo_pg2")

    log = tmp_path / "revert.log.json"
    log.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(UTC).isoformat(),
                "dry_run": False,
                "source": "dbt-orphans",
                "renames": [
                    {
                        "database": "demo_pg",
                        "schema": SCHEMA,
                        "old_name": "pg_orphan",
                        "new_name": f"pg_orphan{DEPRECATED_SUFFIX}",
                        "kind": "table",
                    },
                    {
                        "database": "demo_pg2",
                        "schema": SCHEMA,
                        "old_name": "pg2_orphan",
                        "new_name": f"pg2_orphan{DEPRECATED_SUFFIX}",
                        "kind": "table",
                    },
                ],
            }
        )
    )

    present = {f"pg_orphan{DEPRECATED_SUFFIX}", f"pg2_orphan{DEPRECATED_SUFFIX}"}
    monkeypatch.setattr(
        do,
        "resolve_orphans_connection_params",
        lambda engine, *, env_prefix: env_prefix,
    )

    open_connections: list[str] = []

    @contextlib.contextmanager
    def _open(params: object, *, dry_run: bool) -> Iterator[_Conn]:
        open_connections.append(str(params))
        yield _Conn()

    monkeypatch.setattr(do, "open_transactional_connection", _open)
    monkeypatch.setattr(
        do,
        "classify_object",
        lambda cur, schema, name, **kw: "table" if name in present else None,
    )

    reverted: list[tuple[str, str, str]] = []

    def _rename(
        cur: object, schema: str, old: str, new: str, kind: str, **kw: object
    ) -> None:
        reverted.append((open_connections[-1], old, new))

    monkeypatch.setattr(do, "rename_object", _rename)

    result = _scan(["revert", "-p", "demo_project", "--no-dry-run", "--log", str(log)])

    assert result.exit_code == 0, result.output
    assert reverted == [
        ("DEMO_PG", f"pg_orphan{DEPRECATED_SUFFIX}", "pg_orphan"),
        ("DEMO_PG2", f"pg2_orphan{DEPRECATED_SUFFIX}", "pg2_orphan"),
    ]


def test_scan_unset_dbt_project_exits_config(
    warehouse: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An unset DP_DBT_PROJECT is the operator's to fix, so it exits 3, not 5.

    The per-engine step re-raises it with an ``[<engine>]`` label, which used to
    widen it to ServiceError on the way. Under the new codes that would have
    told a CI job the *warehouse* had failed — an error it would retry forever,
    when nothing about retrying can set an environment variable.
    """
    monkeypatch.setattr(
        do,
        "node_prefix",
        lambda project=None: (_ for _ in ()).throw(ConfigError("DP_DBT_PROJECT")),
    )
    log = tmp_path / "s.json"

    result = _scan(["--log", str(log)])

    assert result.exit_code == ExitCode.CONFIG
    assert f"[{LABEL}] DP_DBT_PROJECT" in result.output
    # The partial log still lands, and still says so after the error.
    assert json.loads(log.read_text())["renames"] == []
    assert "Partial audit log written" in result.output


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


def test_purge_older_than_recognizes_pre_upgrade_apply_logs(
    warehouse: SimpleNamespace, no_tty: None, tmp_path: Path
) -> None:
    """The rename-age index has the same legacy-value problem revert's log
    filter does: an apply log written before target-name identity existed
    keys its entries on the engine family ("postgres"), not a target name.
    ``--older-than`` has to keep recognizing it as a recorded rename, or
    every pre-upgrade rename looks unrecorded and is skipped (silently,
    without --include-unknown) instead of purged once its grace period has
    actually passed. See _LEGACY_ENGINE_LABELS.
    """
    when = datetime.now(UTC) - timedelta(days=30)
    path = warehouse.log_dir / f"{do.APPLY_LOG_PREFIX}-20240101T000000Z.log.json"
    path.write_text(
        json.dumps(
            {
                "generated_at": when.isoformat(),
                "dry_run": False,
                "source": "dbt-orphans",
                "renames": [
                    {
                        # The legacy value: what an apply log written before
                        # this migration actually contains.
                        "database": "postgres",
                        "schema": SCHEMA,
                        "old_name": ORPHAN,
                        "new_name": DEPRECATED,
                        "kind": "table",
                    }
                ],
            }
        )
    )

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
    assert "no recorded rename" not in result.output


def test_purge_refuses_ambiguous_legacy_rename_age_across_same_engine_targets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The variant that matters, applied to the rename-age index: an old
    apply log's "postgres"-labeled entry cannot be safely attributed to
    demo_pg's grace period vs demo_pg2's -- that information was never
    recorded. Purging across both (unscoped) with --older-than must refuse
    the whole invocation rather than apply the recorded age to whichever
    target happens to have a matching deprecated object, which risks
    purging one target's object on the strength of another's grace period.
    See _ambiguous_legacy_conflicts.
    """
    monkeypatch.setenv("DP_TARGETS", "demo_pg,demo_pg2,demo_rs")
    monkeypatch.setenv("DEMO_PROJECT_DBT_TARGETS", "demo_pg,demo_pg2")

    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    monkeypatch.setattr(do, "LOG_DIR", log_dir)
    when = datetime.now(UTC) - timedelta(days=30)
    apply_log = log_dir / f"{do.APPLY_LOG_PREFIX}-20240101T000000Z.log.json"
    apply_log.write_text(
        json.dumps(
            {
                "generated_at": when.isoformat(),
                "dry_run": False,
                "source": "dbt-orphans",
                "renames": [
                    {
                        "database": "postgres",
                        "schema": SCHEMA,
                        "old_name": ORPHAN,
                        "new_name": DEPRECATED,
                        "kind": "table",
                    }
                ],
            }
        )
    )

    def _forbid_open(*args: object, **kwargs: object) -> None:
        raise AssertionError("a refused invocation must not open a connection")

    monkeypatch.setattr(do, "open_transactional_connection", _forbid_open)

    result = _scan(
        [
            "purge",
            "-p",
            "demo_project",
            "--no-dry-run",
            "--yes",
            "--older-than",
            "7",
            "--log",
            str(tmp_path / "p.json"),
        ]
    )

    assert result.exit_code == ExitCode.INVALID_INPUT, result.output
    assert "postgres" in result.output
    assert "demo_pg" in result.output
    assert "demo_pg2" in result.output


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


def test_revert_still_works_against_a_log_written_before_this_migration(
    warehouse: SimpleNamespace, tmp_path: Path
) -> None:
    """A log written before target-name identity existed carries the legacy
    engine-family value ("postgres"/"redshift") as `database`, not a target
    name. Revert has to keep matching it: refusing to recognize the legacy
    value at all would make revert silently no-op every pre-upgrade rename
    while still reporting success -- and purge, which scans the catalog
    rather than the log, would then permanently drop objects revert claimed
    to have already restored. See _LEGACY_ENGINE_LABELS.

    This is the unambiguous case the fallback exists for: exactly one
    Postgres target (the `warehouse` fixture's single stubbed engine) is in
    play, so the legacy value can only mean that target. See the sibling
    test below for what happens when it cannot.
    """
    warehouse.present[SCHEMA] = {DEPRECATED}
    path = tmp_path / "legacy.log.json"
    path.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(UTC).isoformat(),
                "dry_run": False,
                "source": "dbt-orphans",
                "renames": [
                    {
                        # The legacy value: what a log written before this
                        # migration actually contains, not a target name.
                        "database": "postgres",
                        "schema": SCHEMA,
                        "old_name": ORPHAN,
                        "new_name": DEPRECATED,
                        "kind": "table",
                    }
                ],
            }
        )
    )

    result = _scan(["revert", "--no-dry-run", "--log", str(path)])

    assert result.exit_code == 0, result.output
    assert warehouse.renamed == [(SCHEMA, DEPRECATED, ORPHAN)]


def test_revert_refuses_ambiguous_legacy_entries_across_same_engine_targets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The variant that matters: an old-format log where *both* entries say
    "database": "postgres" cannot be safely attributed to demo_pg vs
    demo_pg2 -- that information was never recorded. Reverting across both
    (unscoped) must refuse the whole invocation rather than apply every
    "postgres" entry to both targets, which would be exactly the
    cross-application Important Finding 4 fixed, reintroduced for legacy
    logs instead of new ones. See _ambiguous_legacy_conflicts.
    """
    monkeypatch.setenv("DP_TARGETS", "demo_pg,demo_pg2,demo_rs")
    monkeypatch.setenv("DEMO_PROJECT_DBT_TARGETS", "demo_pg,demo_pg2")

    log = tmp_path / "legacy.log.json"
    log.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(UTC).isoformat(),
                "dry_run": False,
                "source": "dbt-orphans",
                "renames": [
                    {
                        "database": "postgres",
                        "schema": SCHEMA,
                        "old_name": "pg_orphan",
                        "new_name": f"pg_orphan{DEPRECATED_SUFFIX}",
                        "kind": "table",
                    },
                    {
                        "database": "postgres",
                        "schema": SCHEMA,
                        "old_name": "pg2_orphan",
                        "new_name": f"pg2_orphan{DEPRECATED_SUFFIX}",
                        "kind": "table",
                    },
                ],
            }
        )
    )

    def _forbid_open(*args: object, **kwargs: object) -> None:
        raise AssertionError("a refused invocation must not open a connection")

    monkeypatch.setattr(do, "open_transactional_connection", _forbid_open)

    result = _scan(["revert", "-p", "demo_project", "--no-dry-run", "--log", str(log)])

    assert result.exit_code == ExitCode.INVALID_INPUT, result.output
    assert "postgres" in result.output
    assert "demo_pg" in result.output
    assert "demo_pg2" in result.output


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


# --- a DuckDB target: the mechanism itself is refused ----------------------
#
# Against a real DuckDB database, not a stub, and with the drivers booby-trapped
# so that a refusal arriving after a connection fails the test. This command
# renames and drops, so "before any work" is the whole point: the file is also
# compared byte for byte afterwards.
#
# Note these tests do *not* use the `warehouse` fixture, which stubs out
# `_engines_for_project` — the very function that refuses.


def _flat(text: str) -> str:
    """One long line: Rich wraps at the terminal width, assertions are wording."""
    return " ".join(text.split())


def _duckdb_target(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A real DuckDB database with a view over a table — the dbt shape.

    The view is what makes the refusal concrete rather than theoretical: renaming
    `orders` here raises DependencyException on duckdb 1.5.5, and there is no
    CASCADE. In a dbt project this is the ordinary case, not a corner.
    """
    import duckdb

    path = tmp_path / "warehouse.duckdb"
    connection = duckdb.connect(str(path))
    connection.execute("CREATE SCHEMA analytics")
    connection.execute("CREATE TABLE analytics.orders(id INTEGER)")
    connection.execute(
        "CREATE VIEW analytics.v_orders AS SELECT * FROM analytics.orders"
    )
    connection.close()
    monkeypatch.setenv("DP_TARGETS", "ddb")
    monkeypatch.setenv("DDB_ENGINE", "duckdb")
    monkeypatch.setenv("DDB_PATH", str(path))
    monkeypatch.delenv("DP_DEFAULT_TARGET", raising=False)
    # The default project (demo_project) must declare ddb as one of its own
    # targets, or `projects_and_targets` rejects `--target ddb` with "does not
    # build into" before the capability gate this test is actually exercising
    # is ever reached.
    monkeypatch.setenv("DEMO_PROJECT_DBT_TARGETS", "ddb")
    return path


def _forbid_connections(monkeypatch: pytest.MonkeyPatch) -> None:
    import duckdb
    import psycopg

    def _forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("a refused command opened a connection")

    monkeypatch.setattr(psycopg, "connect", _forbidden)
    monkeypatch.setattr(duckdb, "connect", _forbidden)


def _assert_rename_refusal(result: Any) -> None:
    out = _flat(result.output)
    assert result.exit_code == ExitCode.INVALID_INPUT, result.output
    assert "[ddb] dp dbt orphans cannot run against DuckDB" in out
    # The engine fact...
    assert "ALTER TABLE ... RENAME TO fails with a DependencyException" in out
    assert "no CASCADE" in out
    # ...and why *this* command needs it, which the fact alone does not say.
    assert "quarantines an orphan by renaming it" in out
    assert "a view on a model is the normal case" in out
    assert "That is what DuckDB is, not a missing dataplat feature" in out
    for wording in ("not supported", "not implemented"):
        assert wording not in out.lower()


def test_scan_refuses_a_duckdb_target(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = _duckdb_target(monkeypatch, tmp_path)
    before = path.read_bytes()
    _forbid_connections(monkeypatch)
    log = tmp_path / "s.log.json"

    result = _scan(["--target", "ddb", "--log", str(log)])

    _assert_rename_refusal(result)
    # Nothing was renamed, and no audit log claims anything was.
    assert not log.exists()
    assert path.read_bytes() == before


def test_scan_refuses_a_duckdb_target_before_the_gate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, no_tty: None
) -> None:
    """--no-dry-run must not prompt for renames that cannot happen."""
    _duckdb_target(monkeypatch, tmp_path)
    _forbid_connections(monkeypatch)

    result = _scan(
        ["--target", "ddb", "--no-dry-run", "--log", str(tmp_path / "s.json")]
    )

    _assert_rename_refusal(result)
    assert "Rename every orphaned dbt object" not in _flat(result.output)


def test_purge_refuses_a_duckdb_target_before_the_gate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, no_tty: None
) -> None:
    """The destructive half: refused before the prompt and before any DROP."""
    path = _duckdb_target(monkeypatch, tmp_path)
    before = path.read_bytes()
    _forbid_connections(monkeypatch)
    log = tmp_path / "p.log.json"

    result = _scan(["purge", "--target", "ddb", "--no-dry-run", "--log", str(log)])

    _assert_rename_refusal(result)
    assert "Permanently DROP" not in _flat(result.output)
    assert not log.exists()
    assert path.read_bytes() == before


def test_revert_refuses_a_duckdb_target_before_looking_for_a_log(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """ "No log found" would send the reader after a file that is not the problem.

    Revert renames too, so the engine refuses it whether or not a previous run
    left a log behind — and the log is deliberately absent here, which is the
    error this used to report instead.
    """
    _duckdb_target(monkeypatch, tmp_path)
    _forbid_connections(monkeypatch)
    monkeypatch.setattr(do, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(do, "LEGACY_LOG_DIR", tmp_path / "absent")

    result = _scan(["revert", "--target", "ddb"])

    _assert_rename_refusal(result)
    assert "no dbt_orphans log found" not in result.output


def test_all_refuses_when_one_target_cannot_rename(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Every declared target is the default (no ``--target``), and this command
    renames and drops.

    Deliberately not per-target degradation: a partly-applied destructive run is
    the outcome worth avoiding most, and the refusal names the target so the
    operator can ask for the rest explicitly.
    """
    _duckdb_target(monkeypatch, tmp_path)
    monkeypatch.setenv("DP_TARGETS", "demo_pg,ddb")
    monkeypatch.setenv("DEMO_PROJECT_DBT_TARGETS", "demo_pg,ddb")
    _forbid_connections(monkeypatch)

    result = _scan(["--log", str(tmp_path / "s.json")])

    _assert_rename_refusal(result)


def test_all_refuses_across_a_genuinely_multi_project_fan_out(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The all-or-nothing refusal proven again across *projects*, not just
    across targets within one project (see
    ``test_all_refuses_when_one_target_cannot_rename`` above): demo_project's
    own targets are fine, but demo_other now declares a DuckDB target that
    cannot rename-with-dependents, and the whole ``-p all`` invocation must
    refuse before any connection is opened for *any* project -- including
    demo_project's, which would otherwise have worked.

    demo_project (demo_pg, demo_rs) and demo_other (ddb) declare disjoint
    targets here, deliberately -- overlapping targets are a different refusal
    (see test_dbt_orphans_project.py's
    test_orphans_refuses_projects_that_share_a_target), and this test would
    hit that one first instead of the capability gate it means to exercise.
    """
    _duckdb_target(monkeypatch, tmp_path)
    monkeypatch.setenv("DP_TARGETS", "demo_pg,demo_rs,ddb")
    monkeypatch.setenv("DEMO_PROJECT_DBT_TARGETS", "demo_pg,demo_rs")
    monkeypatch.setenv("DEMO_OTHER_DBT_TARGETS", "ddb")
    _forbid_connections(monkeypatch)

    result = _scan(["-p", "all", "--log", str(tmp_path / "s.json")])

    _assert_rename_refusal(result)


def test_all_fan_out_reaches_each_project_with_its_own_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The green case the two refusal tests above never exercise: a
    genuinely multi-project ``-p all`` fan-out that *succeeds*, and reaches
    each project's own targets with that project's own node prefix and
    exclusions -- the entire reason ``_engines_for_project`` carries the
    resolved project alongside each target rather than resolving one
    globally for the whole run (see its docstring).

    demo_project (demo_pg) and demo_other (demo_pg2) declare disjoint
    targets here, deliberately, so this hits neither overlap refusal
    (test_dbt_orphans_project.py) nor the capability refusal above -- this
    test is only about whether the right settings reach the right target.
    """
    monkeypatch.setenv("DEMO_PROJECT_DBT_TARGETS", "demo_pg")
    monkeypatch.setenv("DEMO_OTHER_DBT_TARGETS", "demo_pg2")
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    monkeypatch.setattr(do, "LOG_DIR", log_dir)
    monkeypatch.setattr(
        do,
        "resolve_orphans_connection_params",
        lambda engine, *, env_prefix: object(),
    )

    @contextlib.contextmanager
    def _open(params: object, *, dry_run: bool) -> Iterator[_Conn]:
        yield _Conn()

    monkeypatch.setattr(do, "open_transactional_connection", _open)

    seen_node_prefixes: list[str] = []

    def _fetch_live(
        cur: object, *, invocation_command: object, node_prefix: str, **kw: object
    ) -> dict[str, set[str]]:
        seen_node_prefixes.append(node_prefix)
        return {SCHEMA: {"kept"}}

    monkeypatch.setattr(do, "fetch_live_model_relations", _fetch_live)
    monkeypatch.setattr(
        do, "fetch_existing_relations", lambda cur, schemas, **kw: {SCHEMA: {"kept"}}
    )

    result = _scan(["-p", "all", "--log", str(tmp_path / "s.json")])

    assert result.exit_code == 0, result.output
    assert "[demo_pg]" in result.output
    assert "[demo_pg2]" in result.output
    # demo_project's own dbt_project.yml names it demo_project_from_yml;
    # demo_other is named via DEMO_OTHER_DBT_NAME=explicit_name (conftest).
    # Each target got its own project's node prefix, not one shared value.
    assert sorted(seen_node_prefixes) == [
        "model.demo_project_from_yml.",
        "model.explicit_name.",
    ]
