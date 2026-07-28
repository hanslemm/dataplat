from __future__ import annotations

import json

import pytest
from botocore.exceptions import ClientError
from rich.console import Console
from typer.testing import CliRunner

from dataplat.cli.cloud.aws import secrets as secrets_cli
from dataplat.core.errors import AuthError, ExitCode
from dataplat.core.trace import verbose

runner = CliRunner()

# A value that used to break both ways: the closing tag raised MarkupError and
# killed the command, the style name was swallowed and misreported the data.
HOSTILE = "closes [/x] 42 and [bold]kept[/bold]"


@pytest.fixture
def wide(monkeypatch: pytest.MonkeyPatch) -> None:
    """Render wide and unstyled so assertions do not depend on the terminal."""
    monkeypatch.setattr(
        secrets_cli,
        "console",
        Console(width=400, no_color=True, legacy_windows=False),
    )


class _FakeNotFound(Exception):
    pass


class _FakeExceptions:
    ResourceNotFoundException = _FakeNotFound


class _FakeClient:
    def __init__(self, initial: dict | str) -> None:
        self._stored = initial if isinstance(initial, str) else json.dumps(initial)
        self.put_calls: list[str] = []
        self.exceptions = _FakeExceptions()

    def get_secret_value(self, SecretId: str) -> dict:
        return {"SecretString": self._stored}

    def put_secret_value(self, SecretId: str, SecretString: str) -> None:
        self.put_calls.append(SecretString)
        self._stored = SecretString


class _FakeStsClient:
    def get_caller_identity(self) -> dict:
        return {
            "Arn": "arn:aws:sts::000000000000:assumed-role/Test/test@example.com",
            "Account": "000000000000",
            "UserId": "AIDA",
        }


def _patch_client(monkeypatch, client: _FakeClient) -> None:
    monkeypatch.setattr(
        secrets_cli, "_get_client", lambda profile=None, region=None: client
    )
    monkeypatch.setattr(
        secrets_cli,
        "_get_sts_client",
        lambda profile=None, region=None: _FakeStsClient(),
    )
    monkeypatch.setattr(
        secrets_cli, "_resolve_profiles", lambda profiles: ["prod-profile"]
    )


def test_edit_from_file_merges_keys(tmp_path, monkeypatch) -> None:
    client = _FakeClient({"existing": "old", "keep": "untouched"})
    _patch_client(monkeypatch, client)

    patch = tmp_path / "patch.json"
    patch.write_text(json.dumps({"existing": "new", "added": "value"}))

    result = runner.invoke(
        secrets_cli.app,
        ["edit", "--yes", "my/secret", "--from-file", str(patch)],
    )

    assert result.exit_code == 0, result.stdout
    assert len(client.put_calls) == 1
    written = json.loads(client.put_calls[0])
    assert written == {"existing": "new", "keep": "untouched", "added": "value"}
    assert "+ added" in result.stdout
    assert "~ existing" in result.stdout
    assert "keep" not in result.stdout.split("Updated", 1)[1]


def test_edit_from_file_no_changes_skips_put(tmp_path, monkeypatch) -> None:
    client = _FakeClient({"a": "1"})
    _patch_client(monkeypatch, client)

    patch = tmp_path / "patch.json"
    patch.write_text(json.dumps({"a": "1"}))

    result = runner.invoke(
        secrets_cli.app,
        ["edit", "--yes", "my/secret", "--from-file", str(patch)],
    )

    assert result.exit_code == 0, result.stdout
    assert client.put_calls == []
    assert "No changes" in result.stdout


def test_edit_rejects_both_pair_and_file(tmp_path, monkeypatch) -> None:
    client = _FakeClient({"a": "1"})
    _patch_client(monkeypatch, client)

    patch = tmp_path / "patch.json"
    patch.write_text("{}")

    result = runner.invoke(
        secrets_cli.app,
        [
            "edit",
            "my/secret",
            "-k",
            "a",
            "-v",
            "2",
            "--from-file",
            str(patch),
        ],
    )

    assert result.exit_code == 1
    assert "not both" in result.stdout


def test_edit_rejects_missing_inputs(monkeypatch) -> None:
    client = _FakeClient({"a": "1"})
    _patch_client(monkeypatch, client)

    result = runner.invoke(secrets_cli.app, ["edit", "--yes", "my/secret"])

    assert result.exit_code == 1
    assert "Provide --key and --value" in result.stdout


def test_edit_from_file_rejects_non_object(tmp_path, monkeypatch) -> None:
    client = _FakeClient({"a": "1"})
    _patch_client(monkeypatch, client)

    patch = tmp_path / "patch.json"
    patch.write_text("[1, 2, 3]")

    result = runner.invoke(
        secrets_cli.app,
        ["edit", "--yes", "my/secret", "--from-file", str(patch)],
    )

    assert result.exit_code == 1
    assert "object at the top level" in result.stdout


def test_edit_handles_access_denied_on_get(tmp_path, monkeypatch) -> None:
    class _DeniedClient(_FakeClient):
        def get_secret_value(self, SecretId: str) -> dict:
            raise ClientError(
                {
                    "Error": {
                        "Code": "AccessDeniedException",
                        "Message": "not authorized",
                    }
                },
                "GetSecretValue",
            )

    client = _DeniedClient({})
    _patch_client(monkeypatch, client)

    patch = tmp_path / "patch.json"
    patch.write_text(json.dumps({"a": "1"}))

    result = runner.invoke(
        secrets_cli.app,
        ["edit", "--yes", "my/secret", "--from-file", str(patch)],
    )

    assert result.exit_code == 0, result.stdout
    assert "AccessDeniedException" in result.stdout
    assert "not authorized" in result.stdout
    assert client.put_calls == []


def test_edit_handles_access_denied_on_put(tmp_path, monkeypatch) -> None:
    class _DeniedPutClient(_FakeClient):
        def put_secret_value(self, SecretId: str, SecretString: str) -> None:
            raise ClientError(
                {
                    "Error": {
                        "Code": "AccessDeniedException",
                        "Message": "no write",
                    }
                },
                "PutSecretValue",
            )

    client = _DeniedPutClient({"a": "1"})
    _patch_client(monkeypatch, client)

    patch = tmp_path / "patch.json"
    patch.write_text(json.dumps({"a": "2"}))

    result = runner.invoke(
        secrets_cli.app,
        ["edit", "--yes", "my/secret", "--from-file", str(patch)],
    )

    assert result.exit_code == 0, result.stdout
    assert "AccessDeniedException" in result.stdout
    assert "put_secret_value" in result.stdout


def test_edit_single_key_still_works(monkeypatch) -> None:
    client = _FakeClient({"a": "1"})
    _patch_client(monkeypatch, client)

    result = runner.invoke(
        secrets_cli.app,
        ["edit", "--yes", "my/secret", "-k", "a", "-v", "2"],
    )

    assert result.exit_code == 0, result.stdout
    written = json.loads(client.put_calls[0])
    assert written == {"a": "2"}


def test_edit_prints_caller_identity(tmp_path, monkeypatch) -> None:
    client = _FakeClient({"a": "1"})
    _patch_client(monkeypatch, client)

    patch = tmp_path / "patch.json"
    patch.write_text(json.dumps({"a": "2"}))

    result = runner.invoke(
        secrets_cli.app,
        ["edit", "--yes", "my/secret", "--from-file", str(patch)],
    )

    assert result.exit_code == 0, result.stdout
    assert "Authenticated as" in result.stdout
    assert "test@example.com" in result.stdout
    assert "000000000000" in result.stdout


def test_set_refuses_without_yes_non_interactive(monkeypatch) -> None:
    client = _FakeClient({})
    _patch_client(monkeypatch, client)

    result = runner.invoke(secrets_cli.app, ["set", "my/secret", "--value", "x"])

    assert result.exit_code == 1
    assert "--yes" in result.stdout
    assert client.put_calls == []


def test_set_with_yes_writes(monkeypatch) -> None:
    client = _FakeClient({})
    _patch_client(monkeypatch, client)

    result = runner.invoke(
        secrets_cli.app, ["set", "--yes", "my/secret", "--value", "x"]
    )

    assert result.exit_code == 0, result.stdout
    assert client.put_calls == ["x"]


def test_set_value_stdin(monkeypatch) -> None:
    client = _FakeClient({})
    _patch_client(monkeypatch, client)

    result = runner.invoke(
        secrets_cli.app,
        ["set", "--yes", "my/secret", "--value-stdin"],
        input="from-stdin\n",
    )

    assert result.exit_code == 0, result.stdout
    assert client.put_calls == ["from-stdin"]


def test_edit_masks_values_in_output(tmp_path, monkeypatch) -> None:
    client = _FakeClient({"password": "old-secret"})
    _patch_client(monkeypatch, client)

    patch = tmp_path / "patch.json"
    patch.write_text(json.dumps({"password": "new-secret-value"}))

    result = runner.invoke(
        secrets_cli.app,
        ["edit", "--yes", "my/secret", "--from-file", str(patch)],
    )

    assert result.exit_code == 0, result.stdout
    assert "new-secret-value" not in result.stdout
    assert "old-secret" not in result.stdout
    assert "~ password" in result.stdout


def test_rollback_moves_current_stage(monkeypatch) -> None:
    class _VersionedClient(_FakeClient):
        def __init__(self) -> None:
            super().__init__({})
            self.stage_calls: list[dict] = []

        def list_secret_version_ids(self, SecretId: str, **kwargs) -> dict:
            return {
                "Versions": [
                    {"VersionId": "v-current", "VersionStages": ["AWSCURRENT"]},
                    {"VersionId": "v-previous", "VersionStages": ["AWSPREVIOUS"]},
                ]
            }

        def update_secret_version_stage(self, **kwargs) -> None:
            self.stage_calls.append(kwargs)

    client = _VersionedClient()
    _patch_client(monkeypatch, client)

    result = runner.invoke(secrets_cli.app, ["rollback", "--yes", "my/secret"])

    assert result.exit_code == 0, result.stdout
    assert client.stage_calls == [
        {
            "SecretId": "my/secret",
            "VersionStage": "AWSCURRENT",
            "MoveToVersionId": "v-previous",
            "RemoveFromVersionId": "v-current",
        }
    ]


def test_delete_force_refuses_without_yes(monkeypatch) -> None:
    class _DeleteClient(_FakeClient):
        def __init__(self) -> None:
            super().__init__({})
            self.deleted: list[dict] = []

        def delete_secret(self, **kwargs) -> None:
            self.deleted.append(kwargs)

    client = _DeleteClient()
    _patch_client(monkeypatch, client)

    result = runner.invoke(secrets_cli.app, ["delete", "my/secret", "--force"])

    assert result.exit_code == 1
    assert client.deleted == []


# ── markup safety ───────────────────────────────────────────────────────────


def test_get_prints_non_json_value_verbatim(monkeypatch, wide) -> None:
    """The worst case in the repo: a secret value printed on its own."""
    _patch_client(monkeypatch, _FakeClient(HOSTILE))

    result = runner.invoke(secrets_cli.app, ["get", "my/secret"])

    assert result.exit_code == 0, result.stdout
    assert "[/x]" in result.stdout
    assert "[bold]kept[/bold]" in result.stdout


def test_get_json_value_keeps_brackets(monkeypatch, wide) -> None:
    """Syntax renders its source literally — it must not be escaped twice."""
    _patch_client(monkeypatch, _FakeClient({"token": "[/x] [bold]"}))

    result = runner.invoke(secrets_cli.app, ["get", "my/secret"])

    assert result.exit_code == 0, result.stdout
    assert "[/x] [bold]" in result.stdout
    assert "\\[" not in result.stdout


def test_get_raw_stays_byte_identical(monkeypatch, wide) -> None:
    _patch_client(monkeypatch, _FakeClient(HOSTILE))

    result = runner.invoke(secrets_cli.app, ["get", "my/secret", "--raw"])

    assert result.exit_code == 0, result.stdout
    assert result.stdout == HOSTILE + "\n"


def test_get_missing_key_escapes_the_key(monkeypatch, wide) -> None:
    _patch_client(monkeypatch, _FakeClient({"a": "1"}))

    result = runner.invoke(secrets_cli.app, ["get", "my/secret", "-k", "no[/x]such"])

    assert result.exit_code == 1
    assert "no[/x]such" in result.stdout


def test_get_not_found_escapes_the_name(monkeypatch, wide) -> None:
    class _MissingClient(_FakeClient):
        def get_secret_value(self, SecretId: str) -> dict:
            raise _FakeNotFound

    _patch_client(monkeypatch, _MissingClient({}))

    result = runner.invoke(secrets_cli.app, ["get", "my/[/x]"])

    assert result.exit_code == 1
    assert "Secret not found: my/[/x]" in result.stdout


def test_client_error_message_is_not_markup(monkeypatch, wide) -> None:
    class _BrokenClient(_FakeClient):
        def get_secret_value(self, SecretId: str) -> dict:
            raise ClientError(
                {"Error": {"Code": "AccessDenied[/x]", "Message": HOSTILE}},
                "GetSecretValue",
            )

    _patch_client(monkeypatch, _BrokenClient({}))

    result = runner.invoke(secrets_cli.app, ["get", "my/secret"])

    assert result.exit_code == 1
    assert "AccessDenied[/x]" in result.stdout
    assert "[bold]kept[/bold]" in result.stdout


def test_list_renders_hostile_names_and_descriptions(monkeypatch, wide) -> None:
    class _Paginator:
        def paginate(self, **kwargs):
            return [
                {
                    "SecretList": [
                        {
                            "Name": "app/[/x]",
                            "Description": "[bold]desc[/bold]",
                            "LastChangedDate": "2026-01-01",
                        }
                    ]
                }
            ]

    class _ListClient(_FakeClient):
        def get_paginator(self, name: str) -> _Paginator:
            return _Paginator()

    _patch_client(monkeypatch, _ListClient({}))

    result = runner.invoke(secrets_cli.app, ["list"])

    assert result.exit_code == 0, result.stdout
    assert "app/[/x]" in result.stdout
    assert "[bold]desc[/bold]" in result.stdout


def test_list_json_stays_machine_readable(monkeypatch, wide) -> None:
    class _Paginator:
        def paginate(self, **kwargs):
            return [{"SecretList": [{"Name": "app/[/x]", "Description": ""}]}]

    class _ListClient(_FakeClient):
        def get_paginator(self, name: str) -> _Paginator:
            return _Paginator()

    _patch_client(monkeypatch, _ListClient({}))

    result = runner.invoke(secrets_cli.app, ["list", "--json"])

    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout)[0]["name"] == "app/[/x]"


def test_compare_renders_hostile_aliases_keys_and_values(monkeypatch, wide) -> None:
    monkeypatch.setenv("DP_AWS_PROFILE_ALIASES", "pr[/x]d=Admin-Prod")
    values = [
        json.dumps({"k[/x]": "left[bold]b[/bold]"}),
        json.dumps({"k[/x]": "right"}),
    ]

    class _CompareClient(_FakeClient):
        def get_secret_value(self, SecretId: str) -> dict:
            return {"SecretString": values.pop(0)}

    client = _CompareClient({})
    monkeypatch.setattr(
        secrets_cli, "_get_client", lambda profile=None, region=None: client
    )
    monkeypatch.setattr(
        secrets_cli, "_resolve_profiles", lambda profiles: ["Admin-Prod", "Admin-QA"]
    )

    result = runner.invoke(secrets_cli.app, ["compare", "sec[/x]ret"])

    assert result.exit_code == 0, result.stdout
    # alias column header, key column, and the differing (red) values
    assert "pr[/x]d" in result.stdout
    assert "k[/x]" in result.stdout
    assert "left[bold]b[/bold]" in result.stdout
    assert "sec[/x]ret" in result.stdout


def test_compare_marks_missing_keys_as_null(monkeypatch, wide) -> None:
    values = [json.dumps({"a": "1"}), json.dumps({})]

    class _CompareClient(_FakeClient):
        def get_secret_value(self, SecretId: str) -> dict:
            return {"SecretString": values.pop(0)}

    client = _CompareClient({})
    monkeypatch.setattr(
        secrets_cli, "_get_client", lambda profile=None, region=None: client
    )
    monkeypatch.setattr(
        secrets_cli, "_resolve_profiles", lambda profiles: ["Admin-Prod", "Admin-QA"]
    )

    result = runner.invoke(secrets_cli.app, ["compare", "my/secret"])

    assert result.exit_code == 0, result.stdout
    assert "null" in result.stdout


def test_describe_renders_hostile_metadata(monkeypatch, wide) -> None:
    class _DescribeClient(_FakeClient):
        def describe_secret(self, SecretId: str) -> dict:
            return {
                "Name": "app/[/x]",
                "ARN": "arn:aws:secretsmanager:::secret/app",
                "Description": HOSTILE,
                "Tags": [{"Key": "owner[/x]", "Value": "[bold]team[/bold]"}],
            }

    _patch_client(monkeypatch, _DescribeClient({}))

    result = runner.invoke(secrets_cli.app, ["describe", "app/[/x]"])

    assert result.exit_code == 0, result.stdout
    assert "app/[/x]" in result.stdout
    assert "[bold]kept[/bold]" in result.stdout
    assert "owner[/x]=[bold]team[/bold]" in result.stdout


# ── profile aliases ─────────────────────────────────────────────────────────
# The alias logic moved to _common so rds and redshift honour it too; these pin
# the behaviour secrets already had, on both the single- and multi-profile paths.


def test_single_profile_command_resolves_the_alias(monkeypatch, wide) -> None:
    """`-p prod` must reach boto3 as the full profile name, never the alias."""
    monkeypatch.setenv("DP_AWS_PROFILE_ALIASES", "prod=AdminAccess-Prod")
    seen: dict[str, object] = {}

    def _record(*, service_name: str, profile=None, region=None, notify=None):
        seen["profile"] = profile
        return _FakeClient({"a": "1"})

    monkeypatch.setattr(secrets_cli, "get_client", _record)

    result = runner.invoke(secrets_cli.app, ["get", "my/secret", "-p", "prod"])

    assert result.exit_code == 0, result.stdout
    assert seen["profile"] == "AdminAccess-Prod"


def test_multi_profile_command_still_expands_all(monkeypatch, wide) -> None:
    """'all' fans a write out to every alias, in declaration order."""
    monkeypatch.setenv("DP_AWS_PROFILE_ALIASES", "prod=Admin-Prod,qa=Admin-QA")
    clients: dict[str | None, _FakeClient] = {}

    def _record(profile=None, region=None) -> _FakeClient:
        return clients.setdefault(profile, _FakeClient({}))

    monkeypatch.setattr(secrets_cli, "_get_client", _record)

    result = runner.invoke(
        secrets_cli.app, ["set", "--yes", "my/secret", "--value", "x", "-p", "all"]
    )

    assert result.exit_code == 0, result.stdout
    assert list(clients) == ["Admin-Prod", "Admin-QA"]
    # Each per-profile line reports the short alias back, not the full name.
    assert "[prod] Updated secret" in result.stdout
    assert "[qa] Updated secret" in result.stdout


# ── authentication failures ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("seam", "argv"),
    [
        ("secretsmanager", ["get", "my/secret"]),
        # `edit` reaches STS first, so its client builder is a second, separate
        # place an expired session surfaces.
        ("sts", ["edit", "--yes", "my/secret", "-k", "a", "-v", "2"]),
    ],
    ids=["secretsmanager-client", "sts-client"],
)
def test_expired_sso_exits_four_from_either_client_builder(
    seam: str, argv: list[str], monkeypatch, wide
) -> None:
    """4, not 1: a rotation script must retry a login and stop on anything else.

    It matters most here. ``secrets set -p all`` fans a write across accounts,
    and "log in again" is the one failure a wrapper may safely retry — which it
    cannot do if a bad config, a denied API call and an expired token all exit 1.
    """
    message = "SSO login failed for profile [/x] [bold]prod[/bold]"

    def _boom(**kwargs: object):
        raise AuthError(message)

    monkeypatch.setattr(secrets_cli, "get_client", _boom)

    result = runner.invoke(secrets_cli.app, argv)

    assert result.exit_code == ExitCode.AUTH == 4
    # Unescaped, "[/x]" raises MarkupError and nothing is printed at all.
    assert message in result.stdout


# ── --verbose ───────────────────────────────────────────────────────────────
# This module reads secret values for a living, so the rule is not "redaction
# will catch it": the trace records that a value was read and which secret it
# was, and never the value or its length.


def test_verbose_traces_the_fetch_but_never_the_value(monkeypatch, wide) -> None:
    monkeypatch.setenv("DP_AWS_PROFILE_ALIASES", "prod=AdminAccess-Prod")
    _patch_client(monkeypatch, _FakeClient({"password": "s3cr3t-value"}))

    with verbose():
        result = runner.invoke(
            secrets_cli.app,
            ["get", "my/app/config", "-p", "prod", "-r", "eu-central-1"],
        )

    assert result.exit_code == 0, result.stdout
    assert (
        "[dp:aws] secretsmanager.get_secret_value | profile=AdminAccess-Prod | "
        "region=eu-central-1 | name=my/app/config" in result.stderr
    )
    # The value is on stdout because that is what the command is for. It must not
    # also be on stderr, where a CI log keeps it next to the request that got it.
    assert "s3cr3t-value" in result.stdout
    assert "s3cr3t-value" not in result.stderr


def test_verbose_keeps_a_credential_shaped_secret_name_legible(
    monkeypatch, wide
) -> None:
    """The field is ``name=`` for a reason, and this is the reason.

    redact() masks whatever follows a key called secret, token, api-key or
    password — so ``secret=prod/db/password`` would be published as
    ``secret=***``, and the one identifier that makes the trace worth reading
    would be gone. Secrets in this tool are routinely *named* after credentials.
    """
    _patch_client(monkeypatch, _FakeClient({"a": "1"}))

    with verbose():
        result = runner.invoke(secrets_cli.app, ["get", "prod/db/password"])

    assert result.exit_code == 0, result.stdout
    assert "name=prod/db/password" in result.stderr


def test_verbose_leaves_raw_output_byte_identical(monkeypatch, wide) -> None:
    """``--raw`` pipes a secret into another program; --verbose may not touch it."""
    _patch_client(monkeypatch, _FakeClient("s3cr3t-value"))

    with verbose():
        result = runner.invoke(secrets_cli.app, ["get", "my/secret", "--raw"])

    assert result.exit_code == 0, result.stdout
    assert result.stdout == "s3cr3t-value\n"
    assert "[dp:aws]" in result.stderr


def test_verbose_traces_a_write_by_key_name_not_by_value(monkeypatch, wide) -> None:
    client = _FakeClient({"password": "old-s3cr3t"})
    _patch_client(monkeypatch, client)

    with verbose():
        result = runner.invoke(
            secrets_cli.app,
            ["edit", "--yes", "my/secret", "-k", "password", "-v", "new-s3cr3t"],
        )

    assert result.exit_code == 0, result.stdout
    err = result.stderr
    assert "secretsmanager.put_secret_value" in err
    assert "name=my/secret" in err
    assert "keys=password" in err
    # Neither the new value nor the old one, and no length either: "9 chars" is a
    # real hint about a password.
    assert "new-s3cr3t" not in err
    assert "old-s3cr3t" not in err
    # The identity check that precedes the read is traced as its own service.
    assert "[dp:aws] sts.get_caller_identity | profile=prod-profile" in err


def test_verbose_traces_every_profile_a_write_fans_out_to(monkeypatch, wide) -> None:
    """The multi-account case: each line must say which account it belonged to."""
    monkeypatch.setenv("DP_AWS_PROFILE_ALIASES", "prod=Admin-Prod,qa=Admin-QA")
    monkeypatch.setattr(
        secrets_cli, "_get_client", lambda profile=None, region=None: _FakeClient({})
    )

    with verbose():
        result = runner.invoke(
            secrets_cli.app, ["set", "--yes", "my/secret", "--value", "x", "-p", "all"]
        )

    assert result.exit_code == 0, result.stdout
    traced = [line for line in result.stderr.splitlines() if "put_secret_value" in line]
    assert len(traced) == 2
    assert "profile=Admin-Prod" in traced[0]
    assert "profile=Admin-QA" in traced[1]


def test_nothing_is_traced_without_verbose(monkeypatch, wide) -> None:
    _patch_client(monkeypatch, _FakeClient({"a": "1"}))

    result = runner.invoke(secrets_cli.app, ["get", "my/secret"])

    assert result.exit_code == 0, result.stdout
    assert result.stderr == ""


def test_versions_renders_hostile_version_ids(monkeypatch, wide) -> None:
    class _VersionsClient(_FakeClient):
        def list_secret_version_ids(self, SecretId: str, **kwargs) -> dict:
            return {
                "Versions": [
                    {
                        "VersionId": "v[/x]1",
                        "VersionStages": ["AWSCURRENT", "[bold]s[/bold]"],
                        "CreatedDate": "2026-01-01",
                    }
                ]
            }

    _patch_client(monkeypatch, _VersionsClient({}))

    result = runner.invoke(secrets_cli.app, ["versions", "app/[/x]"])

    assert result.exit_code == 0, result.stdout
    assert "v[/x]1" in result.stdout
    assert "[bold]s[/bold]" in result.stdout
