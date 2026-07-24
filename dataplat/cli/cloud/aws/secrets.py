"""AWS Secrets Manager management commands."""

from __future__ import annotations

import json
import os
import sys

import typer
from botocore.exceptions import ClientError
from rich.console import Console
from rich.syntax import Syntax
from rich.table import Table

from dataplat.cli.cloud.aws._common import default_profile, default_region
from dataplat.core.errors import AuthError
from dataplat.services.aws.auth import get_client

app = typer.Typer(
    name="secrets",
    help="Manage AWS Secrets Manager secrets",
    no_args_is_help=True,
)

console = Console()

# ── environment aliases ─────────────────────────────────────────────────────
def _profile_aliases() -> dict[str, str]:
    """Short profile aliases from ``DP_AWS_PROFILE_ALIASES``.

    Format: ``alias=ProfileName,alias2=OtherProfile`` — e.g.
    ``prod=AdminAccess-Prod,qa=AdminAccess-QA``.
    """
    aliases: dict[str, str] = {}
    for chunk in os.getenv("DP_AWS_PROFILE_ALIASES", "").split(","):
        alias, sep, full = chunk.partition("=")
        if sep and alias.strip() and full.strip():
            aliases[alias.strip()] = full.strip()
    return aliases


def _resolve_profile(name: str) -> str:
    """Resolve a short alias to the full AWS profile name."""
    return _profile_aliases().get(name, name)


def _resolve_profiles(profiles: list[str]) -> list[str]:
    """Resolve a list of profile names / aliases, expanding the special 'all' keyword."""
    resolved: list[str] = []
    for p in profiles:
        if p == "all":
            resolved.extend(_profile_aliases().values())
        else:
            resolved.append(_resolve_profile(p))
    # deduplicate while preserving order
    seen: set[str] = set()
    return [p for p in resolved if not (p in seen or seen.add(p))]  # type: ignore[func-returns-value]


def _get_client(profile: str | None = None, region: str | None = None):
    """Return a boto3 Secrets Manager client, triggering SSO login if needed."""
    resolved = _resolve_profile(profile) if profile else default_profile()
    try:
        return get_client(
            service_name="secretsmanager",
            profile=resolved,
            region=region or default_region(),
            notify=lambda msg: console.print(f"[yellow]{msg}[/yellow]"),
        )
    except AuthError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)


def _get_sts_client(profile: str | None = None, region: str | None = None):
    """Return a boto3 STS client, triggering SSO login if needed."""
    resolved = _resolve_profile(profile) if profile else default_profile()
    try:
        return get_client(
            service_name="sts",
            profile=resolved,
            region=region or default_region(),
            notify=lambda msg: console.print(f"[yellow]{msg}[/yellow]"),
        )
    except AuthError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)


# ── shared options ──────────────────────────────────────────────────────────
ProfileOption = typer.Option(
    None,
    "--profile",
    "-p",
    help="AWS profile name or alias (see DP_AWS_PROFILE_ALIASES). "
    "Defaults to DP_AWS_PROFILE.",
)
RegionOption = typer.Option(
    None,
    "--region",
    "-r",
    help="AWS region. Defaults to DP_AWS_REGION/AWS_REGION or the profile's region.",
)
JsonOption = typer.Option(False, "--json", help="Emit JSON instead of a table.")
YesOption = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt.")


def _alias_for(profile: str) -> str:
    """Return the short alias for a full profile name, or the name itself."""
    return next((a for a, p in _profile_aliases().items() if p == profile), profile)


def _confirm_or_abort(summary: str, yes: bool) -> None:
    """Require confirmation before a write. Non-interactive runs need --yes."""
    if yes:
        return
    console.print(summary)
    if sys.stdin.isatty():
        if typer.confirm("Proceed?", default=False):
            return
        console.print("[yellow]Aborted.[/yellow]")
        raise typer.Exit(code=1)
    console.print(
        "[red]Error: refusing to write without confirmation. "
        "Pass --yes/-y in non-interactive contexts.[/red]"
    )
    raise typer.Exit(code=1)


def _confirm_write(action: str, name: str, profs: list[str], yes: bool) -> None:
    aliases = ", ".join(_alias_for(p) for p in profs)
    _confirm_or_abort(
        f"[bold]{action}[/bold] [cyan]{name}[/cyan] in: [yellow]{aliases}[/yellow]",
        yes,
    )


def _client_error_exit(action: str, exc: ClientError) -> None:
    code = exc.response.get("Error", {}).get("Code", "ClientError")
    msg = exc.response.get("Error", {}).get("Message", str(exc))
    console.print(f"[red]{code} on {action}: {msg}[/red]")
    raise typer.Exit(code=1)


def _read_stdin_value() -> str:
    if sys.stdin.isatty():
        console.print("[red]--value-stdin requires piped input[/red]")
        raise typer.Exit(code=1)
    return sys.stdin.read().rstrip("\n")


@app.command("list")
def list_secrets(
    prefix: str | None = typer.Option(
        None, "--prefix", help="Filter secrets whose name starts with this prefix"
    ),
    profile: str | None = ProfileOption,
    region: str | None = RegionOption,
    as_json: bool = JsonOption,
) -> None:
    """List secrets (optionally filtered by name prefix)."""
    client = _get_client(profile, region)

    paginator = client.get_paginator("list_secrets")
    filters = []
    if prefix:
        filters.append({"Key": "name", "Values": [prefix]})

    entries: list[dict] = []
    try:
        for page in (
            paginator.paginate(Filters=filters) if filters else paginator.paginate()
        ):
            for s in page.get("SecretList", []):
                entries.append(
                    {
                        "name": s["Name"],
                        "description": s.get("Description", ""),
                        "last_changed": str(
                            s.get("LastChangedDate", s.get("CreatedDate", ""))
                        ),
                    }
                )
    except ClientError as exc:
        _client_error_exit("list_secrets", exc)

    if as_json:
        typer.echo(json.dumps(entries, indent=2))
        return

    table = Table(title="Secrets")
    table.add_column("Name", style="cyan")
    table.add_column("Description")
    table.add_column("Last Changed", style="dim")
    for entry in entries:
        table.add_row(entry["name"], entry["description"], entry["last_changed"])

    console.print(table)
    console.print(f"\n[dim]{len(entries)} secret(s) found[/dim]")


@app.command("get")
def get_secret(
    name: str = typer.Argument(help="Secret name or ARN"),
    key: str | None = typer.Option(
        None, "--key", "-k", help="Extract a single key from a JSON secret"
    ),
    raw: bool = typer.Option(
        False, "--raw", help="Print the raw value without formatting"
    ),
    profile: str | None = ProfileOption,
    region: str | None = RegionOption,
) -> None:
    """Retrieve and display a secret value."""
    client = _get_client(profile, region)

    try:
        resp = client.get_secret_value(SecretId=name)
    except client.exceptions.ResourceNotFoundException:
        console.print(f"[red]Secret not found: {name}[/red]")
        raise typer.Exit(code=1)
    except ClientError as exc:
        _client_error_exit("get_secret_value", exc)

    value = resp.get("SecretString", "")

    if key:
        try:
            data = json.loads(value)
        except json.JSONDecodeError:
            console.print("[red]Secret is not valid JSON — cannot extract key[/red]")
            raise typer.Exit(code=1)
        if key not in data:
            console.print(f"[red]Key '{key}' not found in secret[/red]")
            raise typer.Exit(code=1)
        value = str(data[key])

    if raw:
        typer.echo(value)
        return

    # try pretty-printing JSON
    try:
        parsed = json.loads(value)
        formatted = json.dumps(parsed, indent=2, ensure_ascii=False)
        console.print(Syntax(formatted, "json", theme="monokai"))
    except json.JSONDecodeError:
        console.print(value)


@app.command("compare")
def compare_secret(
    name: str = typer.Argument(help="Secret name or ARN"),
    profiles: list[str] = typer.Option(
        ["all"],
        "--profile",
        "-p",
        help="AWS profiles/aliases to compare (repeatable). 'all' expands "
        "to every alias in DP_AWS_PROFILE_ALIASES.",
    ),
    region: str | None = RegionOption,
    as_json: bool = JsonOption,
) -> None:
    """Compare a JSON secret across multiple environments side by side.

    Example:

        dp cloud aws secrets compare /my/app/config
        dp cloud aws secrets compare /my/app/config -p prod -p qa
    """
    resolved = _resolve_profiles(profiles)
    env_data: dict[str, dict | None] = {}

    for prof in resolved:
        alias = _alias_for(prof)
        client = _get_client(prof, region)
        try:
            resp = client.get_secret_value(SecretId=name)
            raw = resp.get("SecretString", "")
            try:
                env_data[alias] = json.loads(raw)
            except json.JSONDecodeError:
                console.print(
                    f"[yellow]\\[{alias}] Secret is not JSON — showing raw value[/yellow]"
                )
                env_data[alias] = {"<raw>": raw}
        except client.exceptions.ResourceNotFoundException:
            console.print(f"[yellow]\\[{alias}] Secret not found[/yellow]")
            env_data[alias] = None
        except ClientError as exc:
            _client_error_exit("get_secret_value", exc)

    if as_json:
        typer.echo(json.dumps({"name": name, "environments": env_data}, indent=2))
        return

    # collect all keys across all environments
    all_keys: list[str] = []
    seen: set[str] = set()
    for data in env_data.values():
        if data is not None:
            for k in data:
                if k not in seen:
                    all_keys.append(k)
                    seen.add(k)

    aliases = list(env_data.keys())

    table = Table(title=f"Secret: {name}")
    table.add_column("Key", style="cyan bold")
    for alias in aliases:
        table.add_column(alias, overflow="fold")

    for k in all_keys:
        row_values: list[str] = []
        vals_for_diff: list[str | None] = []
        for alias in aliases:
            data = env_data[alias]
            val = str(data[k]) if data is not None and k in data else None
            vals_for_diff.append(val)
            row_values.append(val if val is not None else "[dim]null[/dim]")

        # highlight differences across environments
        unique = set(v for v in vals_for_diff if v is not None)
        if len(unique) > 1:
            # values differ — colour them
            row_values = [
                f"[red]{v}[/red]" if v != "[dim]null[/dim]" else v for v in row_values
            ]

        table.add_row(k, *row_values)

    console.print(table)


@app.command("set")
def set_secret(
    name: str = typer.Argument(help="Secret name or ARN"),
    value: str | None = typer.Option(
        None,
        "--value",
        "-v",
        help="Plain-text secret value (prefer --value-stdin: argv is visible in ps).",
    ),
    value_stdin: bool = typer.Option(
        False, "--value-stdin", help="Read the secret value from stdin."
    ),
    from_json: str | None = typer.Option(
        None,
        "--from-json",
        "-j",
        help='Set the secret from a JSON string (e.g. \'{"user":"admin"}\')',
    ),
    from_file: str | None = typer.Option(
        None,
        "--from-file",
        "-f",
        help="Read secret value from a file path",
    ),
    description: str | None = typer.Option(
        None, "--description", help="Secret description"
    ),
    profiles: list[str] = typer.Option(
        ["prod"],
        "--profile",
        "-p",
        help="AWS profile/alias (repeatable). Use 'all' for prod+qa.",
    ),
    region: str | None = RegionOption,
    yes: bool = YesOption,
) -> None:
    """Create or update a secret.

    Provide the value with exactly one of --value, --value-stdin, --from-json,
    or --from-file. If the secret already exists it will be updated; otherwise
    it will be created.

    Pass --profile multiple times (or 'all') to target several accounts:

        echo -n "x" | dp cloud aws secrets set my/secret --value-stdin -p prod -p qa
        dp cloud aws secrets set my/secret --from-file value.json -p all
    """
    sources = [value, from_json, from_file, (True if value_stdin else None)]
    provided = sum(1 for s in sources if s is not None)
    if provided != 1:
        console.print(
            "[red]Provide exactly one of --value, --value-stdin, "
            "--from-json, or --from-file[/red]"
        )
        raise typer.Exit(code=1)

    if value_stdin:
        secret_value = _read_stdin_value()
    elif from_file:
        path = os.path.expanduser(from_file)
        try:
            with open(path) as fh:
                secret_value = fh.read()
        except OSError as exc:
            console.print(f"[red]Cannot read file: {exc}[/red]")
            raise typer.Exit(code=1)
    elif from_json:
        try:
            json.loads(from_json)
        except json.JSONDecodeError:
            console.print("[red]--from-json value is not valid JSON[/red]")
            raise typer.Exit(code=1)
        secret_value = from_json
    else:
        secret_value = value  # type: ignore[assignment]

    resolved = _resolve_profiles(profiles)
    _confirm_write("Write secret", name, resolved, yes)

    for prof in resolved:
        client = _get_client(prof, region)
        alias = _alias_for(prof)
        try:
            client.put_secret_value(SecretId=name, SecretString=secret_value)
            if description is not None:
                client.update_secret(SecretId=name, Description=description)
            console.print(f"[green]\\[{alias}] Updated secret:[/green] {name}")
        except client.exceptions.ResourceNotFoundException:
            kwargs: dict = {"Name": name, "SecretString": secret_value}
            if description is not None:
                kwargs["Description"] = description
            client.create_secret(**kwargs)
            console.print(f"[green]\\[{alias}] Created secret:[/green] {name}")
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "ClientError")
            msg = exc.response.get("Error", {}).get("Message", str(exc))
            console.print(f"[red]\\[{alias}] {code} on put_secret_value: {msg}[/red]")


@app.command("edit")
def edit_secret(
    name: str = typer.Argument(help="Secret name or ARN"),
    key: str | None = typer.Option(
        None, "--key", "-k", help="JSON key to add or update"
    ),
    value: str | None = typer.Option(
        None,
        "--value",
        "-v",
        help="New value for the key (prefer --value-stdin: argv is visible in ps).",
    ),
    value_stdin: bool = typer.Option(
        False, "--value-stdin", help="Read the key's new value from stdin."
    ),
    from_file: str | None = typer.Option(
        None,
        "--from-file",
        "-f",
        help="Path to a JSON file whose keys will be merged into the secret "
        "(new keys are added, existing keys are overwritten, untouched keys preserved).",
    ),
    profiles: list[str] = typer.Option(
        ["prod"],
        "--profile",
        "-p",
        help="AWS profile/alias (repeatable). Use 'all' for prod+qa.",
    ),
    region: str | None = RegionOption,
    yes: bool = YesOption,
) -> None:
    """Edit one or more keys inside a JSON secret (read-modify-write).

    Provide either --key with --value/--value-stdin for a single key, or
    --from-file pointing to a JSON object whose keys are merged into the
    existing secret (shallow merge).

    Pass --profile multiple times (or 'all') to target several accounts:

        dp cloud aws secrets edit my/secret -k password --value-stdin -p all < pass.txt
        dp cloud aws secrets edit my/secret --from-file patch.json -p all
    """
    if value_stdin:
        if value is not None:
            console.print("[red]Use either --value or --value-stdin, not both[/red]")
            raise typer.Exit(code=1)
        value = _read_stdin_value()
    using_pair = key is not None or value is not None
    if using_pair and from_file is not None:
        console.print(
            "[red]Use either --key/--value or --from-file, not both[/red]"
        )
        raise typer.Exit(code=1)
    if from_file is None:
        if key is None or value is None:
            console.print(
                "[red]Provide --key and --value, or --from-file with a JSON file[/red]"
            )
            raise typer.Exit(code=1)
        patch: dict = {key: value}
    else:
        path = os.path.expanduser(from_file)
        try:
            with open(path) as fh:
                raw_patch = fh.read()
        except OSError as exc:
            console.print(f"[red]Cannot read file: {exc}[/red]")
            raise typer.Exit(code=1)
        try:
            patch = json.loads(raw_patch)
        except json.JSONDecodeError as exc:
            console.print(f"[red]File is not valid JSON: {exc}[/red]")
            raise typer.Exit(code=1)
        if not isinstance(patch, dict):
            console.print("[red]JSON file must contain an object at the top level[/red]")
            raise typer.Exit(code=1)

    resolved = _resolve_profiles(profiles)
    _confirm_write(
        f"Edit key(s) {', '.join(patch)} in secret", name, resolved, yes
    )

    for prof in resolved:
        alias = _alias_for(prof)
        sts = _get_sts_client(prof, region)
        try:
            ident = sts.get_caller_identity()
            console.print(
                f"[dim]\\[{alias}] Authenticated as {ident.get('Arn')} "
                f"(account {ident.get('Account')})[/dim]"
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "ClientError")
            msg = exc.response.get("Error", {}).get("Message", str(exc))
            console.print(
                f"[red]\\[{alias}] {code} on get_caller_identity: {msg}[/red]"
            )
            continue
        console.print(f"[dim]\\[{alias}] Fetching {name} …[/dim]")
        client = _get_client(prof, region)

        try:
            resp = client.get_secret_value(SecretId=name)
        except client.exceptions.ResourceNotFoundException:
            console.print(f"[red]\\[{alias}] Secret not found: {name}[/red]")
            continue
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "ClientError")
            msg = exc.response.get("Error", {}).get("Message", str(exc))
            console.print(
                f"[red]\\[{alias}] {code} on get_secret_value: {msg}[/red]"
            )
            continue

        raw = resp.get("SecretString", "{}")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            console.print(
                f"[red]\\[{alias}] Existing secret is not valid JSON — cannot edit key[/red]"
            )
            continue

        _MISSING = object()
        added: list[tuple[str, object]] = []
        updated: list[tuple[str, object, object]] = []
        for k, v in patch.items():
            old = data.get(k, _MISSING)
            if old is _MISSING:
                added.append((k, v))
            elif old != v:
                updated.append((k, old, v))
            data[k] = v

        if not added and not updated:
            console.print(
                f"[yellow]\\[{alias}] No changes for[/yellow] [cyan]{name}[/cyan]"
            )
            continue

        try:
            client.put_secret_value(SecretId=name, SecretString=json.dumps(data))
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "ClientError")
            msg = exc.response.get("Error", {}).get("Message", str(exc))
            console.print(
                f"[red]\\[{alias}] {code} on put_secret_value: {msg}[/red]"
            )
            continue
        console.print(
            f"[green]\\[{alias}] Updated[/green] [cyan]{name}[/cyan]  "
            f"({len(added)} added, {len(updated)} changed)"
        )
        # Values are masked: terminal scrollback and CI logs must not hold secrets.
        for k, _v in added:
            console.print(f"  + [bold]{k}[/bold]")
        for k, _old, _v in updated:
            console.print(f"  ~ [bold]{k}[/bold] (changed)")


@app.command("rename-key")
def rename_key(
    name: str = typer.Argument(help="Secret name or ARN"),
    old_key: str = typer.Option(..., "--old-key", help="Existing JSON key to rename"),
    new_key: str = typer.Option(..., "--new-key", help="New name for the key"),
    profiles: list[str] = typer.Option(
        ["prod"],
        "--profile",
        "-p",
        help="AWS profile/alias (repeatable). Use 'all' for prod+qa.",
    ),
    region: str | None = RegionOption,
    yes: bool = YesOption,
) -> None:
    """Rename a key inside a JSON secret (read-modify-write).

    Pass --profile multiple times (or 'all') to target several accounts:

        dp cloud aws secrets rename-key my/secret --old-key user --new-key username -p all
    """
    resolved = _resolve_profiles(profiles)
    _confirm_write(
        f"Rename key '{old_key}' -> '{new_key}' in secret", name, resolved, yes
    )

    for prof in resolved:
        client = _get_client(prof, region)
        alias = _alias_for(prof)

        try:
            resp = client.get_secret_value(SecretId=name)
        except client.exceptions.ResourceNotFoundException:
            console.print(f"[red]\\[{alias}] Secret not found: {name}[/red]")
            continue

        raw = resp.get("SecretString", "{}")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            console.print(
                f"[red]\\[{alias}] Existing secret is not valid JSON — cannot rename key[/red]"
            )
            continue

        if old_key not in data:
            console.print(f"[red]\\[{alias}] Key '{old_key}' not found in secret[/red]")
            continue

        if new_key in data:
            console.print(
                f"[red]\\[{alias}] Key '{new_key}' already exists in secret — aborting to avoid data loss[/red]"
            )
            continue

        # Preserve key order: rebuild dict with the renamed key in the same position
        renamed: dict = {}
        for k, v in data.items():
            renamed[new_key if k == old_key else k] = v

        client.put_secret_value(SecretId=name, SecretString=json.dumps(renamed))
        console.print(f"[green]\\[{alias}] Renamed key[/green] in [cyan]{name}[/cyan]")
        console.print(f"  '{old_key}' → '{new_key}'")


@app.command("delete")
def delete_secret(
    name: str = typer.Argument(help="Secret name or ARN"),
    force: bool = typer.Option(False, "--force", help="Delete without recovery window"),
    profile: str | None = ProfileOption,
    region: str | None = RegionOption,
    yes: bool = YesOption,
) -> None:
    """Schedule a secret for deletion (or force-delete immediately)."""
    resolved_profile = _resolve_profile(profile) if profile else default_profile()
    if force:
        _confirm_or_abort(
            f"[bold red]PERMANENTLY delete[/bold red] [cyan]{name}[/cyan] in "
            f"[yellow]{_alias_for(resolved_profile)}[/yellow] — no recovery window, "
            "cannot be restored.",
            yes,
        )
    else:
        _confirm_write(
            "Schedule deletion (30-day recovery window) of secret",
            name,
            [resolved_profile],
            yes,
        )

    client = _get_client(profile, region)

    kwargs: dict = {"SecretId": name}
    if force:
        kwargs["ForceDeleteWithoutRecovery"] = True
    else:
        kwargs["RecoveryWindowInDays"] = 30

    try:
        client.delete_secret(**kwargs)
    except client.exceptions.ResourceNotFoundException:
        console.print(f"[red]Secret not found: {name}[/red]")
        raise typer.Exit(code=1)
    except ClientError as exc:
        _client_error_exit("delete_secret", exc)

    if force:
        console.print(f"[yellow]Permanently deleted:[/yellow] {name}")
    else:
        console.print(
            f"[yellow]Scheduled for deletion (30-day recovery window):[/yellow] {name}"
        )
        console.print(
            "[dim]Cancel with: dp cloud aws secrets restore " + name + "[/dim]"
        )


@app.command("restore")
def restore_secret(
    name: str = typer.Argument(help="Secret name or ARN"),
    profile: str | None = ProfileOption,
    region: str | None = RegionOption,
) -> None:
    """Cancel a scheduled deletion, restoring the secret."""
    client = _get_client(profile, region)
    try:
        client.restore_secret(SecretId=name)
    except client.exceptions.ResourceNotFoundException:
        console.print(f"[red]Secret not found: {name}[/red]")
        raise typer.Exit(code=1)
    except ClientError as exc:
        _client_error_exit("restore_secret", exc)
    console.print(f"[green]Restored (deletion cancelled):[/green] {name}")


@app.command("describe")
def describe_secret(
    name: str = typer.Argument(help="Secret name or ARN"),
    profile: str | None = ProfileOption,
    region: str | None = RegionOption,
    as_json: bool = JsonOption,
) -> None:
    """Show a secret's metadata: ARN, rotation, timestamps, tags."""
    client = _get_client(profile, region)
    try:
        resp = client.describe_secret(SecretId=name)
    except client.exceptions.ResourceNotFoundException:
        console.print(f"[red]Secret not found: {name}[/red]")
        raise typer.Exit(code=1)
    except ClientError as exc:
        _client_error_exit("describe_secret", exc)

    resp.pop("ResponseMetadata", None)
    if as_json:
        typer.echo(json.dumps(resp, indent=2, default=str))
        return

    table = Table(title=f"Secret: {name}", show_header=False)
    table.add_column("Field", style="cyan", no_wrap=True)
    table.add_column("Value", overflow="fold")
    fields = [
        ("Name", resp.get("Name")),
        ("ARN", resp.get("ARN")),
        ("Description", resp.get("Description")),
        ("KMS key", resp.get("KmsKeyId", "aws/secretsmanager (default)")),
        ("Rotation enabled", resp.get("RotationEnabled", False)),
        ("Rotation lambda", resp.get("RotationLambdaARN")),
        ("Created", resp.get("CreatedDate")),
        ("Last changed", resp.get("LastChangedDate")),
        ("Last accessed", resp.get("LastAccessedDate")),
        ("Deletion date", resp.get("DeletedDate")),
        (
            "Tags",
            ", ".join(
                f"{t.get('Key')}={t.get('Value')}" for t in resp.get("Tags") or []
            ),
        ),
    ]
    for label, val in fields:
        if val not in (None, ""):
            table.add_row(label, str(val))
    console.print(table)


@app.command("versions")
def list_versions(
    name: str = typer.Argument(help="Secret name or ARN"),
    profile: str | None = ProfileOption,
    region: str | None = RegionOption,
    as_json: bool = JsonOption,
) -> None:
    """List a secret's versions with their stages (AWSCURRENT/AWSPREVIOUS)."""
    client = _get_client(profile, region)
    try:
        resp = client.list_secret_version_ids(SecretId=name, IncludeDeprecated=True)
    except client.exceptions.ResourceNotFoundException:
        console.print(f"[red]Secret not found: {name}[/red]")
        raise typer.Exit(code=1)
    except ClientError as exc:
        _client_error_exit("list_secret_version_ids", exc)

    versions = sorted(
        resp.get("Versions", []),
        key=lambda v: str(v.get("CreatedDate", "")),
        reverse=True,
    )
    if as_json:
        payload = [
            {
                "version_id": v.get("VersionId"),
                "stages": v.get("VersionStages", []),
                "created": str(v.get("CreatedDate", "")),
            }
            for v in versions
        ]
        typer.echo(json.dumps(payload, indent=2))
        return

    table = Table(title=f"Versions: {name}")
    table.add_column("Version ID", style="cyan")
    table.add_column("Stages")
    table.add_column("Created", style="dim")
    for v in versions:
        table.add_row(
            v.get("VersionId", ""),
            ", ".join(v.get("VersionStages", [])),
            str(v.get("CreatedDate", "")),
        )
    console.print(table)


@app.command("rollback")
def rollback_secret(
    name: str = typer.Argument(help="Secret name or ARN"),
    profile: str | None = ProfileOption,
    region: str | None = RegionOption,
    yes: bool = YesOption,
) -> None:
    """Roll back to the previous version (move AWSCURRENT to AWSPREVIOUS)."""
    client = _get_client(profile, region)
    try:
        resp = client.list_secret_version_ids(SecretId=name)
    except client.exceptions.ResourceNotFoundException:
        console.print(f"[red]Secret not found: {name}[/red]")
        raise typer.Exit(code=1)
    except ClientError as exc:
        _client_error_exit("list_secret_version_ids", exc)

    current_id: str | None = None
    previous_id: str | None = None
    for v in resp.get("Versions", []):
        stages = v.get("VersionStages", [])
        if "AWSCURRENT" in stages:
            current_id = v.get("VersionId")
        if "AWSPREVIOUS" in stages:
            previous_id = v.get("VersionId")

    if not current_id or not previous_id:
        console.print(
            "[red]Cannot roll back: need both an AWSCURRENT and an "
            "AWSPREVIOUS version.[/red]"
        )
        raise typer.Exit(code=1)

    _confirm_or_abort(
        f"Roll back [cyan]{name}[/cyan]: AWSCURRENT "
        f"{current_id[:8]}… -> {previous_id[:8]}…",
        yes,
    )

    try:
        client.update_secret_version_stage(
            SecretId=name,
            VersionStage="AWSCURRENT",
            MoveToVersionId=previous_id,
            RemoveFromVersionId=current_id,
        )
    except ClientError as exc:
        _client_error_exit("update_secret_version_stage", exc)
    console.print(
        f"[green]Rolled back {name}: AWSCURRENT is now {previous_id}[/green]"
    )
