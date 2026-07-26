from __future__ import annotations

import pytest
import typer
from typer.testing import CliRunner

from dataplat.cli import _missing
from dataplat.core.deps import AREAS, AreaDeps
from dataplat.core.registry import AreaMount, area_by_name

runner = CliRunner()

# A plugin-supplied area: its contract travels on the mount and is not in the
# AREAS global, which the stub used to look up by name.
THIRD_PARTY = AreaMount(
    name="widget",
    help_text="Widget tools",
    target="dataplat.cli.config:app",
    deps=AreaDeps(
        area="widget",
        extra="widget",
        modules=("dataplat_no_such_dependency",),
        enabled_by=("WIDGET_URL",),
    ),
)


class _Proc:
    """Stand-in for the CompletedProcess an install would return."""

    def __init__(self, returncode: int) -> None:
        self.returncode = returncode


def _stub(mount: AreaMount | None = None) -> typer.Typer:
    return _missing.build_missing_deps_app(mount or _db_mount())


def _db_mount() -> AreaMount:
    mount = area_by_name("db")
    assert mount is not None
    return mount


def _wide(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop Rich from wrapping the lines these tests read back."""
    monkeypatch.setenv("COLUMNS", "200")


def test_stub_help_mentions_extra() -> None:
    result = runner.invoke(_stub(), ["--help"])
    assert result.exit_code == 0
    assert "needs extra: db" in result.output


def test_stub_exits_when_install_is_unavailable_or_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``run_install`` returns False only for "nothing to run" / "it failed".

    A *refusal* leaves through confirm_or_exit instead, which is why that case
    needs its own test below rather than being covered by this stub.
    """
    monkeypatch.setattr(_missing, "missing_for", lambda spec: ["psycopg"])
    monkeypatch.setattr(_missing, "run_install", lambda extras, **kwargs: False)

    result = runner.invoke(_stub(), ["query", "SELECT 1", "--format", "json"])

    assert result.exit_code == 1
    assert "psycopg" in result.output
    assert "dataplat[db]" in result.output


def test_stub_refusal_installs_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Answering "no" must reach neither the installer nor a re-exec."""
    _wide(monkeypatch)
    monkeypatch.setattr(_missing, "missing_for", lambda spec: ["psycopg"])
    monkeypatch.setattr(
        _missing,
        "install_command",
        lambda extras: ["uv", "tool", "install", "dataplat[db]==0.1.0", "--force"],
    )
    monkeypatch.setattr(
        _missing.subprocess,
        "run",
        lambda *a, **k: pytest.fail("declined install must not run a subprocess"),
    )
    monkeypatch.setattr(
        _missing, "reexec", lambda: pytest.fail("declined install must not re-exec")
    )

    result = runner.invoke(_stub(), ["query", "SELECT 1"], input="n\n")

    assert result.exit_code == 1
    assert "Will run:" in result.output


def test_will_run_line_is_shell_quoted(monkeypatch: pytest.MonkeyPatch) -> None:
    """The printed command is advice the user may paste, so it must survive a
    shell: the spec's brackets would otherwise glob."""
    _wide(monkeypatch)
    monkeypatch.setattr(
        _missing,
        "install_command",
        lambda extras: ["uv", "tool", "install", "dataplat[db]==0.1.0", "--force"],
    )

    result = runner.invoke(_stub(), ["query", "SELECT 1"], input="n\n")

    assert "'dataplat[db]==0.1.0'" in result.output


def test_completion_does_not_run_the_install_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shell completion walks the tree with resilient_parsing set.

    click calls resolve_command as it descends, so running the handler there
    wrote the install offer onto the stream the shell evaluates — and on bash
    executed the install from a TAB press. Completion must fall through
    silently instead.
    """
    monkeypatch.setattr(
        _missing,
        "run_install",
        lambda *a, **k: pytest.fail("completion must not offer to install"),
    )
    group = typer.main.get_group(_stub())

    with group.make_context("db", ["query"], resilient_parsing=True) as ctx:
        name, cmd, args = group.resolve_command(ctx, ["query"])

    # cmd is None is click's signal to stop descending; nothing was printed.
    assert cmd is None


def test_stub_reexecs_after_successful_install(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installed: list[list[str]] = []
    reexeced: list[bool] = []

    monkeypatch.setattr(_missing, "missing_for", lambda spec: ["psycopg"])
    monkeypatch.setattr(
        _missing,
        "run_install",
        lambda extras, **kwargs: installed.append(extras) or True,
    )

    def fake_reexec():
        reexeced.append(True)
        raise typer.Exit(code=0)

    monkeypatch.setattr(_missing, "reexec", fake_reexec)

    result = runner.invoke(_stub(), ["query", "SELECT 1"])

    assert result.exit_code == 0
    assert installed == [["db"]]
    assert reexeced == [True]


def test_stub_offers_a_reachable_escape_hatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No --yes survives the stub's argument swallowing, so the gate must point
    # at the printed command instead of a flag the user cannot reach.
    _wide(monkeypatch)
    monkeypatch.setattr(_missing, "missing_for", lambda spec: ["psycopg"])
    monkeypatch.setattr(
        _missing, "install_command", lambda extras: ["uv", "tool", "install", "x"]
    )

    result = runner.invoke(_stub(), ["query", "SELECT 1"])

    assert result.exit_code == 1
    assert "Run the command above yourself" in result.output
    assert "--yes" not in result.output


def test_stub_works_for_an_area_that_is_not_a_builtin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _wide(monkeypatch)
    assert THIRD_PARTY.name not in AREAS
    monkeypatch.setattr(_missing, "install_command", lambda extras: None)

    result = runner.invoke(_stub(THIRD_PARTY), ["frob"])

    assert result.exit_code == 1
    assert "`dp widget` needs dataplat_no_such_dependency" in result.output
    assert "dataplat[widget]" in result.output


def test_stub_renders_bracketed_dependency_names_literally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A module or extra name carrying Rich markup used to either crash the
    # render ([/x] -> MarkupError) or vanish from it ([bold] -> a style).
    _wide(monkeypatch)
    monkeypatch.setattr(
        _missing, "missing_for", lambda spec: ["psycopg[/x]", "wheel[bold]"]
    )
    monkeypatch.setattr(_missing, "run_install", lambda extras, **kwargs: False)

    result = runner.invoke(_stub(), ["query"])

    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert result.exit_code == 1
    assert "psycopg[/x]" in result.output
    assert "wheel[bold]" in result.output


def test_run_install_prints_hint_when_no_installer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_missing, "install_command", lambda extras: None)
    assert _missing.run_install(["db"], yes=True) is False


def test_run_install_renders_a_bracketed_command_literally(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _wide(monkeypatch)
    monkeypatch.setattr(
        _missing,
        "install_command",
        lambda extras: ["pip", "install", "dataplat[db]", "--target", "/tmp/[/x]"],
    )
    monkeypatch.setattr(_missing.subprocess, "run", lambda cmd: _Proc(0))

    assert _missing.run_install(["db"], yes=True) is True

    out = capsys.readouterr().out
    assert "dataplat[db]" in out
    assert "/tmp/[/x]" in out


def test_run_install_exits_when_not_a_tty(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # pytest's stdin is never a tty, which is exactly the non-interactive case.
    _wide(monkeypatch)
    monkeypatch.setattr(
        _missing, "install_command", lambda extras: ["uv", "tool", "install", "x"]
    )
    ran: list[list[str]] = []
    monkeypatch.setattr(_missing.subprocess, "run", lambda cmd: ran.append(cmd))

    with pytest.raises(typer.Exit) as exc:
        _missing.run_install(["db"], yes=False)

    assert exc.value.exit_code == 1
    assert ran == []  # never installs silently
    assert "--yes" in capsys.readouterr().out  # the default escape hatch


def test_run_install_runs_command_with_yes(monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[list[str]] = []
    monkeypatch.setattr(
        _missing, "install_command", lambda extras: ["uv", "tool", "install", "x"]
    )
    monkeypatch.setattr(
        _missing.subprocess, "run", lambda cmd: ran.append(cmd) or _Proc(0)
    )

    assert _missing.run_install(["db"], yes=True) is True
    assert ran == [["uv", "tool", "install", "x"]]


def test_run_install_reports_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        _missing, "install_command", lambda extras: ["uv", "tool", "install", "x"]
    )
    monkeypatch.setattr(_missing.subprocess, "run", lambda cmd: _Proc(3))

    assert _missing.run_install(["db"], yes=True) is False
