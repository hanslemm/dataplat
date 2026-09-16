from __future__ import annotations

import importlib.machinery
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from dataplat.core.errors import ValidationError
from dataplat.main import app

runner = CliRunner()


def test_dbt_area_is_mounted() -> None:
    result = runner.invoke(app, ["dbt", "--help"])
    assert result.exit_code == 0
    assert "dbt project" in result.output.lower()


def test_project_fans_out_over_its_declared_targets() -> None:
    from dataplat.cli.dbt._common import projects_and_targets

    pairs = projects_and_targets("demo_project", None)
    assert [p.name for p, _ in pairs] == ["demo_project"]
    assert [t.name for t in pairs[0][1]] == ["demo_pg", "demo_rs"]


def test_target_narrows_the_fan_out() -> None:
    from dataplat.cli.dbt._common import projects_and_targets

    pairs = projects_and_targets("demo_project", "demo_rs")
    assert [t.name for t in pairs[0][1]] == ["demo_rs"]


def test_target_outside_the_project_is_rejected() -> None:
    from dataplat.cli.dbt._common import projects_and_targets

    with pytest.raises(ValidationError, match="does not build into"):
        projects_and_targets("demo_other", "demo_pg")


def test_project_with_no_declared_targets_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataplat.cli.dbt._common import projects_and_targets

    monkeypatch.delenv("DEMO_OTHER_DBT_TARGETS", raising=False)
    with pytest.raises(ValidationError, match="DEMO_OTHER_DBT_TARGETS"):
        projects_and_targets("demo_other", None)


def test_no_project_given_and_none_configured_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``--project``-omitted, nothing-configured branch: every other test
    here passes an explicit project name, so this wiring -- and its specific
    message -- was otherwise untested at the CLI layer (default_project_name()
    itself is covered at the service layer)."""
    from dataplat.cli.dbt._common import projects_and_targets

    monkeypatch.delenv("DP_DBT_PROJECTS", raising=False)
    monkeypatch.delenv("DP_DBT_DEFAULT_PROJECT", raising=False)

    with pytest.raises(
        ValidationError, match="No dbt project given and none configured"
    ):
        projects_and_targets(None, None)


def test_no_project_given_falls_back_to_the_configured_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The path most people hit: omitting ``--project`` with
    ``DP_DBT_DEFAULT_PROJECT`` explicitly set resolves to that project, not
    just to the first name in ``DP_DBT_PROJECTS``."""
    from dataplat.cli.dbt._common import projects_and_targets

    monkeypatch.setenv("DP_DBT_DEFAULT_PROJECT", "demo_other")
    monkeypatch.setenv("DEMO_OTHER_DBT_TARGETS", "demo_rs")

    pairs = projects_and_targets(None, None)

    assert [p.name for p, _ in pairs] == ["demo_other"]


def test_all_fans_out_each_project_with_its_own_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``projects_and_targets("all", None)`` keeps each project's targets its
    own rather than merging them -- resolve_projects("all") is covered at the
    service layer, but the pairing done here is not."""
    from dataplat.cli.dbt._common import projects_and_targets

    monkeypatch.setenv("DEMO_OTHER_DBT_TARGETS", "demo_rs")

    pairs = projects_and_targets("all", None)

    assert [p.name for p, _ in pairs] == ["demo_project", "demo_other"]
    by_name = {p.name: [t.name for t in targets] for p, targets in pairs}
    assert by_name == {
        "demo_project": ["demo_pg", "demo_rs"],
        "demo_other": ["demo_rs"],
    }


def test_doctor_flags_legacy_vars_without_projects(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`dp dbt` needs named projects; doctor should nudge, not fail, when only
    the legacy single-project variables are set. This is rendered as a warning
    ``CheckResult`` from ``_offline_checks`` (see dataplat/cli/config.py) so it
    is tallied like every other check rather than printed out of band."""
    from tests.cli.test_config import (
        _configure_everything,
        _isolate_config_link,
        _pin_envrc,
    )

    _isolate_config_link(monkeypatch, tmp_path)
    envrc = tmp_path / ".envrc"
    envrc.write_text("export A=1")
    _pin_envrc(monkeypatch, envrc)
    _configure_everything(monkeypatch)
    monkeypatch.delenv("DP_DBT_PROJECTS", raising=False)
    monkeypatch.setenv("DP_DBT_PROJECT", "legacy")

    result = runner.invoke(app, ["config", "doctor"])

    # warn must never flip doctor's exit code -- that is a documented contract.
    assert result.exit_code == 0, result.output
    assert "All checks passed" in result.output
    assert "1 warning(s)" in result.output
    assert "DP_DBT_PROJECTS" in result.output


class _NoSpec:
    """A well-behaved meta_path finder: ``find_spec`` returns ``None``,
    matching what a real ``PathFinder`` answers when a module genuinely is
    not installed. ``dataplat.core.deps``'s readiness checks rely on exactly
    that -- ``find_spec(m) is None`` -- so a finder that *raised* instead
    would misrepresent "not installed" as "the import system is broken" and
    make ``area_ready()`` raise too, defeating the guard it is meant to
    prove works.
    """

    def find_spec(self, fullname: str, path: object, target: object = None) -> None:
        return None


def test_component_vars_skips_dbt_projects_without_pyyaml(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``services/dbt/projects.py`` imports PyYAML at module scope, and
    PyYAML ships only under the ``dbt`` extra. ``component_vars()`` must not
    raise for someone who has, say, only ``dataplat[db]`` installed and no
    PyYAML -- it should simply omit the ``"dbt project: ..."`` rows (see the
    ``area_ready("dbt")`` guard in ``dataplat/cli/config.py``).

    This hides PyYAML for real rather than asserting the guard merely
    exists. Returning ``None`` from one finder is not enough by itself --
    Python keeps walking ``sys.meta_path`` until a finder returns a real
    spec, and the standard ``PathFinder`` would still find the genuinely
    installed package on disk -- so ``PathFinder`` itself has to come out of
    the list for the duration of the test.
    """
    # Warm the caches for everything component_vars() needs on the path
    # other than the guarded one, so removing PathFinder below cannot
    # accidentally break some unrelated, not-yet-cached import.
    import dataplat.core.deps  # noqa: F401
    import dataplat.services.db.targets  # noqa: F401
    from dataplat.cli import config as config_cli

    monkeypatch.setattr(
        sys,
        "meta_path",
        [f for f in sys.meta_path if f is not importlib.machinery.PathFinder]
        + [_NoSpec()],
    )
    monkeypatch.delitem(sys.modules, "yaml", raising=False)

    components = config_cli.component_vars()

    assert not any(name.startswith("dbt project:") for name in components)


def test_doctor_offline_checks_do_not_touch_pyyaml(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The legacy-variable warning in ``_offline_checks`` (see
    ``dataplat/cli/config.py``) reads only environment variables -- it must
    not import ``services.dbt.projects`` (and so PyYAML) at all, unlike
    ``component_vars()``, which needed an explicit guard. Same blocking
    technique as ``test_component_vars_skips_dbt_projects_without_pyyaml``;
    see that test's docstring for why PathFinder has to be removed rather
    than merely shadowed."""
    import dataplat.core.deps  # noqa: F401
    import dataplat.services.db.targets  # noqa: F401
    from dataplat.cli import config as config_cli

    monkeypatch.setattr(
        sys,
        "meta_path",
        [f for f in sys.meta_path if f is not importlib.machinery.PathFinder]
        + [_NoSpec()],
    )
    monkeypatch.delitem(sys.modules, "yaml", raising=False)
    monkeypatch.delenv("DP_DBT_PROJECTS", raising=False)
    monkeypatch.setenv("DP_DBT_PROJECT", "legacy")

    results = config_cli._offline_checks()

    assert any(r.label == "dbt legacy vars" for r in results)


_BLOCK_YAML_AND_IMPORT_DB_SCRIPT = """
import sys
import importlib.machinery
import importlib.util


class _BlockYaml:
    '''Delegates to the real PathFinder for every module except yaml, which
    it reports as genuinely absent -- see the test docstring for why the
    blanket "remove PathFinder" technique used elsewhere in this file
    (_NoSpec) does not fit here.'''

    def find_spec(self, fullname, path, target=None):
        if fullname == "yaml" or fullname.startswith("yaml."):
            return None
        return importlib.machinery.PathFinder.find_spec(fullname, path, target)


sys.meta_path = [
    f for f in sys.meta_path if f is not importlib.machinery.PathFinder
] + [_BlockYaml()]

# Canary: prove the block is real (yaml reported genuinely absent), not a
# no-op that would let this script "pass" for the wrong reason.
assert importlib.util.find_spec("yaml") is None, "canary failed: yaml still resolvable"

import dataplat.cli.db as db_area

assert db_area.app.info.name == "db"
assert "yaml" not in sys.modules, "yaml ended up imported anyway"
print("OK")
"""


def test_db_area_imports_without_pyyaml() -> None:
    """``dataplat/cli/db/__init__.py`` imports ``dataplat.cli.dbt.orphans``
    unconditionally (the ``dp db dbt-orphans`` backward-compat mount), which
    imports ``dataplat.cli.dbt._common`` -> ``dataplat.services.dbt.projects``.
    PyYAML ships only under the ``dbt`` extra -- ``db``'s own contract in
    ``dataplat.core.deps.AREAS`` lists only ``psycopg`` -- so a
    ``dataplat[db]``-only install has to be able to import the whole ``db``
    area, and so run any ``dp db`` subcommand at all, without PyYAML
    installed.

    Before ``import yaml`` moved out of ``projects.py``'s module scope and
    into ``_project_name()`` (the only function that touches it), this failed
    with ``ModuleNotFoundError: No module named 'yaml'`` the instant any
    ``dp db`` subcommand was invoked in a fresh process -- for every user,
    whether or not they ever touch orphans or named projects.

    This runs in a fresh subprocess rather than blocking yaml in-process:
    dataplat.cli.db has to be imported genuinely fresh to prove anything
    (an already-cached import proves nothing), and an in-process attempt at
    that -- deleting dataplat.cli.db and its whole yaml-adjacent chain from
    sys.modules, forcing a real reimport under a yaml-blocking meta_path
    finder, then trying to restore every affected sys.modules entry and
    parent-package attribute afterwards -- reliably left stale module
    references behind for *other* tests later in the same session (observed
    concretely: test_top_tables.py's own ``monkeypatch.setattr("dataplat.cli
    .db.top_tables.load_duckdb", ...)`` started failing with ``AttributeError:
    'module' object ... has no attribute 'top_tables'`` after this test ran
    first). A subprocess's whole module cache is thrown away when it exits,
    so there is nothing to restore, and it also happens to be exactly the
    real-world scenario under test: a fresh ``dp db ...`` invocation in a
    process that has never imported anything else.
    """
    result = subprocess.run(
        [sys.executable, "-c", _BLOCK_YAML_AND_IMPORT_DB_SCRIPT],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK" in result.stdout
