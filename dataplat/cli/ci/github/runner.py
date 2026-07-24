"""GitHub Actions runner management commands."""

import os
import re
import subprocess
from pathlib import Path
from urllib.parse import urlparse

import typer
from rich.console import Console
from rich.table import Table

app = typer.Typer(
    name="runner",
    help="Manage GitHub Actions self-hosted runners",
    no_args_is_help=True,
)

console = Console()

DEFAULT_IMAGE = "myoung34/github-runner:2.334.0-ubuntu-noble"
DEFAULT_RUNNER_STATE_DIR = Path.home() / ".config" / "dataplat" / "github-runner"

# Extra DNS resolvers for the runner container. Docker Desktop does not honor
# macOS per-domain VPN resolver scopes, so a container may get only public DNS
# and fail to resolve internal hostnames; point DP_CI_RUNNER_DNS (comma-
# separated) at your VPN resolver when that bites.
DEFAULT_DNS = [
    s.strip() for s in os.getenv("DP_CI_RUNNER_DNS", "").split(",") if s.strip()
]


def run_command(
    cmd: list[str],
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Run a shell command and return the result.

    ``env``, when given, is the full environment for the child process —
    used to hand secrets to ``docker`` without putting them in argv.
    """
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=check,
            env=env,
        )
        return result
    except FileNotFoundError:
        console.print(f"[red]Error: {cmd[0]} not found on PATH[/red]")
        raise typer.Exit(code=1)
    except subprocess.CalledProcessError as e:
        console.print(f"[red]Command failed: {' '.join(cmd)}[/red]")
        console.print(f"[red]Error: {e.stderr}[/red]")
        raise typer.Exit(code=1)


def ensure_docker_available() -> None:
    """Exit with a clear message when docker is missing or the daemon is down."""
    result = run_command(["docker", "info", "--format", "{{.ServerVersion}}"], check=False)
    if result.returncode != 0:
        detail = (result.stderr or "").strip().splitlines()
        hint = detail[0] if detail else "daemon not reachable"
        console.print(f"[red]Error: docker daemon unavailable ({hint})[/red]")
        raise typer.Exit(code=1)


def get_env_var(name: str) -> str:
    """Get an environment variable or exit with error."""
    value = os.getenv(name)
    if not value:
        console.print(f"[red]Error: {name} environment variable must be set[/red]")
        raise typer.Exit(code=1)
    return value


def ensure_image_present(image: str) -> None:
    """Ensure ``image`` is available locally, streaming the pull if not.

    ``docker run`` will pull silently when the image is missing, but the
    surrounding ``run_command`` captures stdout/stderr — so a multi-GB pull
    looks like a hang. Stream the pull through the user's terminal instead.
    """
    inspect = run_command(["docker", "image", "inspect", image], check=False)
    if getattr(inspect, "returncode", 0) == 0:
        return
    console.print(f"[dim]Image {image} not present locally. Pulling…[/dim]")
    try:
        subprocess.run(["docker", "pull", image], check=True)
    except subprocess.CalledProcessError:
        console.print(f"[red]Failed to pull image: {image}[/red]")
        raise typer.Exit(code=1)


def get_container_name(runner_name: str) -> str:
    """Generate a Docker-safe container name from the runner name."""
    return f"gha-runner-{_slug(runner_name, fallback='runner')}"


def get_runner_state_dir() -> Path:
    """Return the local state directory for runner mount data."""
    override = os.getenv("DP_GITHUB_RUNNER_STATE_DIR")
    if override:
        return Path(override).expanduser()
    return DEFAULT_RUNNER_STATE_DIR


def _slug(value: str, fallback: str) -> str:
    """Normalize arbitrary text for local filesystem paths."""
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-.")
    if normalized:
        return normalized
    return fallback


def _normalize_repo_ref(repo_url: str) -> str:
    """Normalize repository URLs to a stable reference."""
    candidate = repo_url.strip()
    if candidate.endswith(".git"):
        candidate = candidate[:-4]

    if "://" in candidate:
        parsed = urlparse(candidate)
        if parsed.netloc and parsed.path:
            return f"{parsed.netloc}/{parsed.path.strip('/')}".lower()

    if "@" in candidate and ":" in candidate.split("@", 1)[1]:
        _, remote = candidate.split("@", 1)
        host, path = remote.split(":", 1)
        return f"{host}/{path.strip('/')}".lower()

    return candidate.lower()


def _repo_id(repo_url: str) -> str:
    """Build a filesystem-safe identifier for a repository URL."""
    return _slug(_normalize_repo_ref(repo_url), fallback="repo")


def get_repo_mount_dir(repo_url: str) -> Path:
    """Return the default host mount directory for a repository."""
    return get_runner_state_dir() / "workdirs" / _repo_id(repo_url)


def get_repo_mount_record_path(repo_url: str) -> Path:
    """Return the path of the mount-record file for a repository."""
    return get_runner_state_dir() / "mounts" / f"{_repo_id(repo_url)}.path"


def resolve_local_workdir(repo_url: str, local_workdir: str | None) -> Path:
    """Resolve (and create) the local workdir for a repository mount."""
    workdir = (
        Path(local_workdir).expanduser()
        if local_workdir
        else get_repo_mount_dir(repo_url)
    )
    workdir.mkdir(parents=True, exist_ok=True)
    return workdir.resolve()


def record_repo_mount(repo_url: str, resolved_workdir: Path) -> Path:
    """Persist the mount record after a successful container start."""
    mount_record_path = get_repo_mount_record_path(repo_url)
    mount_record_path.parent.mkdir(parents=True, exist_ok=True)
    mount_record_path.write_text(f"{resolved_workdir}\n")
    return mount_record_path


@app.command()
def start(
    runner_name: str = typer.Option(
        ..., "--runner-name", "-n", help="Name for the GitHub Actions runner"
    ),
    repo_url: str = typer.Option(
        ...,
        "--repo-url",
        "-r",
        help="Repository URL (e.g., https://github.com/org/repo)",
    ),
    runner_scope: str = typer.Option(
        "repo", "--scope", "-s", help="Runner scope (repo, org, or enterprise)"
    ),
    runner_workdir: str = typer.Option(
        "/tmp/.github/runner", "--workdir", "-w", help="Runner working directory"
    ),
    debug_output: bool = typer.Option(
        True, "--debug/--no-debug", "-d/-D", help="Enable debug output"
    ),
    local_workdir: str | None = typer.Option(
        None,
        "--local-workdir",
        "-l",
        help=(
            "Local directory to mount as runner workdir. "
            "Defaults to ~/.config/dataplat/github-runner/workdirs/<repository-id>"
        ),
    ),
    image: str = typer.Option(
        DEFAULT_IMAGE, "--image", "-i", help="Docker image to use"
    ),
    dns: list[str] = typer.Option(
        DEFAULT_DNS,
        "--dns",
        help=(
            "DNS server(s) for the runner container (repeatable). Defaults "
            "to DP_CI_RUNNER_DNS (comma-separated); useful when the runner "
            "must use a VPN resolver for internal hostnames. Pass --dns '' "
            "for Docker's default DNS."
        ),
    ),
):
    """Start the GitHub Actions runner container."""
    console.print("[blue]Starting GitHub Actions runner...[/blue]")

    # Check for required environment variables
    app_id = get_env_var("GHA_APP_ID")
    app_private_key = get_env_var("GHA_APP_PRIVATE_KEY")

    ensure_docker_available()
    container_name = get_container_name(runner_name)

    # Check for existing container
    console.print("Checking for existing GitHub Actions runner...")
    result = run_command(
        ["docker", "ps", "-a", "-q", "-f", f"name={container_name}"], check=False
    )

    if result.stdout.strip():
        console.print("Found existing runner container. Removing it...")
        run_command(["docker", "stop", container_name], check=False)
        run_command(["docker", "rm", container_name], check=False)

    # Start new container
    console.print("Starting new GitHub Actions runner container...")
    ensure_image_present(image)
    resolved_local_workdir = resolve_local_workdir(repo_url, local_workdir)

    docker_cmd = [
        "docker",
        "run",
        "-d",
        "--restart",
        "on-failure:5",
        "--name",
        container_name,
    ]
    # Pin DNS when internal hostnames need a specific resolver (see
    # DEFAULT_DNS). Empty entries fall back to Docker's default resolver.
    for dns_server in dns:
        if dns_server and dns_server.strip():
            docker_cmd += ["--dns", dns_server.strip()]
    # Secrets are handed to the docker CLI via its process environment and
    # referenced by bare `-e NAME` flags, so the private key never appears
    # in argv (`ps`) or `docker inspect`-able command lines on the host side.
    docker_cmd += [
        "-e",
        f"RUNNER_NAME={runner_name}",
        "-e",
        "APP_ID",
        "-e",
        "APP_PRIVATE_KEY",
        "-e",
        f"RUNNER_SCOPE={runner_scope}",
        "-e",
        f"RUNNER_WORKDIR={runner_workdir}",
        "-e",
        f"DEBUG_OUTPUT={str(debug_output).lower()}",
        "-e",
        f"REPO_URL={repo_url}",
        "-v",
        "/var/run/docker.sock:/var/run/docker.sock",
        "-v",
        f"{resolved_local_workdir}:{runner_workdir}",
        image,
    ]

    docker_env = {
        **os.environ,
        "APP_ID": app_id,
        "APP_PRIVATE_KEY": app_private_key,
    }
    run_command(docker_cmd, env=docker_env)
    record_repo_mount(repo_url, resolved_local_workdir)
    console.print("[green]✓ GitHub Actions runner started successfully[/green]")
    console.print(f"[dim]Container name: {container_name}[/dim]")
    console.print(f"[dim]Local mount: {resolved_local_workdir}[/dim]")
    console.print(f"[dim]Mount record: {get_repo_mount_record_path(repo_url)}[/dim]")


@app.command()
def stop(
    runner_name: str = typer.Option(
        ..., "--runner-name", "-n", help="Name of the GitHub Actions runner to stop"
    ),
):
    """Stop and remove the GitHub Actions runner container."""
    console.print("[blue]Stopping GitHub Actions runner...[/blue]")

    ensure_docker_available()
    container_name = get_container_name(runner_name)

    # Check if container is running
    result = run_command(
        ["docker", "ps", "-q", "-f", f"name={container_name}"], check=False
    )

    if result.stdout.strip():
        run_command(["docker", "stop", container_name])
        run_command(["docker", "rm", container_name])
        console.print("[green]✓ GitHub Actions runner stopped and removed[/green]")
    else:
        console.print("[yellow]! No running GitHub Actions runner found[/yellow]")


@app.command()
def status(
    runner_name: str = typer.Option(
        ..., "--runner-name", "-n", help="Name of the GitHub Actions runner to check"
    ),
):
    """Check the status of the GitHub Actions runner."""
    console.print("[blue]GitHub Actions runner status:[/blue]\n")

    ensure_docker_available()
    container_name = get_container_name(runner_name)

    # Check if container is running
    running_result = run_command(
        ["docker", "ps", "-q", "-f", f"name={container_name}"], check=False
    )

    if running_result.stdout.strip():
        console.print("[green]✓ Runner is running[/green]\n")

        # Get detailed status
        result = run_command(
            [
                "docker",
                "ps",
                "-f",
                f"name={container_name}",
                "--format",
                "{{.Names}}\t{{.Status}}\t{{.Ports}}",
            ],
            check=False,
        )

        if result.stdout.strip():
            lines = result.stdout.strip().split("\n")
            table = Table(show_header=True, header_style="bold cyan")
            table.add_column("Name")
            table.add_column("Status")
            table.add_column("Ports")

            for line in lines:
                parts = line.split("\t")
                table.add_row(*parts)

            console.print(table)
    else:
        # Check if container exists but is not running
        exists_result = run_command(
            ["docker", "ps", "-a", "-q", "-f", f"name={container_name}"], check=False
        )

        if exists_result.stdout.strip():
            console.print(
                "[yellow]! Runner container exists but is not running[/yellow]\n"
            )

            result = run_command(
                [
                    "docker",
                    "ps",
                    "-a",
                    "-f",
                    f"name={container_name}",
                    "--format",
                    "{{.Names}}\t{{.Status}}",
                ],
                check=False,
            )

            if result.stdout.strip():
                lines = result.stdout.strip().split("\n")
                table = Table(show_header=True, header_style="bold yellow")
                table.add_column("Name")
                table.add_column("Status")

                for line in lines:
                    parts = line.split("\t")
                    table.add_row(*parts)

                console.print(table)
        else:
            console.print("[red]✗ No runner container found[/red]")
