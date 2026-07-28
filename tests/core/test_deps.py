from __future__ import annotations

import importlib.metadata

import pytest

from dataplat.core import deps


def test_missing_modules_empty_when_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(deps, "find_spec", lambda name: object())
    assert deps.missing_modules("db") == []
    assert deps.area_ready("db")


def test_missing_modules_lists_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        deps, "find_spec", lambda name: None if name == "textual" else object()
    )
    assert deps.missing_modules("ingest") == ["textual"]
    assert not deps.area_ready("ingest")


def test_missing_for_and_ready_take_a_spec(monkeypatch: pytest.MonkeyPatch) -> None:
    """The spec-taking pair is what the registry calls: a third-party area
    carries its own AreaDeps and is not in the AREAS dict."""
    monkeypatch.setattr(
        deps, "find_spec", lambda name: None if name == "pandas" else object()
    )
    plugin = deps.AreaDeps(
        area="lake",
        extra="lake",
        modules=("json", "pandas"),
        enabled_by=("DP_LAKE_URL",),
    )

    assert deps.missing_for(plugin) == ["pandas"]
    assert not deps.ready(plugin)
    assert deps.ready(deps.AREAS["db"])
    with pytest.raises(KeyError):
        deps.missing_modules(plugin.area)


def test_satisfied_extras_lists_importable_areas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        deps, "find_spec", lambda name: None if name == "psycopg" else object()
    )
    assert deps.satisfied_extras() == ["ingest", "bi", "cloud", "duckdb"]


def test_enabled_areas_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # conftest sets DP_TARGETS; enable ingest and cloud too.
    monkeypatch.setenv("AIRBYTE_BASE_URL", "https://airbyte.example.com")
    monkeypatch.setenv("DP_RDS_INSTANCE", "prod-db-1")
    monkeypatch.delenv("SUPERSET_BASE_URL", raising=False)

    enabled = deps.enabled_areas()

    assert enabled["db"] == "DP_TARGETS"
    assert enabled["ingest"] == "AIRBYTE_BASE_URL"
    assert enabled["cloud"] == "DP_RDS_INSTANCE"
    assert "bi" not in enabled


def test_install_spec_sorted_and_deduplicated() -> None:
    assert deps.install_spec(["ingest", "db", "db"]) == "dataplat[db,ingest]"


def test_install_spec_pins_version() -> None:
    assert deps.install_spec(["db"], version="1.2.3") == "dataplat[db]==1.2.3"


def _not_editable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(deps, "_is_editable_install", lambda: False)


def _env(
    monkeypatch: pytest.MonkeyPatch,
    *,
    version: str | None = "0.1.0",
    satisfied: list[str] | None = None,
) -> None:
    """Pin the two environment facts install_command reads, so the expected
    spec does not depend on what the test machine happens to have installed."""
    _not_editable(monkeypatch)
    monkeypatch.setattr(deps, "_installed_version", lambda: version)
    monkeypatch.setattr(deps, "satisfied_extras", lambda: satisfied or [])


def test_install_command_uv_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch)
    monkeypatch.setattr(deps.shutil, "which", lambda name: f"/usr/local/bin/{name}")
    cmd = deps.install_command(
        ["db"],
        executable="/Users/me/.local/share/uv/tools/dataplat/bin/python",
    )
    assert cmd == ["uv", "tool", "install", "dataplat[db]==0.1.0", "--force"]


def test_install_command_pipx(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch)
    monkeypatch.setattr(deps.shutil, "which", lambda name: f"/usr/local/bin/{name}")
    cmd = deps.install_command(
        ["db", "cloud"],
        executable="/Users/me/.local/pipx/venvs/dataplat/bin/python",
    )
    assert cmd == ["pipx", "install", "dataplat[cloud,db]==0.1.0", "--force"]


def test_install_command_plain_venv(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch)
    monkeypatch.setattr(deps.shutil, "which", lambda name: None)
    monkeypatch.setattr(deps, "_in_venv", lambda: True)
    monkeypatch.setattr(deps, "_has_pip", lambda exe: True)
    cmd = deps.install_command(["bi"], executable="/opt/venv/bin/python")
    assert cmd is not None
    assert cmd[1:] == ["-m", "pip", "install", "dataplat[bi]==0.1.0"]
    assert cmd[0].endswith("python")


def test_plain_venv_install_is_additive_not_unioned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the --force paths need the union.

    A plain install replaces nothing, so dragging already-satisfied extras into
    it would reinstall unrelated packages for no reason.
    """
    _env(monkeypatch, satisfied=["cloud", "ingest"])
    monkeypatch.setattr(deps.shutil, "which", lambda name: None)
    monkeypatch.setattr(deps, "_in_venv", lambda: True)
    monkeypatch.setattr(deps, "_has_pip", lambda exe: True)

    cmd = deps.install_command(["bi"], executable="/opt/venv/bin/python")

    assert cmd == [
        "/opt/venv/bin/python",
        "-m",
        "pip",
        "install",
        "dataplat[bi]==0.1.0",
    ]


def test_install_command_uses_uv_pip_when_venv_has_no_pip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """uv-created venvs ship no pip, so `-m pip` would just fail."""
    _env(monkeypatch)
    monkeypatch.setattr(deps.shutil, "which", lambda name: f"/usr/local/bin/{name}")
    monkeypatch.setattr(deps, "_in_venv", lambda: True)
    monkeypatch.setattr(deps, "_has_pip", lambda exe: False)

    cmd = deps.install_command(["bi"], executable="/opt/venv/bin/python")

    assert cmd == [
        "uv",
        "pip",
        "install",
        "--python",
        "/opt/venv/bin/python",
        "dataplat[bi]==0.1.0",
    ]


def test_install_command_none_when_no_pip_and_no_uv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing to call: the caller must fall back to manual_hint."""
    _env(monkeypatch)
    monkeypatch.setattr(deps.shutil, "which", lambda name: None)
    monkeypatch.setattr(deps, "_in_venv", lambda: True)
    monkeypatch.setattr(deps, "_has_pip", lambda exe: False)

    assert deps.install_command(["bi"], executable="/opt/venv/bin/python") is None


def test_manual_hint_recommends_uv_pip_without_pip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _not_editable(monkeypatch)
    monkeypatch.setattr(deps, "_in_venv", lambda: True)
    monkeypatch.setattr(deps, "_has_pip", lambda exe: False)

    hint = deps.manual_hint(["bi"])

    assert "uv pip install" in hint
    assert "dataplat[bi]" in hint


def test_has_pip_detects_sibling_script(tmp_path) -> None:
    """For another environment's interpreter, the bin/pip script is the signal."""
    bin_dir = tmp_path / "venv" / "bin"
    bin_dir.mkdir(parents=True)
    python = bin_dir / "python"
    python.write_text("")

    assert not deps._has_pip(python)

    (bin_dir / "pip").write_text("")

    assert deps._has_pip(python)


def test_install_command_unpinned_when_not_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch, version=None)
    monkeypatch.setattr(deps.shutil, "which", lambda name: f"/usr/local/bin/{name}")
    cmd = deps.install_command(
        ["db"],
        executable="/Users/me/.local/share/uv/tools/dataplat/bin/python",
    )
    assert cmd == ["uv", "tool", "install", "dataplat[db]", "--force"]


def test_installed_version_none_when_distribution_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_not_found(name: str) -> str:
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", raise_not_found)
    assert deps._installed_version() is None


def test_install_command_unions_already_satisfied_extras(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`uv tool install --force` rebuilds from the spec, so an installed extra
    left out of it would be uninstalled behind the user's back."""
    _env(monkeypatch, satisfied=["ingest", "bi"])
    monkeypatch.setattr(deps.shutil, "which", lambda name: f"/usr/local/bin/{name}")
    cmd = deps.install_command(
        ["db"],
        executable="/Users/me/.local/share/uv/tools/dataplat/bin/python",
    )
    assert cmd == [
        "uv",
        "tool",
        "install",
        "dataplat[bi,db,ingest]==0.1.0",
        "--force",
    ]


def test_install_command_unknown_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch)
    monkeypatch.setattr(deps.shutil, "which", lambda name: None)
    monkeypatch.setattr(deps, "_in_venv", lambda: False)
    assert deps.install_command(["db"], executable="/usr/bin/python3") is None


def test_install_command_editable_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(deps, "_is_editable_install", lambda: True)
    # A dev checkout must bail before any spec is built; probing the
    # environment for a version or extras would be wasted work.
    monkeypatch.setattr(
        deps, "satisfied_extras", lambda: pytest.fail("built a spec for a checkout")
    )
    assert (
        deps.install_command(
            ["db"],
            executable="/Users/me/.local/share/uv/tools/dataplat/bin/python",
        )
        is None
    )
    assert "uv sync" in deps.manual_hint(["db"])


def test_manual_hint_mentions_spec(monkeypatch: pytest.MonkeyPatch) -> None:
    _not_editable(monkeypatch)
    assert "dataplat[db]" in deps.manual_hint(["db"])


def test_install_command_does_not_resolve_venv_symlink(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """A venv's bin/python is a symlink; pip must run through the venv path,
    not the base interpreter it points to."""
    _not_editable(monkeypatch)
    monkeypatch.setattr(deps.shutil, "which", lambda name: None)
    monkeypatch.setattr(deps, "_in_venv", lambda: True)
    base = tmp_path / "base-python"
    base.write_text("")
    link = tmp_path / "venv" / "bin" / "python"
    link.parent.mkdir(parents=True)
    link.symlink_to(base)
    # A pip beside the interpreter is what marks this venv as pip-capable.
    (link.parent / "pip").write_text("")

    cmd = deps.install_command(["db"], executable=str(link))

    assert cmd is not None
    assert cmd[0] == str(link)


# =========================================================================
# Engine-level extras.
#
# dataplat[duckdb] is not an area's dependency: duckdb is one SqlEngine inside
# the db area, needed only when a target names it. The area machinery is
# deliberately not extended to reach it (EngineDeps says why), so what is
# tested here is the little that *is* shared — the install hint, and the one
# place engine extras must appear or they get uninstalled.
# =========================================================================


def test_duckdb_is_an_engine_extra_not_an_area(monkeypatch: pytest.MonkeyPatch) -> None:
    """It must not be reachable by the area helpers, or `dp duckdb` would mount."""
    spec = deps.ENGINE_DEPS["duckdb"]

    assert (spec.extra, spec.module) == ("duckdb", "duckdb")
    assert "duckdb" not in deps.AREAS
    with pytest.raises(KeyError):
        deps.missing_modules("duckdb")
    # And the db area still means psycopg alone: a PostgreSQL user must not be
    # made to install an embedded database engine.
    assert deps.AREAS["db"].modules == ("psycopg",)


def test_engine_deps_ready_follows_the_import(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = deps.ENGINE_DEPS["duckdb"]
    monkeypatch.setattr(deps, "find_spec", lambda name: None)
    assert not deps.engine_deps_ready(spec)
    monkeypatch.setattr(deps, "find_spec", lambda name: object())
    assert deps.engine_deps_ready(spec)


def test_satisfied_extras_keeps_duckdb_from_being_uninstalled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`uv tool install --force` rebuilds from the spec it is handed.

    An extra missing from satisfied_extras() is dropped from that spec and
    therefore uninstalled the next time any area self-installs — and no area
    would ever put dataplat[duckdb] back.
    """
    _not_editable(monkeypatch)
    monkeypatch.setattr(deps, "_installed_version", lambda: "0.3.0")
    monkeypatch.setattr(deps, "find_spec", lambda name: object())
    monkeypatch.setattr(deps.shutil, "which", lambda name: f"/usr/local/bin/{name}")

    cmd = deps.install_command(
        ["ingest"],
        executable="/Users/me/.local/share/uv/tools/dataplat/bin/python",
    )

    assert cmd is not None
    assert "duckdb" in cmd[3]


def test_engine_install_hint_is_a_runnable_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = deps.ENGINE_DEPS["duckdb"]
    _env(monkeypatch)
    monkeypatch.setattr(deps.shutil, "which", lambda name: None)
    monkeypatch.setattr(deps, "_in_venv", lambda: True)
    monkeypatch.setattr(deps, "_has_pip", lambda exe: True)

    hint = deps.engine_install_hint(spec)

    assert hint.startswith("Run: ")
    # Quoted as printed: the brackets would otherwise be globbed by a shell.
    assert "'dataplat[duckdb]==0.1.0'" in hint


def test_engine_install_hint_falls_back_to_the_manual_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing to run in an environment we do not manage — say what to do."""
    spec = deps.ENGINE_DEPS["duckdb"]
    monkeypatch.setattr(deps, "install_command", lambda extras: None)
    monkeypatch.setattr(deps, "_is_editable_install", lambda: True)

    assert deps.engine_install_hint(spec) == (
        "Development checkout — run: uv sync --group dev --all-extras"
    )
