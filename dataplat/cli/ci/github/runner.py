"""GitHub Actions runner management commands."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import typer
from rich.console import Console
from rich.table import Table

from dataplat.cli._render import cell, esc

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
        console.print(f"[red]Error: {esc(cmd[0])} not found on PATH[/red]")
        raise typer.Exit(code=1)
    except subprocess.CalledProcessError as e:
        # argv carries runner names, repo URLs and paths, and stderr is docker's
        # own text: both must be escaped or a stray "[..]" breaks the render.
        console.print(f"[red]Command failed: {esc(' '.join(cmd))}[/red]")
        console.print(f"[red]Error: {esc(e.stderr)}[/red]")
        raise typer.Exit(code=1)


def ensure_docker_available() -> None:
    """Exit with a clear message when docker is missing or the daemon is down."""
    result = run_command(
        ["docker", "info", "--format", "{{.ServerVersion}}"], check=False
    )
    if result.returncode != 0:
        detail = (result.stderr or "").strip().splitlines()
        hint = detail[0] if detail else "daemon not reachable"
        console.print(f"[red]Error: docker daemon unavailable ({esc(hint)})[/red]")
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
    console.print(f"[dim]Image {esc(image)} not present locally. Pulling…[/dim]")
    try:
        subprocess.run(["docker", "pull", image], check=True)
    except subprocess.CalledProcessError:
        console.print(f"[red]Failed to pull image: {esc(image)}[/red]")
        raise typer.Exit(code=1)


def get_container_name(runner_name: str) -> str:
    """Generate a Docker-safe container name from the runner name."""
    return f"gha-runner-{_slug(runner_name, fallback='runner')}"


def name_filter(container_name: str) -> str:
    """Build a ``docker ps -f`` value matching exactly one container.

    ``name=`` is an unanchored regex, not an equality test: filtering on
    ``gha-runner-foo`` also matches ``gha-runner-foobar``, so status would
    report a sibling runner's row as yours and stop/start would act on the
    wrong container. Anchor both ends, and escape the one metacharacter
    :func:`_slug` still lets through (``.``) so it cannot widen the match.
    """
    pattern = container_name.replace(".", r"\.")
    return f"name=^{pattern}$"


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


def _repo_owner(repo_url: str) -> str | None:
    """Return the account owning ``repo_url`` (``github.com/<owner>/<repo>``)."""
    parts = _normalize_repo_ref(repo_url).split("/")
    return parts[1] if len(parts) >= 3 and parts[1] else None


def _org_id(org: str) -> str:
    """Build a filesystem-safe identifier for an organization."""
    return _slug(f"github.com/{org.strip().lower()}", fallback="org")


def get_mount_dir(target_id: str) -> Path:
    """Return the default host mount directory for a runner target."""
    return get_runner_state_dir() / "workdirs" / target_id


def get_mount_record_path(target_id: str) -> Path:
    """Return the path of the mount-record file for a runner target."""
    return get_runner_state_dir() / "mounts" / f"{target_id}.path"


def get_repo_mount_dir(repo_url: str) -> Path:
    """Return the default host mount directory for a repository."""
    return get_mount_dir(_repo_id(repo_url))


def get_repo_mount_record_path(repo_url: str) -> Path:
    """Return the path of the mount-record file for a repository."""
    return get_mount_record_path(_repo_id(repo_url))


def get_org_mount_dir(org: str) -> Path:
    """Return the default host mount directory for an organization runner."""
    return get_mount_dir(_org_id(org))


def get_org_mount_record_path(org: str) -> Path:
    """Return the path of the mount-record file for an organization runner."""
    return get_mount_record_path(_org_id(org))


def resolve_local_workdir(target_id: str, local_workdir: str | None) -> Path:
    """Resolve (and create) the local workdir for a runner target's mount."""
    workdir = (
        Path(local_workdir).expanduser() if local_workdir else get_mount_dir(target_id)
    )
    workdir.mkdir(parents=True, exist_ok=True)
    return workdir.resolve()


def record_mount(target_id: str, resolved_workdir: Path) -> Path:
    """Persist the mount record after a successful container start."""
    mount_record_path = get_mount_record_path(target_id)
    mount_record_path.parent.mkdir(parents=True, exist_ok=True)
    mount_record_path.write_text(f"{resolved_workdir}\n")
    return mount_record_path


@dataclass(frozen=True)
class RunnerTarget:
    """Where a runner registers: one repository, or a whole organization.

    Organization runners serve every repository their runner group allows, so
    they need no ``REPO_URL``; the image instead needs ``ORG_NAME`` and, for
    GitHub App authentication, ``APP_LOGIN`` (the login the App is installed
    on). Registering at org level requires the App to hold the organization
    permission "Self-hosted runners: Read and write".
    """

    scope: str  # "repo" | "org"
    repo_url: str | None = None
    org: str | None = None
    runner_group: str | None = None

    @property
    def id(self) -> str:
        if self.scope == "org":
            assert self.org is not None
            return _org_id(self.org)
        assert self.repo_url is not None
        return _repo_id(self.repo_url)

    @property
    def label(self) -> str:
        if self.scope == "org":
            group = f" (runner group: {self.runner_group})" if self.runner_group else ""
            return f"organization {self.org}{group}"
        return f"repository {self.repo_url}"

    def docker_env(self) -> list[str]:
        """``NAME=value`` pairs the runner image needs for this target."""
        if self.scope == "org":
            assert self.org is not None
            env = [
                "RUNNER_SCOPE=org",
                f"ORG_NAME={self.org}",
                f"APP_LOGIN={self.org}",
            ]
            if self.runner_group:
                env.append(f"RUNNER_GROUP={self.runner_group}")
            return env
        assert self.repo_url is not None
        env = ["RUNNER_SCOPE=repo", f"REPO_URL={self.repo_url}"]
        owner = _repo_owner(self.repo_url)
        if owner:
            env.append(f"APP_LOGIN={owner}")
        return env


def resolve_runner_target(
    repo_url: str | None,
    org: str | None,
    runner_scope: str | None,
    runner_group: str | None,
) -> RunnerTarget:
    """Validate the target options and pick the registration scope.

    The scope is inferred from which of ``--repo-url`` / ``--org`` is given;
    an explicit ``--scope`` must agree with it. Enterprise scope is rejected
    because GitHub Apps cannot register enterprise runners.
    """
    if repo_url and org:
        raise typer.BadParameter(
            "Pass either --repo-url (repository runner) or --org "
            "(organization runner), not both."
        )
    if not repo_url and not org:
        raise typer.BadParameter(
            "Pass --repo-url for a repository runner or --org for an "
            "organization runner."
        )
    inferred = "org" if org else "repo"
    if runner_scope:
        wanted = runner_scope.strip().lower()
        if wanted in {"ent", "enterprise"}:
            raise typer.BadParameter(
                "Enterprise-scope runners cannot be registered with a GitHub "
                "App; use --repo-url or --org."
            )
        if wanted not in {"repo", "org"}:
            raise typer.BadParameter(f"Unknown --scope '{runner_scope}' (repo or org).")
        if wanted != inferred:
            flag = "--org" if org else "--repo-url"
            raise typer.BadParameter(
                f"--scope {wanted} does not match {flag}; drop --scope, it is inferred."
            )
    if runner_group and inferred == "repo":
        raise typer.BadParameter(
            "--runner-group only applies to organization runners (--org); "
            "repository runners always use the repository's default group."
        )
    return RunnerTarget(
        scope=inferred,
        repo_url=repo_url.strip() if repo_url else None,
        org=org.strip() if org else None,
        runner_group=runner_group.strip() if runner_group else None,
    )


@app.command()
def start(
    runner_name: str = typer.Option(
        ..., "--runner-name", "-n", help="Name for the GitHub Actions runner"
    ),
    repo_url: str | None = typer.Option(
        None,
        "--repo-url",
        "-r",
        help=(
            "Repository URL for a repository-level runner "
            "(e.g., https://github.com/org/repo)."
        ),
    ),
    org: str | None = typer.Option(
        None,
        "--org",
        "-o",
        help=(
            "Organization login for an organization-level runner "
            "(e.g., my-org). Serves every repository its runner group "
            "allows. Mutually exclusive with --repo-url."
        ),
    ),
    runner_group: str | None = typer.Option(
        None,
        "--runner-group",
        "-g",
        help="Runner group to join (organization runners only; default: Default).",
    ),
    runner_scope: str | None = typer.Option(
        None,
        "--scope",
        "-s",
        help="repo or org; inferred from --repo-url / --org when omitted.",
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
            "Defaults to ~/.config/dataplat/github-runner/workdirs/<target-id>"
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
    """Start the GitHub Actions runner container.

    Register against one repository (--repo-url) or a whole organization
    (--org, optionally --runner-group). Authentication uses the GitHub App
    from GHA_APP_ID / GHA_APP_PRIVATE_KEY; organization registration needs
    that App to hold "Self-hosted runners: Read and write" on the org.
    """
    target = resolve_runner_target(repo_url, org, runner_scope, runner_group)
    console.print(
        f"[blue]Starting GitHub Actions runner for {esc(target.label)}...[/blue]"
    )

    # Check for required environment variables
    app_id = get_env_var("GHA_APP_ID")
    app_private_key = get_env_var("GHA_APP_PRIVATE_KEY")

    ensure_docker_available()
    container_name = get_container_name(runner_name)

    # Check for existing container
    console.print("Checking for existing GitHub Actions runner...")
    result = run_command(
        ["docker", "ps", "-a", "-q", "-f", name_filter(container_name)], check=False
    )

    if result.stdout.strip():
        console.print("Found existing runner container. Removing it...")
        run_command(["docker", "stop", container_name], check=False)
        run_command(["docker", "rm", container_name], check=False)

    # Start new container
    console.print("Starting new GitHub Actions runner container...")
    ensure_image_present(image)
    resolved_local_workdir = resolve_local_workdir(target.id, local_workdir)

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
        f"RUNNER_WORKDIR={runner_workdir}",
        "-e",
        f"DEBUG_OUTPUT={str(debug_output).lower()}",
    ]
    for pair in target.docker_env():
        docker_cmd += ["-e", pair]
    docker_cmd += [
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
    record_mount(target.id, resolved_local_workdir)
    console.print("[green]✓ GitHub Actions runner started successfully[/green]")
    console.print(f"[dim]Container name: {esc(container_name)}[/dim]")
    console.print(f"[dim]Target: {esc(target.label)}[/dim]")
    console.print(f"[dim]Local mount: {esc(resolved_local_workdir)}[/dim]")
    console.print(f"[dim]Mount record: {esc(get_mount_record_path(target.id))}[/dim]")


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
        ["docker", "ps", "-q", "-f", name_filter(container_name)], check=False
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
        ["docker", "ps", "-q", "-f", name_filter(container_name)], check=False
    )

    if running_result.stdout.strip():
        console.print("[green]✓ Runner is running[/green]\n")

        # Get detailed status
        result = run_command(
            [
                "docker",
                "ps",
                "-f",
                name_filter(container_name),
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
                table.add_row(*(cell(part) for part in parts))

            console.print(table)
    else:
        # Check if container exists but is not running
        exists_result = run_command(
            ["docker", "ps", "-a", "-q", "-f", name_filter(container_name)],
            check=False,
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
                    name_filter(container_name),
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
                    table.add_row(*(cell(part) for part in parts))

                console.print(table)
        else:
            console.print("[red]✗ No runner container found[/red]")
