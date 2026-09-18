"""`dp dbt orphans` — manifest-based produced-set and partition awareness.

Focused sibling to tests/cli/test_dbt_orphans.py (same split rationale as
test_dbt_orphans_alias.py / test_dbt_orphans_project.py): this file is only
about the new behaviour the command gained by consulting
``dataplat.services.dbt.manifest`` -- sparing a name the manifest still
produces, sparing a partition child of a produced parent, and refusing
outright when the manifest cannot be trusted (missing, or reporting zero
produced relations). The manifest is read once per project by
``_preflight_produced`` before any target is touched; ``_run_for_engine``
(and, through it, ``diff_orphans``) only *consults* the resulting produced
set, via the ``produced`` parameter -- it does not read the manifest itself.

Each test builds its own throwaway ``DbtProject`` under ``tmp_path`` and
runs it through the full CLI entry point (``main``, via ``_scan``) by
monkeypatching ``_engines_for_project`` -- the same technique
tests/services/dbt/test_manifest.py uses to avoid writing into the tracked
demo_project/demo_other fixtures, applied here without needing the full
DP_DBT_PROJECTS env-var registry at all, since ``_produced_relations`` only
ever reads ``project.path``.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from dataplat.cli.dbt import orphans as do
from dataplat.core.errors import ExitCode
from dataplat.services.db.connection import SqlEngine
from dataplat.services.dbt.projects import DbtProject

runner = CliRunner()

SCHEMA = "analytics"
LABEL = "demo_pg"


class _Cursor:
    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class _Conn:
    def cursor(self) -> _Cursor:
        return _Cursor()


def _node(name: str) -> dict[str, Any]:
    return {"name": name, "resource_type": "model"}


def _project(
    tmp_path: Path,
    *,
    nodes: dict[str, dict[str, Any]] | None,
    raw_manifest_text: str | None = None,
) -> DbtProject:
    """A throwaway ``DbtProject`` whose manifest is exactly what the test wants.

    ``nodes=None`` means no target/manifest.json at all -- never compiled --
    which is a different fact than a manifest that exists and produces
    nothing (``nodes={}``); tests distinguish the two on purpose.

    ``raw_manifest_text``, when given, is written verbatim as
    ``target/manifest.json`` instead of ``{"nodes": nodes}`` -- for a test
    that needs manifest content ``relation_names`` cannot even parse into
    the normal shape (e.g. valid JSON that is not an object at all).
    ``nodes`` is ignored in that case.
    """
    project_dir = tmp_path / "manifest_project"
    project_dir.mkdir()
    (project_dir / "dbt_project.yml").write_text("name: manifest_demo\n")
    if raw_manifest_text is not None:
        target_dir = project_dir / "target"
        target_dir.mkdir()
        (target_dir / "manifest.json").write_text(raw_manifest_text)
    elif nodes is not None:
        target_dir = project_dir / "target"
        target_dir.mkdir()
        (target_dir / "manifest.json").write_text(json.dumps({"nodes": nodes}))
    return DbtProject(
        name="manifest_demo",
        env_prefix="DEMO_PG",
        path=project_dir,
        profiles_dir=project_dir,
        project_name="manifest_demo",
        target_names=("demo_pg",),
    )


def _wire_engine(monkeypatch: pytest.MonkeyPatch, project: DbtProject | None) -> None:
    monkeypatch.setattr(
        do,
        "_engines_for_project",
        lambda _project, _target: [(LABEL, SqlEngine.postgresql, "DEMO_PG", project)],
    )
    monkeypatch.setattr(
        do, "resolve_orphans_connection_params", lambda engine, *, env_prefix: object()
    )
    monkeypatch.setattr(do, "node_prefix", lambda project=None: "model.demo.")
    monkeypatch.setattr(do, "invocation_command", lambda project=None: None)
    monkeypatch.setattr(do, "excluded_schemas", lambda project=None: frozenset())


def _forbid_open(*args: object, **kwargs: object) -> None:
    raise AssertionError("a refused invocation must not open a connection")


@contextlib.contextmanager
def _open(params: object, *, dry_run: bool) -> Iterator[_Conn]:
    yield _Conn()


def _scan(tmp_path: Path, args: list[str] | None = None) -> Any:
    return runner.invoke(do.app, [*(args or []), "--log", str(tmp_path / "s.json")])


def _flat(text: str) -> str:
    """One long line: Rich wraps at the terminal width, assertions aren't.

    Load-bearing for the manifest-refusal messages below: they interpolate
    ``tmp_path``, whose length varies with how many tests a given pytest
    session has already run, so the exact wrap point (and thus whether a
    phrase like "zero produced relations" straddles a newline) is not
    stable across runs. See tests/cli/test_dbt_orphans.py's own ``_flat``.
    """
    return " ".join(text.split())


def test_scan_spares_a_name_the_manifest_still_produces(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A relation absent from the recent-build-results table (`live`) but
    still claimed by the manifest is not a real orphan -- it is just a model
    that has not rebuilt inside the window, and the manifest is the
    authoritative source for what the project currently produces.
    """
    project = _project(tmp_path, nodes={"model.demo.a": _node("stale_but_produced")})
    _wire_engine(monkeypatch, project)
    monkeypatch.setattr(do, "open_transactional_connection", _open)
    monkeypatch.setattr(
        do, "fetch_live_model_relations", lambda cur, **kw: {SCHEMA: set()}
    )
    monkeypatch.setattr(
        do,
        "fetch_existing_relations",
        lambda cur, schemas, **kw: {SCHEMA: {"stale_but_produced"}},
    )
    monkeypatch.setattr(do, "classify_object", lambda cur, schema, name, **kw: "table")

    result = _scan(tmp_path)

    assert result.exit_code == 0, result.output
    assert "Processed 0 object(s)" in result.output


def test_scan_spares_a_partition_child_of_a_produced_parent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    project = _project(tmp_path, nodes={"model.demo.a": _node("fct_events")})
    _wire_engine(monkeypatch, project)
    monkeypatch.setattr(do, "open_transactional_connection", _open)
    monkeypatch.setattr(
        do, "fetch_live_model_relations", lambda cur, **kw: {SCHEMA: set()}
    )
    monkeypatch.setattr(
        do,
        "fetch_existing_relations",
        lambda cur, schemas, **kw: {SCHEMA: {"fct_events_p_trello"}},
    )
    monkeypatch.setattr(do, "classify_object", lambda cur, schema, name, **kw: "table")

    result = _scan(tmp_path)

    assert result.exit_code == 0, result.output
    assert "Processed 0 object(s)" in result.output


def test_scan_still_flags_a_genuine_orphan(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Manifest awareness narrows false positives; it must not stop catching
    real ones -- a name unrelated to anything the manifest produces, and not
    shaped like a partition of anything it produces, is still an orphan.
    """
    project = _project(tmp_path, nodes={"model.demo.a": _node("fct_events")})
    _wire_engine(monkeypatch, project)
    monkeypatch.setattr(do, "open_transactional_connection", _open)
    monkeypatch.setattr(
        do, "fetch_live_model_relations", lambda cur, **kw: {SCHEMA: set()}
    )
    monkeypatch.setattr(
        do,
        "fetch_existing_relations",
        lambda cur, schemas, **kw: {SCHEMA: {"truly_orphaned"}},
    )
    # Only the pre-existing name classifies as present -- classify_object
    # returning "table" unconditionally would make the *_deprecated target
    # name look already taken, and _rename_orphan would then (correctly, for
    # that state) skip the rename, defeating the point of this test.
    monkeypatch.setattr(
        do,
        "classify_object",
        lambda cur, schema, name, **kw: "table" if name == "truly_orphaned" else None,
    )

    result = _scan(tmp_path)

    assert result.exit_code == 0, result.output
    assert "Processed 1 object(s)" in result.output
    assert "Would rename table analytics.truly_orphaned" in result.output


def test_scan_produced_membership_is_case_insensitive(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """relation_names lowercases everything it returns; a warehouse catalog
    can report a quoted, mixed-case name, so the comparison has to lowercase
    that side too or the manifest match silently stops working.
    """
    project = _project(tmp_path, nodes={"model.demo.a": _node("orders")})
    _wire_engine(monkeypatch, project)
    monkeypatch.setattr(do, "open_transactional_connection", _open)
    monkeypatch.setattr(
        do, "fetch_live_model_relations", lambda cur, **kw: {SCHEMA: set()}
    )
    monkeypatch.setattr(
        do,
        "fetch_existing_relations",
        lambda cur, schemas, **kw: {SCHEMA: {"Orders"}},
    )
    monkeypatch.setattr(do, "classify_object", lambda cur, schema, name, **kw: "table")

    result = _scan(tmp_path)

    assert result.exit_code == 0, result.output
    assert "Processed 0 object(s)" in result.output


def test_scan_refuses_when_manifest_produces_zero_relations(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An empty produced set is always wrong to run destructively against: a
    genuinely empty project has no warehouse tables to scan in the first
    place, so refuse rather than treat every existing object as an orphan.
    """
    project = _project(tmp_path, nodes={})
    _wire_engine(monkeypatch, project)
    monkeypatch.setattr(do, "open_transactional_connection", _forbid_open)

    result = _scan(tmp_path)

    assert result.exit_code == ExitCode.CONFIG, result.output
    out = _flat(result.output)
    assert "manifest_demo" in out
    assert "zero produced relations" in out


def test_scan_refuses_when_manifest_is_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    project = _project(tmp_path, nodes=None)
    _wire_engine(monkeypatch, project)
    monkeypatch.setattr(do, "open_transactional_connection", _forbid_open)

    result = _scan(tmp_path)

    assert result.exit_code == ExitCode.CONFIG, result.output
    assert "No manifest.json" in _flat(result.output)


def _named_project(
    base: Path,
    name: str,
    env_prefix: str,
    *,
    nodes: dict[str, dict[str, Any]] | None,
) -> DbtProject:
    """Like ``_project``, but with a distinct name/env_prefix per call.

    ``_project`` hardcodes ``name="manifest_demo"``, which is fine for every
    other test in this file (one project at a time) but wrong for a fan-out
    test: the preflight below dedupes manifest reads by project *name*, so
    two same-named ``DbtProject`` objects would collapse into one read and
    the test could not tell whether the second project's manifest was ever
    consulted at all.
    """
    project_dir = base / name
    project_dir.mkdir()
    (project_dir / "dbt_project.yml").write_text(f"name: {name}\n")
    if nodes is not None:
        target_dir = project_dir / "target"
        target_dir.mkdir()
        (target_dir / "manifest.json").write_text(json.dumps({"nodes": nodes}))
    return DbtProject(
        name=name,
        env_prefix=env_prefix,
        path=project_dir,
        profiles_dir=project_dir,
        project_name=name,
        target_names=(env_prefix.lower(),),
    )


def test_apply_preflights_every_project_manifest_before_confirm_or_work(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The ordering hazard a whole-branch review caught: under
    ``--project all --no-dry-run``, the manifest refusal used to run
    per-target inside ``_run_for_engine``, so a bad manifest on a *later*
    project was only discovered after an *earlier* project's renames had
    already been applied -- and the confirmation had already been answered
    for nothing.

    Two projects here, good manifest first and bad (missing) manifest
    second -- the worst ordering, since a per-target check would have let
    the first project's renames through before ever reaching the second.
    Both ``open_transactional_connection`` and ``confirm_or_exit`` are made
    to raise if reached at all, so this proves the refusal happens before
    *either* project is touched and before the operator is even asked to
    confirm -- not merely that it happens "early enough".
    """
    good = _named_project(
        tmp_path, "good_project", "DEMO_PG", nodes={"model.demo.a": _node("kept")}
    )
    bad = _named_project(tmp_path, "bad_project", "DEMO_PG2", nodes=None)

    monkeypatch.setattr(
        do,
        "_engines_for_project",
        lambda _project, _target: [
            ("demo_pg", SqlEngine.postgresql, "DEMO_PG", good),
            ("demo_pg2", SqlEngine.postgresql, "DEMO_PG2", bad),
        ],
    )
    monkeypatch.setattr(
        do, "resolve_orphans_connection_params", lambda engine, *, env_prefix: object()
    )
    monkeypatch.setattr(do, "node_prefix", lambda project=None: "model.demo.")
    monkeypatch.setattr(do, "invocation_command", lambda project=None: None)
    monkeypatch.setattr(do, "excluded_schemas", lambda project=None: frozenset())
    monkeypatch.setattr(do, "open_transactional_connection", _forbid_open)

    def _forbid_confirm(*args: object, **kwargs: object) -> None:
        raise AssertionError(
            "the manifest refusal must fire before the operator is asked to confirm"
        )

    monkeypatch.setattr(do, "confirm_or_exit", _forbid_confirm)

    result = _scan(tmp_path, ["-p", "all", "--no-dry-run"])

    assert result.exit_code == ExitCode.CONFIG, result.output
    assert "No manifest.json" in _flat(result.output)

    log_path = tmp_path / "s.json"
    assert log_path.exists(), "a preflight refusal must still write the audit log"
    payload = json.loads(log_path.read_text())
    assert payload["renames"] == []


def test_legacy_project_none_never_reads_a_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The legacy, pre-named-project path (``project=None``) has never had a
    manifest to read. It must keep working exactly as it always did -- not
    gain a new refusal, and not even attempt to call into
    dataplat.services.dbt.manifest, which ``_produced_relations`` guards by
    returning ``None`` outright for a ``None`` project.
    """

    def _forbid_relation_names(*args: object, **kwargs: object) -> None:
        raise AssertionError("the legacy path must never read a manifest")

    monkeypatch.setattr(do, "relation_names", _forbid_relation_names)
    _wire_engine(monkeypatch, None)
    monkeypatch.setattr(do, "open_transactional_connection", _open)
    monkeypatch.setattr(
        do, "fetch_live_model_relations", lambda cur, **kw: {SCHEMA: set()}
    )
    monkeypatch.setattr(
        do,
        "fetch_existing_relations",
        lambda cur, schemas, **kw: {SCHEMA: {"an_orphan"}},
    )
    monkeypatch.setattr(
        do,
        "classify_object",
        lambda cur, schema, name, **kw: "table" if name == "an_orphan" else None,
    )

    result = _scan(tmp_path)

    assert result.exit_code == 0, result.output
    assert "Processed 1 object(s)" in result.output


def test_scan_refuses_cleanly_on_a_manifest_that_is_not_a_json_object(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A manifest.json that parses as valid JSON but isn't an object at the
    top level (a hostile or corrupted file, not merely a missing one) must
    refuse cleanly -- exit CONFIG with a message, not an uncaught
    AttributeError past the command's own DataplatError handler.

    The audit log surviving is the part that actually matters: under
    --project all --no-dry-run, an earlier target may already have renamed
    objects before this one refuses, and `main`'s `except DataplatError`
    handler is what writes the (partial) log recording those renames. A raw
    traceback here would skip that handler entirely and lose the log,
    making already-applied renames unrecoverable.
    """
    project = _project(tmp_path, nodes=None, raw_manifest_text=json.dumps([1, 2, 3]))
    _wire_engine(monkeypatch, project)
    monkeypatch.setattr(do, "open_transactional_connection", _forbid_open)

    result = _scan(tmp_path)

    assert result.exit_code == ExitCode.CONFIG, result.output
    assert "does not contain a JSON object" in _flat(result.output)

    log_path = tmp_path / "s.json"
    assert log_path.exists(), "the audit log must survive a manifest-shape refusal"
    payload = json.loads(log_path.read_text())
    assert payload["renames"] == []


def test_scan_selectively_spares_produced_and_partition_but_flags_the_orphan(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """All three outcomes in one existing set, proving they don't interfere
    with each other: a name the manifest produces directly is spared, a
    partition child of a produced parent is spared, and a name unrelated to
    either is still renamed. The three earlier tests each prove one outcome
    in isolation; this is the one that pins the selectivity itself.
    """
    project = _project(tmp_path, nodes={"model.demo.a": _node("fct_events")})
    _wire_engine(monkeypatch, project)
    monkeypatch.setattr(do, "open_transactional_connection", _open)
    monkeypatch.setattr(
        do, "fetch_live_model_relations", lambda cur, **kw: {SCHEMA: set()}
    )
    monkeypatch.setattr(
        do,
        "fetch_existing_relations",
        lambda cur, schemas, **kw: {
            SCHEMA: {"fct_events", "fct_events_p_trello", "truly_orphaned"}
        },
    )
    monkeypatch.setattr(
        do,
        "classify_object",
        lambda cur, schema, name, **kw: "table" if name == "truly_orphaned" else None,
    )

    result = _scan(tmp_path)

    assert result.exit_code == 0, result.output
    assert "Processed 1 object(s)" in result.output
    assert result.output.count("Would rename") == 1
    assert "Would rename table analytics.truly_orphaned" in result.output


def test_scan_still_renames_a_partition_shaped_name_with_no_produced_parent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A name that merely looks like a partition child (contains the ``_p_``
    marker) but whose supposed parent the manifest does not produce is not
    spared -- it is an orphan like any other. Pins this end-to-end, through
    _run_for_engine and diff_orphans, rather than only at
    is_partition_of's own unit level
    (test_partition_without_a_live_parent_is_not_spared in
    tests/services/dbt/test_manifest.py) -- the guard against ever
    shortcutting this to "contains the marker means spare".
    """
    project = _project(tmp_path, nodes={"model.demo.a": _node("fct_events")})
    _wire_engine(monkeypatch, project)
    monkeypatch.setattr(do, "open_transactional_connection", _open)
    monkeypatch.setattr(
        do, "fetch_live_model_relations", lambda cur, **kw: {SCHEMA: set()}
    )
    monkeypatch.setattr(
        do,
        "fetch_existing_relations",
        lambda cur, schemas, **kw: {SCHEMA: {"fct_gone_p_trello"}},
    )
    monkeypatch.setattr(
        do,
        "classify_object",
        lambda cur, schema, name, **kw: (
            "table" if name == "fct_gone_p_trello" else None
        ),
    )

    result = _scan(tmp_path)

    assert result.exit_code == 0, result.output
    assert "Processed 1 object(s)" in result.output
    assert "Would rename table analytics.fct_gone_p_trello" in result.output
