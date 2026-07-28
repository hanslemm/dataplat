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
from rich.text import Text

from dataplat.cli._exit import fail
from dataplat.cli._options import JsonOption, YesOption
from dataplat.cli._prompt import confirm_or_exit
from dataplat.cli._render import cell, esc
from dataplat.cli.cloud.aws._common import (
    default_region,
    effective_profile,
    profile_aliases,
    profile_option,
    region_option,
    resolve_profiles,
    trace_aws,
)
from dataplat.core.errors import AuthError
from dataplat.services.aws.auth import get_client

app = typer.Typer(
    name="secrets",
    help="Manage AWS Secrets Manager secrets",
    no_args_is_help=True,
)

console = Console()

# Alias resolution lives in _common so `-p prod` means the same thing for rds,
# redshift and secrets. The private spelling stays: this module's call sites —
# and the tests that patch them — address it as a local seam.
_resolve_profiles = resolve_profiles


def _get_client(profile: str | None = None, region: str | None = None):
    """Return a boto3 Secrets Manager client, triggering SSO login if needed.

    Auth failures exit through :func:`dataplat.cli._exit.fail`, so an expired SSO
    session exits 4 here exactly as it does from ``cli_session``. It matters most
    on this command group: a rotation script that fans a write across accounts
    needs to retry "log in again" and stop on "your config is wrong".
    """
    resolved = effective_profile(profile)
    try:
        return get_client(
            service_name="secretsmanager",
            profile=resolved,
            region=region or default_region(),
            notify=lambda msg: console.print(f"[yellow]{esc(msg)}[/yellow]"),
        )
    except AuthError as exc:
        fail(exc, console=console)


def _get_sts_client(profile: str | None = None, region: str | None = None):
    """Return a boto3 STS client, triggering SSO login if needed."""
    resolved = effective_profile(profile)
    try:
        return get_client(
            service_name="sts",
            profile=resolved,
            region=region or default_region(),
            notify=lambda msg: console.print(f"[yellow]{esc(msg)}[/yellow]"),
        )
    except AuthError as exc:
        fail(exc, console=console)


def _trace_call(
    operation: str,
    *,
    profile: str | None = None,
    region: str | None = None,
    **fields: object,
) -> None:
    """Trace one Secrets Manager call — the secret's name, never its value.

    This module reads secret values for a living, so the line has to be drawn
    explicitly rather than left to the redactor: the trace records *that* a value
    was fetched or written and *which* secret it belonged to. Not the contents,
    and not their length either — for a password, a length is a real hint, and
    "48 chars" would be a credential detail sitting in a CI log.

    The name is passed as ``name=``, never ``secret=``: :func:`redact` masks the
    value after any credential-shaped key, and would blank out the one field that
    makes the trace worth reading.
    """
    trace_aws("secretsmanager", operation, profile=profile, region=region, **fields)


# ── shared options ──────────────────────────────────────────────────────────
ProfileOption = profile_option()
RegionOption = region_option()


def _alias_for(profile: str) -> str:
    """Return the short alias for a full profile name, or the name itself."""
    return next((a for a, p in profile_aliases().items() if p == profile), profile)


def _confirm_or_abort(summary: str, yes: bool) -> None:
    """Require confirmation before a write. Non-interactive runs need --yes.

    ``summary`` is markup: escape any interpolated secret name, key or alias.
    """
    confirm_or_exit(summary, yes=yes, console=console)


def _confirm_write(action: str, name: str, profs: list[str], yes: bool) -> None:
    """Confirm ``action`` on ``name`` in every targeted profile.

    ``action`` is plain text — callers build it from key names, which is why it
    is escaped here rather than trusted.
    """
    aliases = ", ".join(_alias_for(p) for p in profs)
    _confirm_or_abort(
        f"[bold]{esc(action)}[/bold] [cyan]{esc(name)}[/cyan] in: "
        f"[yellow]{esc(aliases)}[/yellow]",
        yes,
    )


def _client_error_exit(action: str, exc: ClientError) -> None:
    code = exc.response.get("Error", {}).get("Code", "ClientError")
    msg = exc.response.get("Error", {}).get("Message", str(exc))
    console.print(f"[red]{esc(code)} on {action}: {esc(msg)}[/red]")
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
    _trace_call(
        "list_secrets", profile=profile, region=region, prefix=prefix or "(none)"
    )

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
        table.add_row(
            cell(entry["name"]),
            cell(entry["description"]),
            cell(entry["last_changed"]),
        )

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

    _trace_call("get_secret_value", profile=profile, region=region, name=name)
    try:
        resp = client.get_secret_value(SecretId=name)
    except client.exceptions.ResourceNotFoundException:
        console.print(f"[red]Secret not found: {esc(name)}[/red]")
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
            console.print(f"[red]Key '{esc(key)}' not found in secret[/red]")
            raise typer.Exit(code=1)
        value = str(data[key])

    if raw:
        typer.echo(value)
        return

    # try pretty-printing JSON
    try:
        parsed = json.loads(value)
        formatted = json.dumps(parsed, indent=2, ensure_ascii=False)
        # Syntax renders its source literally; escaping here would show the
        # backslashes as part of the secret.
        console.print(Syntax(formatted, "json", theme="monokai"))
    except json.JSONDecodeError:
        # A secret value is the most hostile string in the codebase: printing it
        # as markup drops [bold] silently and crashes on [/anything].
        console.print(cell(value))


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
        # Per profile, because "compare" is exactly the command where knowing
        # which account answered what is the point.
        _trace_call("get_secret_value", profile=prof, region=region, name=name)
        try:
            resp = client.get_secret_value(SecretId=name)
            raw = resp.get("SecretString", "")
            try:
                env_data[alias] = json.loads(raw)
            except json.JSONDecodeError:
                console.print(
                    f"[yellow]\\[{esc(alias)}] Secret is not JSON — "
                    "showing raw value[/yellow]"
                )
                env_data[alias] = {"<raw>": raw}
        except client.exceptions.ResourceNotFoundException:
            console.print(f"[yellow]\\[{esc(alias)}] Secret not found[/yellow]")
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

    table = Table(title=f"Secret: {esc(name)}")
    table.add_column("Key", style="cyan bold")
    for alias in aliases:
        # The alias comes from DP_AWS_PROFILE_ALIASES, so even the header is
        # data we did not author.
        table.add_column(cell(alias), overflow="fold")

    for k in all_keys:
        vals_for_diff: list[str | None] = []
        for alias in aliases:
            data = env_data[alias]
            vals_for_diff.append(
                str(data[k]) if data is not None and k in data else None
            )

        # highlight differences across environments
        unique = {v for v in vals_for_diff if v is not None}
        differs = len(unique) > 1
        row_values = [
            Text("null", style="dim")
            if val is None
            else cell(val, style="red" if differs else "")
            for val in vals_for_diff
        ]

        table.add_row(cell(k), *row_values)

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
            console.print(f"[red]Cannot read file: {esc(exc)}[/red]")
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
        _trace_call("put_secret_value", profile=prof, region=region, name=name)
        try:
            client.put_secret_value(SecretId=name, SecretString=secret_value)
            if description is not None:
                _trace_call("update_secret", profile=prof, region=region, name=name)
                client.update_secret(SecretId=name, Description=description)
            console.print(
                f"[green]\\[{esc(alias)}] Updated secret:[/green] {esc(name)}"
            )
        except client.exceptions.ResourceNotFoundException:
            kwargs: dict = {"Name": name, "SecretString": secret_value}
            if description is not None:
                kwargs["Description"] = description
            _trace_call("create_secret", profile=prof, region=region, name=name)
            client.create_secret(**kwargs)
            console.print(
                f"[green]\\[{esc(alias)}] Created secret:[/green] {esc(name)}"
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "ClientError")
            msg = exc.response.get("Error", {}).get("Message", str(exc))
            console.print(
                f"[red]\\[{esc(alias)}] {esc(code)} on put_secret_value: "
                f"{esc(msg)}[/red]"
            )


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
        "(new keys are added, existing keys are overwritten, untouched keys "
        "preserved).",
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
        console.print("[red]Use either --key/--value or --from-file, not both[/red]")
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
            console.print(f"[red]Cannot read file: {esc(exc)}[/red]")
            raise typer.Exit(code=1)
        try:
            patch = json.loads(raw_patch)
        except json.JSONDecodeError as exc:
            console.print(f"[red]File is not valid JSON: {esc(exc)}[/red]")
            raise typer.Exit(code=1)
        if not isinstance(patch, dict):
            console.print(
                "[red]JSON file must contain an object at the top level[/red]"
            )
            raise typer.Exit(code=1)

    resolved = _resolve_profiles(profiles)
    _confirm_write(f"Edit key(s) {', '.join(patch)} in secret", name, resolved, yes)

    for prof in resolved:
        alias = _alias_for(prof)
        sts = _get_sts_client(prof, region)
        trace_aws("sts", "get_caller_identity", profile=prof, region=region)
        try:
            ident = sts.get_caller_identity()
            console.print(
                f"[dim]\\[{esc(alias)}] Authenticated as {esc(ident.get('Arn'))} "
                f"(account {esc(ident.get('Account'))})[/dim]"
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "ClientError")
            msg = exc.response.get("Error", {}).get("Message", str(exc))
            console.print(
                f"[red]\\[{esc(alias)}] {esc(code)} on get_caller_identity: "
                f"{esc(msg)}[/red]"
            )
            continue
        console.print(f"[dim]\\[{esc(alias)}] Fetching {esc(name)} …[/dim]")
        client = _get_client(prof, region)

        _trace_call("get_secret_value", profile=prof, region=region, name=name)
        try:
            resp = client.get_secret_value(SecretId=name)
        except client.exceptions.ResourceNotFoundException:
            console.print(f"[red]\\[{esc(alias)}] Secret not found: {esc(name)}[/red]")
            continue
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "ClientError")
            msg = exc.response.get("Error", {}).get("Message", str(exc))
            console.print(
                f"[red]\\[{esc(alias)}] {esc(code)} on get_secret_value: "
                f"{esc(msg)}[/red]"
            )
            continue

        raw = resp.get("SecretString", "{}")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            console.print(
                f"[red]\\[{esc(alias)}] Existing secret is not valid JSON — "
                "cannot edit key[/red]"
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
                f"[yellow]\\[{esc(alias)}] No changes for[/yellow] "
                f"[cyan]{esc(name)}[/cyan]"
            )
            continue

        # Key names, not values: the same keys are already printed to stdout
        # below, and they are what identifies the edit in a fan-out run.
        _trace_call(
            "put_secret_value",
            profile=prof,
            region=region,
            name=name,
            keys=",".join(patch),
        )
        try:
            client.put_secret_value(SecretId=name, SecretString=json.dumps(data))
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "ClientError")
            msg = exc.response.get("Error", {}).get("Message", str(exc))
            console.print(
                f"[red]\\[{esc(alias)}] {esc(code)} on put_secret_value: "
                f"{esc(msg)}[/red]"
            )
            continue
        console.print(
            f"[green]\\[{esc(alias)}] Updated[/green] [cyan]{esc(name)}[/cyan]  "
            f"({len(added)} added, {len(updated)} changed)"
        )
        # Values are masked: terminal scrollback and CI logs must not hold secrets.
        for k, _v in added:
            console.print(f"  + [bold]{esc(k)}[/bold]")
        for k, _old, _v in updated:
            console.print(f"  ~ [bold]{esc(k)}[/bold] (changed)")


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

        dp cloud aws secrets rename-key my/secret \\
            --old-key user --new-key username -p all
    """
    resolved = _resolve_profiles(profiles)
    _confirm_write(
        f"Rename key '{old_key}' -> '{new_key}' in secret", name, resolved, yes
    )

    for prof in resolved:
        client = _get_client(prof, region)
        alias = _alias_for(prof)

        _trace_call("get_secret_value", profile=prof, region=region, name=name)
        try:
            resp = client.get_secret_value(SecretId=name)
        except client.exceptions.ResourceNotFoundException:
            console.print(f"[red]\\[{esc(alias)}] Secret not found: {esc(name)}[/red]")
            continue

        raw = resp.get("SecretString", "{}")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            console.print(
                f"[red]\\[{esc(alias)}] Existing secret is not valid JSON — "
                "cannot rename key[/red]"
            )
            continue

        if old_key not in data:
            console.print(
                f"[red]\\[{esc(alias)}] Key '{esc(old_key)}' not found in secret[/red]"
            )
            continue

        if new_key in data:
            console.print(
                f"[red]\\[{esc(alias)}] Key '{esc(new_key)}' already exists in "
                "secret — aborting to avoid data loss[/red]"
            )
            continue

        # Preserve key order: rebuild dict with the renamed key in the same position
        renamed: dict = {}
        for k, v in data.items():
            renamed[new_key if k == old_key else k] = v

        _trace_call(
            "put_secret_value",
            profile=prof,
            region=region,
            name=name,
            keys=f"{old_key}->{new_key}",
        )
        client.put_secret_value(SecretId=name, SecretString=json.dumps(renamed))
        console.print(
            f"[green]\\[{esc(alias)}] Renamed key[/green] in [cyan]{esc(name)}[/cyan]"
        )
        console.print(f"  '{esc(old_key)}' → '{esc(new_key)}'")


@app.command("delete")
def delete_secret(
    name: str = typer.Argument(help="Secret name or ARN"),
    force: bool = typer.Option(False, "--force", help="Delete without recovery window"),
    profile: str | None = ProfileOption,
    region: str | None = RegionOption,
    yes: bool = YesOption,
) -> None:
    """Schedule a secret for deletion (or force-delete immediately)."""
    resolved_profile = effective_profile(profile)
    if force:
        _confirm_or_abort(
            f"[bold red]PERMANENTLY delete[/bold red] [cyan]{esc(name)}[/cyan] in "
            f"[yellow]{esc(_alias_for(resolved_profile))}[/yellow] — no recovery "
            "window, cannot be restored.",
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

    _trace_call(
        "delete_secret",
        profile=profile,
        region=region,
        name=name,
        # Which of the two deletions was sent is the whole question afterwards.
        recovery="none" if force else "30d",
    )
    try:
        client.delete_secret(**kwargs)
    except client.exceptions.ResourceNotFoundException:
        console.print(f"[red]Secret not found: {esc(name)}[/red]")
        raise typer.Exit(code=1)
    except ClientError as exc:
        _client_error_exit("delete_secret", exc)

    if force:
        console.print(f"[yellow]Permanently deleted:[/yellow] {esc(name)}")
    else:
        console.print(
            "[yellow]Scheduled for deletion (30-day recovery window):[/yellow] "
            f"{esc(name)}"
        )
        console.print(
            f"[dim]Cancel with: dp cloud aws secrets restore {esc(name)}[/dim]"
        )


@app.command("restore")
def restore_secret(
    name: str = typer.Argument(help="Secret name or ARN"),
    profile: str | None = ProfileOption,
    region: str | None = RegionOption,
) -> None:
    """Cancel a scheduled deletion, restoring the secret."""
    client = _get_client(profile, region)
    _trace_call("restore_secret", profile=profile, region=region, name=name)
    try:
        client.restore_secret(SecretId=name)
    except client.exceptions.ResourceNotFoundException:
        console.print(f"[red]Secret not found: {esc(name)}[/red]")
        raise typer.Exit(code=1)
    except ClientError as exc:
        _client_error_exit("restore_secret", exc)
    console.print(f"[green]Restored (deletion cancelled):[/green] {esc(name)}")


@app.command("describe")
def describe_secret(
    name: str = typer.Argument(help="Secret name or ARN"),
    profile: str | None = ProfileOption,
    region: str | None = RegionOption,
    as_json: bool = JsonOption,
) -> None:
    """Show a secret's metadata: ARN, rotation, timestamps, tags."""
    client = _get_client(profile, region)
    _trace_call("describe_secret", profile=profile, region=region, name=name)
    try:
        resp = client.describe_secret(SecretId=name)
    except client.exceptions.ResourceNotFoundException:
        console.print(f"[red]Secret not found: {esc(name)}[/red]")
        raise typer.Exit(code=1)
    except ClientError as exc:
        _client_error_exit("describe_secret", exc)

    resp.pop("ResponseMetadata", None)
    if as_json:
        typer.echo(json.dumps(resp, indent=2, default=str))
        return

    table = Table(title=f"Secret: {esc(name)}", show_header=False)
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
            table.add_row(label, cell(val))
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
    _trace_call(
        "list_secret_version_ids",
        profile=profile,
        region=region,
        name=name,
        deprecated="included",
    )
    try:
        resp = client.list_secret_version_ids(SecretId=name, IncludeDeprecated=True)
    except client.exceptions.ResourceNotFoundException:
        console.print(f"[red]Secret not found: {esc(name)}[/red]")
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

    table = Table(title=f"Versions: {esc(name)}")
    table.add_column("Version ID", style="cyan")
    table.add_column("Stages")
    table.add_column("Created", style="dim")
    for v in versions:
        table.add_row(
            cell(v.get("VersionId", "")),
            cell(", ".join(v.get("VersionStages", []))),
            cell(v.get("CreatedDate", "")),
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
    _trace_call("list_secret_version_ids", profile=profile, region=region, name=name)
    try:
        resp = client.list_secret_version_ids(SecretId=name)
    except client.exceptions.ResourceNotFoundException:
        console.print(f"[red]Secret not found: {esc(name)}[/red]")
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
        f"Roll back [cyan]{esc(name)}[/cyan]: AWSCURRENT "
        f"{esc(current_id[:8])}… -> {esc(previous_id[:8])}…",
        yes,
    )

    _trace_call(
        "update_secret_version_stage",
        profile=profile,
        region=region,
        name=name,
        stage="AWSCURRENT",
        # Version ids are opaque handles, not values; which two were swapped is
        # the only thing that makes a rollback reviewable after the fact.
        moved=f"{current_id}->{previous_id}",
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
        f"[green]Rolled back {esc(name)}: AWSCURRENT is now {esc(previous_id)}[/green]"
    )
