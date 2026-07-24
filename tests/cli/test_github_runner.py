from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

from dataplat.cli.ci.github import runner as runner_cli


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
    assert (
        runner_cli.get_container_name("My Runner/1")
        == "gha-runner-My-Runner-1"
    )


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

    assert pull_calls == [
        {"cmd": ["docker", "pull", image], "kwargs": {"check": True}}
    ]
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
