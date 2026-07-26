from __future__ import annotations

import re
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import typer
from rich.console import Console

from dataplat.cli.ci.github import runner as runner_cli


def _wide_console(monkeypatch) -> Console:
    """Render through a console wide enough not to wrap the asserted values."""
    console = Console(width=200, no_color=True, legacy_windows=False)
    monkeypatch.setattr(runner_cli, "console", console)
    return console


def _fake_docker(
    running: dict[str, str] | None = None,
    stopped: dict[str, str] | None = None,
    ports: str = "0.0.0.0:80->80/tcp",
) -> tuple[list[list[str]], object]:
    """A docker stand-in that honors ``-f name=`` exactly as docker does.

    docker treats the filter value as an *unanchored* regex, so this fake
    matches with ``re.search``: an unanchored filter really does return
    sibling containers here, which is what makes the anchor testable.
    """
    live = running or {}
    dead = stopped or {}
    calls: list[list[str]] = []

    def _matching(cmd: list[str], pool: dict[str, str]) -> list[str]:
        pattern = next(a.split("=", 1)[1] for a in cmd if a.startswith("name="))
        return [name for name in pool if re.search(pattern, name)]

    def fake_run_command(cmd: list[str], check: bool = True, env=None):  # noqa: ARG001
        calls.append(cmd)
        if cmd[:2] == ["docker", "info"]:
            return SimpleNamespace(stdout="27.0", stderr="", returncode=0)
        if cmd[:2] == ["docker", "ps"]:
            pool = {**dead, **live} if "-a" in cmd else live
            matched = _matching(cmd, pool)
            if "-q" in cmd:
                stdout = "".join(f"{name}-id\n" for name in matched)
            else:
                columns = cmd[cmd.index("--format") + 1].count("{{")
                rows = [
                    "\t".join([name, pool[name], ports][:columns]) for name in matched
                ]
                stdout = "\n".join(rows) + ("\n" if rows else "")
            return SimpleNamespace(stdout=stdout, stderr="", returncode=0)
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    return calls, fake_run_command


def _fake_run_command_factory() -> tuple[list[list[str]], object]:
    commands: list[list[str]] = []
    envs: list[dict | None] = []

    def _fake_run_command(cmd: list[str], check: bool = True, env=None):  # noqa: ARG001
        commands.append(cmd)
        envs.append(env)
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    _fake_run_command.envs = envs  # type: ignore[attr-defined]
    return commands, _fake_run_command


def test_runner_start_uses_fixed_mount_dir(monkeypatch, tmp_path: Path) -> None:
    state_dir = tmp_path / "runner-state"
    repo_url = "https://github.com/org/repo"
    monkeypatch.setenv("DP_GITHUB_RUNNER_STATE_DIR", str(state_dir))
    monkeypatch.setenv("GHA_APP_ID", "123")
    monkeypatch.setenv("GHA_APP_PRIVATE_KEY", "private-key")
    commands, fake_run_command = _fake_run_command_factory()
    monkeypatch.setattr(runner_cli, "run_command", fake_run_command)

    runner_cli.start(
        runner_name="my-runner",
        repo_url=repo_url,
        runner_scope="repo",
        runner_workdir="/tmp/.github/runner",
        debug_output=True,
        local_workdir=None,
        image=runner_cli.DEFAULT_IMAGE,
        dns=runner_cli.DEFAULT_DNS,
    )

    expected_mount_dir = runner_cli.get_repo_mount_dir(repo_url).resolve()
    assert expected_mount_dir.is_dir()

    docker_run_cmd = next(cmd for cmd in commands if cmd[:2] == ["docker", "run"])
    assert f"{expected_mount_dir}:/tmp/.github/runner" in docker_run_cmd

    mount_record_path = runner_cli.get_repo_mount_record_path(repo_url)
    assert mount_record_path.read_text().strip() == str(expected_mount_dir)


def test_runner_start_records_explicit_mount_dir(monkeypatch, tmp_path: Path) -> None:
    state_dir = tmp_path / "runner-state"
    custom_mount_dir = tmp_path / "custom-runner-dir"
    repo_url = "https://github.com/org/repo"
    monkeypatch.setenv("DP_GITHUB_RUNNER_STATE_DIR", str(state_dir))
    monkeypatch.setenv("GHA_APP_ID", "123")
    monkeypatch.setenv("GHA_APP_PRIVATE_KEY", "private-key")
    commands, fake_run_command = _fake_run_command_factory()
    monkeypatch.setattr(runner_cli, "run_command", fake_run_command)

    runner_cli.start(
        runner_name="repo-runner",
        repo_url=repo_url,
        runner_scope="repo",
        runner_workdir="/tmp/.github/runner",
        debug_output=True,
        local_workdir=str(custom_mount_dir),
        image=runner_cli.DEFAULT_IMAGE,
        dns=runner_cli.DEFAULT_DNS,
    )

    resolved_custom_mount = custom_mount_dir.resolve()
    assert resolved_custom_mount.is_dir()

    docker_run_cmd = next(cmd for cmd in commands if cmd[:2] == ["docker", "run"])
    assert f"{resolved_custom_mount}:/tmp/.github/runner" in docker_run_cmd

    mount_record_path = runner_cli.get_repo_mount_record_path(repo_url)
    assert mount_record_path.read_text().strip() == str(resolved_custom_mount)


def test_runner_mount_default_is_shared_per_repository(
    monkeypatch, tmp_path: Path
) -> None:
    state_dir = tmp_path / "runner-state"
    repo_url = "https://github.com/org/repo"
    monkeypatch.setenv("DP_GITHUB_RUNNER_STATE_DIR", str(state_dir))
    monkeypatch.setenv("GHA_APP_ID", "123")
    monkeypatch.setenv("GHA_APP_PRIVATE_KEY", "private-key")
    commands, fake_run_command = _fake_run_command_factory()
    monkeypatch.setattr(runner_cli, "run_command", fake_run_command)

    runner_cli.start(
        runner_name="runner-a",
        repo_url=repo_url,
        runner_scope="repo",
        runner_workdir="/tmp/.github/runner",
        debug_output=True,
        local_workdir=None,
        image=runner_cli.DEFAULT_IMAGE,
        dns=runner_cli.DEFAULT_DNS,
    )
    runner_cli.start(
        runner_name="runner-b",
        repo_url=repo_url,
        runner_scope="repo",
        runner_workdir="/tmp/.github/runner",
        debug_output=True,
        local_workdir=None,
        image=runner_cli.DEFAULT_IMAGE,
        dns=runner_cli.DEFAULT_DNS,
    )

    docker_run_cmds = [cmd for cmd in commands if cmd[:2] == ["docker", "run"]]
    expected_mount_dir = runner_cli.get_repo_mount_dir(repo_url).resolve()
    assert len(docker_run_cmds) == 2
    assert f"{expected_mount_dir}:/tmp/.github/runner" in docker_run_cmds[0]
    assert f"{expected_mount_dir}:/tmp/.github/runner" in docker_run_cmds[1]


def test_runner_start_pins_dns_servers(monkeypatch, tmp_path: Path) -> None:
    state_dir = tmp_path / "runner-state"
    repo_url = "https://github.com/org/repo"
    monkeypatch.setenv("DP_GITHUB_RUNNER_STATE_DIR", str(state_dir))
    monkeypatch.setenv("GHA_APP_ID", "123")
    monkeypatch.setenv("GHA_APP_PRIVATE_KEY", "private-key")
    commands, fake_run_command = _fake_run_command_factory()
    monkeypatch.setattr(runner_cli, "run_command", fake_run_command)

    runner_cli.start(
        runner_name="dns-runner",
        repo_url=repo_url,
        runner_scope="repo",
        runner_workdir="/tmp/.github/runner",
        debug_output=True,
        local_workdir=None,
        image=runner_cli.DEFAULT_IMAGE,
        dns=["100.68.255.254", "  ", ""],
    )

    docker_run_cmd = next(cmd for cmd in commands if cmd[:2] == ["docker", "run"])
    # Each non-empty DNS server becomes one `--dns <server>` pair; blanks drop.
    assert docker_run_cmd.count("--dns") == 1
    dns_idx = docker_run_cmd.index("--dns")
    assert docker_run_cmd[dns_idx + 1] == "100.68.255.254"
    # docker run flags must precede the image argument.
    assert dns_idx < docker_run_cmd.index(runner_cli.DEFAULT_IMAGE)


def test_runner_start_without_dns_omits_flag(monkeypatch, tmp_path: Path) -> None:
    state_dir = tmp_path / "runner-state"
    repo_url = "https://github.com/org/repo"
    monkeypatch.setenv("DP_GITHUB_RUNNER_STATE_DIR", str(state_dir))
    monkeypatch.setenv("GHA_APP_ID", "123")
    monkeypatch.setenv("GHA_APP_PRIVATE_KEY", "private-key")
    commands, fake_run_command = _fake_run_command_factory()
    monkeypatch.setattr(runner_cli, "run_command", fake_run_command)

    runner_cli.start(
        runner_name="no-dns-runner",
        repo_url=repo_url,
        runner_scope="repo",
        runner_workdir="/tmp/.github/runner",
        debug_output=True,
        local_workdir=None,
        image=runner_cli.DEFAULT_IMAGE,
        dns=[],
    )

    docker_run_cmd = next(cmd for cmd in commands if cmd[:2] == ["docker", "run"])
    assert "--dns" not in docker_run_cmd


def test_runner_start_keeps_private_key_out_of_argv(
    monkeypatch, tmp_path: Path
) -> None:
    state_dir = tmp_path / "runner-state"
    repo_url = "https://github.com/org/repo"
    secret = "-----BEGIN RSA PRIVATE KEY-----\nsuper-secret\n-----END-----"
    monkeypatch.setenv("DP_GITHUB_RUNNER_STATE_DIR", str(state_dir))
    monkeypatch.setenv("GHA_APP_ID", "123")
    monkeypatch.setenv("GHA_APP_PRIVATE_KEY", secret)
    commands, fake_run_command = _fake_run_command_factory()
    monkeypatch.setattr(runner_cli, "run_command", fake_run_command)

    runner_cli.start(
        runner_name="secret-runner",
        repo_url=repo_url,
        runner_scope="repo",
        runner_workdir="/tmp/.github/runner",
        debug_output=True,
        local_workdir=None,
        image=runner_cli.DEFAULT_IMAGE,
        dns=runner_cli.DEFAULT_DNS,
    )

    docker_run_idx, docker_run_cmd = next(
        (i, cmd) for i, cmd in enumerate(commands) if cmd[:2] == ["docker", "run"]
    )
    # Secrets referenced by bare name, values never in argv.
    assert "APP_PRIVATE_KEY" in docker_run_cmd
    assert all(secret not in arg for arg in docker_run_cmd)
    assert all("APP_PRIVATE_KEY=" not in arg for arg in docker_run_cmd)
    # Values delivered via the docker CLI's process environment instead.
    env = fake_run_command.envs[docker_run_idx]  # type: ignore[attr-defined]
    assert env is not None
    assert env["APP_PRIVATE_KEY"] == secret
    assert env["APP_ID"] == "123"


def test_container_name_is_slugified() -> None:
    assert runner_cli.get_container_name("My Runner/1") == "gha-runner-My-Runner-1"


def test_runner_stop_running_container(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run_command(cmd: list[str], check: bool = True, env=None):  # noqa: ARG001
        calls.append(cmd)
        if cmd[:2] == ["docker", "info"]:
            return SimpleNamespace(stdout="27.0", stderr="", returncode=0)
        if cmd[:2] == ["docker", "ps"]:
            return SimpleNamespace(stdout="abc123\n", stderr="", returncode=0)
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(runner_cli, "run_command", fake_run_command)

    runner_cli.stop(runner_name="my-runner")

    assert ["docker", "stop", "gha-runner-my-runner"] in calls
    assert ["docker", "rm", "gha-runner-my-runner"] in calls


def test_runner_stop_no_container(monkeypatch, capsys) -> None:
    def fake_run_command(cmd: list[str], check: bool = True, env=None):  # noqa: ARG001
        if cmd[:2] == ["docker", "info"]:
            return SimpleNamespace(stdout="27.0", stderr="", returncode=0)
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(runner_cli, "run_command", fake_run_command)

    runner_cli.stop(runner_name="ghost")

    assert "No running GitHub Actions runner found" in capsys.readouterr().out


def test_runner_status_daemon_down(monkeypatch, capsys) -> None:
    import typer

    def fake_run_command(cmd: list[str], check: bool = True, env=None):  # noqa: ARG001
        if cmd[:2] == ["docker", "info"]:
            return SimpleNamespace(
                stdout="",
                stderr="Cannot connect to the Docker daemon",
                returncode=1,
            )
        raise AssertionError("should not reach other docker calls")

    monkeypatch.setattr(runner_cli, "run_command", fake_run_command)

    try:
        runner_cli.status(runner_name="any")
    except typer.Exit as exc:
        assert exc.exit_code == 1
    else:
        raise AssertionError("status should have exited")

    assert "docker daemon unavailable" in capsys.readouterr().out


def test_runner_status_not_found(monkeypatch, capsys) -> None:
    def fake_run_command(cmd: list[str], check: bool = True, env=None):  # noqa: ARG001
        if cmd[:2] == ["docker", "info"]:
            return SimpleNamespace(stdout="27.0", stderr="", returncode=0)
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(runner_cli, "run_command", fake_run_command)

    runner_cli.status(runner_name="missing")

    assert "No runner container found" in capsys.readouterr().out


def test_run_command_docker_missing(monkeypatch, capsys) -> None:
    import typer

    def fake_subprocess_run(cmd, **kwargs):  # noqa: ARG001
        raise FileNotFoundError("docker")

    monkeypatch.setattr(runner_cli.subprocess, "run", fake_subprocess_run)

    try:
        runner_cli.run_command(["docker", "ps"])
    except typer.Exit as exc:
        assert exc.exit_code == 1
    else:
        raise AssertionError("run_command should have exited")

    assert "docker not found on PATH" in capsys.readouterr().out


def test_ensure_image_present_streams_pull_when_image_missing(monkeypatch) -> None:
    image = "myoung34/github-runner:9.9.9-ubuntu-noble"

    def fake_run_command(cmd: list[str], check: bool = True):  # noqa: ARG001
        # Simulate `docker image inspect` reporting a missing image.
        return SimpleNamespace(stdout="", returncode=1)

    pull_calls: list[dict] = []

    def fake_subprocess_run(cmd, **kwargs):
        pull_calls.append({"cmd": cmd, "kwargs": kwargs})
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(runner_cli, "run_command", fake_run_command)
    monkeypatch.setattr(runner_cli.subprocess, "run", fake_subprocess_run)

    runner_cli.ensure_image_present(image)

    assert pull_calls == [{"cmd": ["docker", "pull", image], "kwargs": {"check": True}}]
    # Output must NOT be captured — the user needs to see pull progress live.
    assert "capture_output" not in pull_calls[0]["kwargs"]
    assert "stdout" not in pull_calls[0]["kwargs"]
    assert "stderr" not in pull_calls[0]["kwargs"]


def test_ensure_image_present_skips_pull_when_image_already_local(
    monkeypatch,
) -> None:
    image = "myoung34/github-runner:2.334.0-ubuntu-noble"

    def fake_run_command(cmd: list[str], check: bool = True):  # noqa: ARG001
        return SimpleNamespace(stdout="", returncode=0)

    pull_calls: list[list[str]] = []

    def fake_subprocess_run(cmd, **kwargs):  # noqa: ARG001
        pull_calls.append(cmd)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(runner_cli, "run_command", fake_run_command)
    monkeypatch.setattr(runner_cli.subprocess, "run", fake_subprocess_run)

    runner_cli.ensure_image_present(image)

    assert pull_calls == []


def test_ensure_image_present_exits_when_pull_fails(monkeypatch) -> None:
    import typer

    image = "myoung34/github-runner:does-not-exist"

    def fake_run_command(cmd: list[str], check: bool = True):  # noqa: ARG001
        return SimpleNamespace(stdout="", returncode=1)

    def fake_subprocess_run(cmd, **kwargs):  # noqa: ARG001
        raise subprocess.CalledProcessError(returncode=1, cmd=cmd)

    monkeypatch.setattr(runner_cli, "run_command", fake_run_command)
    monkeypatch.setattr(runner_cli.subprocess, "run", fake_subprocess_run)

    try:
        runner_cli.ensure_image_present(image)
    except typer.Exit as exc:
        assert exc.exit_code == 1
    else:
        raise AssertionError("ensure_image_present should have exited")


def test_name_filter_is_anchored() -> None:
    assert runner_cli.name_filter("gha-runner-foo") == "name=^gha-runner-foo$"


def test_name_filter_escapes_the_regex_dot() -> None:
    # A slugged name may keep dots; unescaped, "." would match any character.
    assert runner_cli.name_filter("gha-runner-v1.2") == r"name=^gha-runner-v1\.2$"


def test_status_filters_on_the_exact_container_name(monkeypatch) -> None:
    calls, fake_run_command = _fake_docker(running={"gha-runner-foo": "Up 2 minutes"})
    monkeypatch.setattr(runner_cli, "run_command", fake_run_command)

    runner_cli.status(runner_name="foo")

    assert ["docker", "ps", "-q", "-f", "name=^gha-runner-foo$"] in calls
    assert [
        "docker",
        "ps",
        "-f",
        "name=^gha-runner-foo$",
        "--format",
        "{{.Names}}\t{{.Status}}\t{{.Ports}}",
    ] in calls


def test_status_of_stopped_container_filters_on_the_exact_name(monkeypatch) -> None:
    calls, fake_run_command = _fake_docker(stopped={"gha-runner-foo": "Exited (0)"})
    monkeypatch.setattr(runner_cli, "run_command", fake_run_command)

    runner_cli.status(runner_name="foo")

    assert ["docker", "ps", "-a", "-q", "-f", "name=^gha-runner-foo$"] in calls
    assert [
        "docker",
        "ps",
        "-a",
        "-f",
        "name=^gha-runner-foo$",
        "--format",
        "{{.Names}}\t{{.Status}}",
    ] in calls


def test_stop_filters_on_the_exact_container_name(monkeypatch) -> None:
    calls, fake_run_command = _fake_docker(running={"gha-runner-foo": "Up 2 minutes"})
    monkeypatch.setattr(runner_cli, "run_command", fake_run_command)

    runner_cli.stop(runner_name="foo")

    assert ["docker", "ps", "-q", "-f", "name=^gha-runner-foo$"] in calls
    assert ["docker", "stop", "gha-runner-foo"] in calls


def test_start_filters_on_the_exact_container_name(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DP_GITHUB_RUNNER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("GHA_APP_ID", "123")
    monkeypatch.setenv("GHA_APP_PRIVATE_KEY", "private-key")
    calls, fake_run_command = _fake_docker()
    monkeypatch.setattr(runner_cli, "run_command", fake_run_command)

    runner_cli.start(
        runner_name="foo",
        repo_url="https://github.com/org/repo",
        runner_scope="repo",
        runner_workdir="/tmp/.github/runner",
        debug_output=True,
        local_workdir=None,
        image=runner_cli.DEFAULT_IMAGE,
        dns=[],
    )

    assert ["docker", "ps", "-a", "-q", "-f", "name=^gha-runner-foo$"] in calls


def test_status_ignores_a_longer_sibling_container(monkeypatch, capsys) -> None:
    """Regression: `-n foo` used to report gha-runner-foobar's row as ours."""
    calls, fake_run_command = _fake_docker(
        running={"gha-runner-foobar": "Up 3 hours"},
    )
    monkeypatch.setattr(runner_cli, "run_command", fake_run_command)

    runner_cli.status(runner_name="foo")

    out = capsys.readouterr().out
    assert "No runner container found" in out
    assert "foobar" not in out


def test_stop_ignores_a_longer_sibling_container(monkeypatch, capsys) -> None:
    calls, fake_run_command = _fake_docker(running={"gha-runner-foobar": "Up 3 hours"})
    monkeypatch.setattr(runner_cli, "run_command", fake_run_command)

    runner_cli.stop(runner_name="foo")

    assert "No running GitHub Actions runner found" in capsys.readouterr().out
    assert not [cmd for cmd in calls if cmd[:2] == ["docker", "stop"]]
    assert not [cmd for cmd in calls if cmd[:2] == ["docker", "rm"]]


def test_start_leaves_a_longer_sibling_container_alone(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("DP_GITHUB_RUNNER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("GHA_APP_ID", "123")
    monkeypatch.setenv("GHA_APP_PRIVATE_KEY", "private-key")
    calls, fake_run_command = _fake_docker(running={"gha-runner-foobar": "Up 3 hours"})
    monkeypatch.setattr(runner_cli, "run_command", fake_run_command)

    runner_cli.start(
        runner_name="foo",
        repo_url="https://github.com/org/repo",
        runner_scope="repo",
        runner_workdir="/tmp/.github/runner",
        debug_output=True,
        local_workdir=None,
        image=runner_cli.DEFAULT_IMAGE,
        dns=[],
    )

    assert not [cmd for cmd in calls if cmd[:2] == ["docker", "stop"]]
    assert not [cmd for cmd in calls if cmd[:2] == ["docker", "rm"]]


def test_status_table_shows_markup_like_docker_output_literally(monkeypatch) -> None:
    """``[/x]`` used to raise MarkupError and ``[bold]`` was swallowed."""
    console = _wide_console(monkeypatch)
    _, fake_run_command = _fake_docker(
        running={"gha-runner-hostile": "Up 2 minutes [/issue]"},
        ports="[bold]0.0.0.0:80->80/tcp",
    )
    monkeypatch.setattr(runner_cli, "run_command", fake_run_command)

    with console.capture() as capture:
        runner_cli.status(runner_name="hostile")

    out = capture.get()
    assert "Up 2 minutes [/issue]" in out
    assert "[bold]0.0.0.0:80->80/tcp" in out


def test_stopped_status_table_shows_markup_literally(monkeypatch) -> None:
    console = _wide_console(monkeypatch)
    _, fake_run_command = _fake_docker(
        stopped={"gha-runner-hostile": "Exited (1) [/issue] [bold]"},
    )
    monkeypatch.setattr(runner_cli, "run_command", fake_run_command)

    with console.capture() as capture:
        runner_cli.status(runner_name="hostile")

    assert "Exited (1) [/issue] [bold]" in capture.get()


def test_run_command_failure_echoes_argv_and_stderr_literally(monkeypatch) -> None:
    console = _wide_console(monkeypatch)
    cmd = ["docker", "run", "-e", "REPO_URL=https://git.test/[bold]/repo"]

    def fake_subprocess_run(*args, **kwargs):  # noqa: ARG001
        raise subprocess.CalledProcessError(
            returncode=1, cmd=cmd, stderr="docker: unknown flag [/oops]\n"
        )

    monkeypatch.setattr(runner_cli.subprocess, "run", fake_subprocess_run)

    with console.capture() as capture, pytest.raises(typer.Exit):
        runner_cli.run_command(cmd)

    out = capture.get()
    assert "REPO_URL=https://git.test/[bold]/repo" in out
    assert "docker: unknown flag [/oops]" in out


def test_daemon_unavailable_shows_markup_like_stderr_literally(monkeypatch) -> None:
    console = _wide_console(monkeypatch)

    def fake_run_command(cmd: list[str], check: bool = True, env=None):  # noqa: ARG001
        return SimpleNamespace(
            stdout="", stderr="Cannot connect [/issue] [bold]\n", returncode=1
        )

    monkeypatch.setattr(runner_cli, "run_command", fake_run_command)

    with console.capture() as capture, pytest.raises(typer.Exit):
        runner_cli.ensure_docker_available()

    assert "Cannot connect [/issue] [bold]" in capture.get()


def test_image_pull_failure_shows_markup_like_image_literally(monkeypatch) -> None:
    console = _wide_console(monkeypatch)
    image = "registry.test/runner:[bold]-tag"

    monkeypatch.setattr(
        runner_cli,
        "run_command",
        lambda cmd, check=True: SimpleNamespace(stdout="", returncode=1),
    )

    def fake_subprocess_run(cmd, **kwargs):  # noqa: ARG001
        raise subprocess.CalledProcessError(returncode=1, cmd=cmd)

    monkeypatch.setattr(runner_cli.subprocess, "run", fake_subprocess_run)

    with console.capture() as capture, pytest.raises(typer.Exit):
        runner_cli.ensure_image_present(image)

    out = capture.get()
    assert f"Image {image} not present locally" in out
    assert f"Failed to pull image: {image}" in out


def test_start_reports_markup_like_paths_literally(monkeypatch, tmp_path: Path) -> None:
    console = _wide_console(monkeypatch)
    monkeypatch.setenv("DP_GITHUB_RUNNER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("GHA_APP_ID", "123")
    monkeypatch.setenv("GHA_APP_PRIVATE_KEY", "private-key")
    _, fake_run_command = _fake_docker()
    monkeypatch.setattr(runner_cli, "run_command", fake_run_command)
    local_workdir = tmp_path / "[bold]work"

    with console.capture() as capture:
        runner_cli.start(
            runner_name="foo",
            repo_url="https://github.com/org/repo",
            runner_scope="repo",
            runner_workdir="/tmp/.github/runner",
            debug_output=True,
            local_workdir=str(local_workdir),
            image=runner_cli.DEFAULT_IMAGE,
            dns=[],
        )

    assert "[bold]work" in capture.get()


def test_runner_cli_wiring(monkeypatch) -> None:
    """Exercise option parsing through Typer, not just direct function calls."""
    from typer.testing import CliRunner

    import dataplat.main as main_module

    monkeypatch.setattr(main_module, "load_envrc", lambda: None)
    runner = CliRunner()

    result = runner.invoke(main_module.app, ["ci", "github", "runner", "--help"])
    assert result.exit_code == 0
    for cmd in ("start", "stop", "status"):
        assert cmd in result.stdout

    def fake_run_command(cmd, check=True, env=None):
        if cmd[:2] == ["docker", "info"]:
            return SimpleNamespace(stdout="27.0", stderr="", returncode=0)
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(runner_cli, "run_command", fake_run_command)
    result = runner.invoke(
        main_module.app, ["ci", "github", "runner", "status", "-n", "wired"]
    )
    assert result.exit_code == 0
    assert "No runner container found" in result.stdout
