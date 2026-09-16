"""`dp dbt orphans` — manifest-based produced-set and partition awareness.

Focused sibling to tests/cli/test_dbt_orphans.py (same split rationale as
test_dbt_orphans_alias.py / test_dbt_orphans_project.py): this file is only
about the new behaviour ``_run_for_engine`` gained by consulting
``dataplat.services.dbt.manifest`` -- sparing a name the manifest still
produces, sparing a partition child of a produced parent, and refusing
outright when the manifest cannot be trusted (missing, or reporting zero
produced relations).

Each test builds its own throwaway ``DbtProject`` under ``tmp_path`` and
hands it to ``_run_for_engine`` directly by monkeypatching
``_engines_for_project`` -- the same technique
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


def _project(tmp_path: Path, *, nodes: dict[str, dict[str, Any]] | None) -> DbtProject:
    """A throwaway ``DbtProject`` whose manifest is exactly what the test wants.

    ``nodes=None`` means no target/manifest.json at all -- never compiled --
    which is a different fact than a manifest that exists and produces
    nothing (``nodes={}``); tests distinguish the two on purpose.
    """
    project_dir = tmp_path / "manifest_project"
    project_dir.mkdir()
    (project_dir / "dbt_project.yml").write_text("name: manifest_demo\n")
    if nodes is not None:
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
    assert "manifest_demo" in result.output
    assert "zero produced relations" in result.output


def test_scan_refuses_when_manifest_is_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    project = _project(tmp_path, nodes=None)
    _wire_engine(monkeypatch, project)
    monkeypatch.setattr(do, "open_transactional_connection", _forbid_open)

    result = _scan(tmp_path)

    assert result.exit_code == ExitCode.CONFIG, result.output
    assert "No manifest.json" in result.output


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
