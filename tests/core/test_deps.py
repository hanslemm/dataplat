from __future__ import annotations

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


def _not_editable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(deps, "_is_editable_install", lambda: False)


def test_install_command_uv_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    _not_editable(monkeypatch)
    monkeypatch.setattr(deps.shutil, "which", lambda name: f"/usr/local/bin/{name}")
    cmd = deps.install_command(
        ["db"],
        executable="/Users/me/.local/share/uv/tools/dataplat/bin/python",
    )
    assert cmd == ["uv", "tool", "install", "dataplat[db]", "--force"]


def test_install_command_pipx(monkeypatch: pytest.MonkeyPatch) -> None:
    _not_editable(monkeypatch)
    monkeypatch.setattr(deps.shutil, "which", lambda name: f"/usr/local/bin/{name}")
    cmd = deps.install_command(
        ["db", "cloud"],
        executable="/Users/me/.local/pipx/venvs/dataplat/bin/python",
    )
    assert cmd == ["pipx", "install", "dataplat[cloud,db]", "--force"]


def test_install_command_plain_venv(monkeypatch: pytest.MonkeyPatch) -> None:
    _not_editable(monkeypatch)
    monkeypatch.setattr(deps.shutil, "which", lambda name: None)
    monkeypatch.setattr(deps, "_in_venv", lambda: True)
    cmd = deps.install_command(["bi"], executable="/opt/venv/bin/python")
    assert cmd is not None
    assert cmd[1:] == ["-m", "pip", "install", "dataplat[bi]"]
    assert cmd[0].endswith("python")


def test_install_command_unknown_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    _not_editable(monkeypatch)
    monkeypatch.setattr(deps.shutil, "which", lambda name: None)
    monkeypatch.setattr(deps, "_in_venv", lambda: False)
    assert deps.install_command(["db"], executable="/usr/bin/python3") is None


def test_install_command_editable_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(deps, "_is_editable_install", lambda: True)
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

    cmd = deps.install_command(["db"], executable=str(link))

    assert cmd is not None
    assert cmd[0] == str(link)
