"""The write paths of ``dp cloud aws secrets``: one gate, no silent writes."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
import typer
from rich.console import Console
from typer.testing import CliRunner

from dataplat.cli import _prompt
from dataplat.cli.cloud.aws import secrets as secrets_cli

runner = CliRunner()


class _FakeNotFound(Exception):
    pass


class _FakeClient:
    """The slice of the Secrets Manager API the write commands touch."""

    def __init__(self, stored: dict | str | None = None) -> None:
        if stored is None:
            self._stored: str | None = None
        else:
            self._stored = stored if isinstance(stored, str) else json.dumps(stored)
        self.calls: list[tuple[str, dict]] = []
        self.exceptions = SimpleNamespace(ResourceNotFoundException=_FakeNotFound)

    def get_secret_value(self, SecretId: str) -> dict:
        if self._stored is None:
            raise _FakeNotFound
        return {"SecretString": self._stored}

    def put_secret_value(self, SecretId: str, SecretString: str) -> None:
        self.calls.append(("put_secret_value", {"SecretString": SecretString}))
        self._stored = SecretString

    def create_secret(self, **kwargs: object) -> None:
        self.calls.append(("create_secret", dict(kwargs)))

    def update_secret(self, **kwargs: object) -> None:
        self.calls.append(("update_secret", dict(kwargs)))

    def delete_secret(self, **kwargs: object) -> None:
        self.calls.append(("delete_secret", dict(kwargs)))

    def restore_secret(self, SecretId: str) -> None:
        self.calls.append(("restore_secret", {"SecretId": SecretId}))

    def list_secret_version_ids(self, SecretId: str, **kwargs: object) -> dict:
        return {
            "Versions": [
                {"VersionId": "v-current", "VersionStages": ["AWSCURRENT"]},
                {"VersionId": "v-previous", "VersionStages": ["AWSPREVIOUS"]},
            ]
        }

    def update_secret_version_stage(self, **kwargs: object) -> None:
        self.calls.append(("update_secret_version_stage", dict(kwargs)))


class _FakeSts:
    def get_caller_identity(self) -> dict:
        return {"Arn": "arn:aws:sts::0:assumed-role/Test", "Account": "0"}


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> _FakeClient:
    """A patched-in client, plus a wide unstyled console for assertions."""
    monkeypatch.delenv("DP_AWS_PROFILE_ALIASES", raising=False)
    monkeypatch.setattr(
        secrets_cli,
        "console",
        Console(width=400, no_color=True, legacy_windows=False),
    )
    fake = _FakeClient({"a": "1"})
    monkeypatch.setattr(
        secrets_cli, "_get_client", lambda profile=None, region=None: fake
    )
    monkeypatch.setattr(
        secrets_cli, "_get_sts_client", lambda profile=None, region=None: _FakeSts()
    )
    monkeypatch.setattr(
        secrets_cli, "_resolve_profiles", lambda profiles: ["Admin-Prod"]
    )
    return fake


def _answer(monkeypatch: pytest.MonkeyPatch, accepted: bool) -> None:
    """Make the shared gate see a TTY and answer the prompt.

    Only ``_prompt``'s view of stdin is replaced: the CliRunner keeps the real
    one, which click still needs for commands that read piped input.
    """
    monkeypatch.setattr(
        _prompt, "sys", SimpleNamespace(stdin=SimpleNamespace(isatty=lambda: True))
    )
    monkeypatch.setattr(typer, "confirm", lambda *a, **k: accepted)


# Every destructive command, with the arguments that make it reach the gate.
WRITE_COMMANDS: list[tuple[str, list[str]]] = [
    ("set", ["set", "my/secret", "--value", "x"]),
    ("edit", ["edit", "my/secret", "-k", "a", "-v", "2"]),
    ("rename-key", ["rename-key", "my/secret", "--old-key", "a", "--new-key", "b"]),
    ("delete", ["delete", "my/secret"]),
    ("delete-force", ["delete", "my/secret", "--force"]),
    ("rollback", ["rollback", "my/secret"]),
]


@pytest.mark.parametrize(
    ("name", "argv"), WRITE_COMMANDS, ids=[c[0] for c in WRITE_COMMANDS]
)
def test_accepted_confirmation_writes(
    name: str, argv: list[str], client: _FakeClient, monkeypatch
) -> None:
    _answer(monkeypatch, accepted=True)

    result = runner.invoke(secrets_cli.app, argv)

    assert result.exit_code == 0, result.stdout
    assert client.calls, f"{name} confirmed but called no API"


@pytest.mark.parametrize(
    ("name", "argv"), WRITE_COMMANDS, ids=[c[0] for c in WRITE_COMMANDS]
)
def test_declined_confirmation_exits_one_without_calling_aws(
    name: str, argv: list[str], client: _FakeClient, monkeypatch
) -> None:
    _answer(monkeypatch, accepted=False)

    result = runner.invoke(secrets_cli.app, argv)

    assert result.exit_code == 1
    assert client.calls == []
    assert "Aborted." in result.stdout


@pytest.mark.parametrize(
    ("name", "argv"), WRITE_COMMANDS, ids=[c[0] for c in WRITE_COMMANDS]
)
def test_non_interactive_without_yes_refuses_and_names_the_flag(
    name: str, argv: list[str], client: _FakeClient
) -> None:
    # The CliRunner's stdin is not a TTY, which is the CI case.
    result = runner.invoke(secrets_cli.app, argv)

    assert result.exit_code == 1
    assert client.calls == []
    assert "--yes" in result.stdout


@pytest.mark.parametrize(
    ("name", "argv"), WRITE_COMMANDS, ids=[c[0] for c in WRITE_COMMANDS]
)
def test_yes_proceeds_without_prompting(
    name: str, argv: list[str], client: _FakeClient, monkeypatch
) -> None:
    def _boom(*args: object, **kwargs: object) -> bool:
        raise AssertionError("--yes must never prompt")

    monkeypatch.setattr(typer, "confirm", _boom)

    result = runner.invoke(secrets_cli.app, [*argv, "--yes"])

    assert result.exit_code == 0, result.stdout
    assert client.calls, f"{name} was authorized but called no API"


def test_set_creates_when_the_secret_is_missing(monkeypatch, client) -> None:
    class _Missing(_FakeClient):
        def put_secret_value(self, SecretId: str, SecretString: str) -> None:
            raise _FakeNotFound

    fake = _Missing(None)
    monkeypatch.setattr(
        secrets_cli, "_get_client", lambda profile=None, region=None: fake
    )

    result = runner.invoke(
        secrets_cli.app, ["set", "--yes", "my/secret", "--value", "x"]
    )

    assert result.exit_code == 0, result.stdout
    assert fake.calls == [("create_secret", {"Name": "my/secret", "SecretString": "x"})]


def test_delete_force_skips_the_recovery_window(monkeypatch, client) -> None:
    result = runner.invoke(secrets_cli.app, ["delete", "--yes", "my/secret", "--force"])

    assert result.exit_code == 0, result.stdout
    assert client.calls == [
        (
            "delete_secret",
            {"SecretId": "my/secret", "ForceDeleteWithoutRecovery": True},
        )
    ]


def test_delete_without_force_keeps_the_recovery_window(monkeypatch, client) -> None:
    result = runner.invoke(secrets_cli.app, ["delete", "--yes", "my/secret"])

    assert result.exit_code == 0, result.stdout
    assert client.calls == [
        ("delete_secret", {"SecretId": "my/secret", "RecoveryWindowInDays": 30})
    ]


# ── markup safety on the write paths ─────────────────────────────────────────


def test_confirmation_summary_escapes_names_and_keys(monkeypatch, client) -> None:
    """The summary is markup: a hostile name must show, not crash or vanish."""
    _answer(monkeypatch, accepted=False)

    result = runner.invoke(
        secrets_cli.app,
        [
            "rename-key",
            "my/[/x]",
            "--old-key",
            "a[/x]",
            "--new-key",
            "[bold]b[/bold]",
        ],
    )

    assert result.exit_code == 1
    assert "my/[/x]" in result.stdout
    assert "a[/x]" in result.stdout
    assert "[bold]b[/bold]" in result.stdout


def test_confirmation_summary_escapes_hostile_profile_alias(
    monkeypatch, client
) -> None:
    monkeypatch.setenv("DP_AWS_PROFILE_ALIASES", "pr[/x]d=Admin-Prod")
    _answer(monkeypatch, accepted=False)

    result = runner.invoke(secrets_cli.app, ["set", "my/secret", "--value", "x"])

    assert result.exit_code == 1
    assert "pr[/x]d" in result.stdout


def test_set_reports_hostile_names_and_client_errors(monkeypatch, client) -> None:
    from botocore.exceptions import ClientError

    class _Denied(_FakeClient):
        def put_secret_value(self, SecretId: str, SecretString: str) -> None:
            raise ClientError(
                {
                    "Error": {
                        "Code": "AccessDenied[/x]",
                        "Message": "no [bold]write[/bold]",
                    }
                },
                "PutSecretValue",
            )

    fake = _Denied({"a": "1"})
    monkeypatch.setattr(
        secrets_cli, "_get_client", lambda profile=None, region=None: fake
    )

    result = runner.invoke(secrets_cli.app, ["set", "--yes", "my/[/x]", "--value", "x"])

    assert result.exit_code == 0, result.stdout
    assert "AccessDenied[/x]" in result.stdout
    assert "no [bold]write[/bold]" in result.stdout


def test_edit_reports_hostile_key_names(monkeypatch, client) -> None:
    result = runner.invoke(
        secrets_cli.app,
        ["edit", "--yes", "my/[/x]", "-k", "tok[/x]en", "-v", "2"],
    )

    assert result.exit_code == 0, result.stdout
    assert "+ tok[/x]en" in result.stdout
    assert "my/[/x]" in result.stdout


def test_rename_key_reports_hostile_keys(monkeypatch, client) -> None:
    result = runner.invoke(
        secrets_cli.app,
        [
            "rename-key",
            "--yes",
            "my/secret",
            "--old-key",
            "a",
            "--new-key",
            "[bold]b[/bold]",
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert "'a' → '[bold]b[/bold]'" in result.stdout
    assert json.loads(client.calls[0][1]["SecretString"]) == {"[bold]b[/bold]": "1"}


def test_rollback_summary_escapes_version_ids(monkeypatch) -> None:
    class _Hostile(_FakeClient):
        def list_secret_version_ids(self, SecretId: str, **kwargs: object) -> dict:
            return {
                "Versions": [
                    {"VersionId": "[/x]current", "VersionStages": ["AWSCURRENT"]},
                    {"VersionId": "[bold]prev", "VersionStages": ["AWSPREVIOUS"]},
                ]
            }

    fake = _Hostile({})
    monkeypatch.setattr(
        secrets_cli,
        "console",
        Console(width=400, no_color=True, legacy_windows=False),
    )
    monkeypatch.setattr(
        secrets_cli, "_get_client", lambda profile=None, region=None: fake
    )
    _answer(monkeypatch, accepted=False)

    result = runner.invoke(secrets_cli.app, ["rollback", "my/secret"])

    assert result.exit_code == 1
    assert "[/x]curr" in result.stdout
    assert "[bold]pr" in result.stdout
    assert fake.calls == []


# ---------------------------------------------------------------------------
# Choosing where the value comes from
# ---------------------------------------------------------------------------
# Every one of these refuses before a client is built. A secret written from
# the wrong source is not an error anyone sees: it is a value that is simply
# wrong from then on.


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(["set", "my/secret"], id="none"),
        pytest.param(
            ["set", "my/secret", "--value", "x", "--from-json", "{}"], id="two"
        ),
        pytest.param(
            ["set", "my/secret", "--value", "x", "--value-stdin"], id="value-and-stdin"
        ),
    ],
)
def test_set_needs_exactly_one_source(argv: list[str], client: _FakeClient) -> None:
    result = runner.invoke(secrets_cli.app, argv)

    assert result.exit_code == 1
    assert "exactly one of" in result.stdout
    assert client.calls == []


def test_set_refuses_json_that_does_not_parse(client: _FakeClient) -> None:
    result = runner.invoke(
        secrets_cli.app, ["set", "my/secret", "--from-json", "{not json"]
    )

    assert result.exit_code == 1
    assert "not valid JSON" in result.stdout
    assert client.calls == []


def test_set_refuses_a_file_it_cannot_read(client: _FakeClient) -> None:
    result = runner.invoke(
        secrets_cli.app, ["set", "my/secret", "--from-file", "/no/such/file.json"]
    )

    assert result.exit_code == 1
    assert "Cannot read file" in result.stdout
    assert client.calls == []


def test_set_reads_a_file(client: _FakeClient, monkeypatch, tmp_path) -> None:
    path = tmp_path / "value.json"
    path.write_text('{"from": "file"}')
    _answer(monkeypatch, accepted=True)

    result = runner.invoke(
        secrets_cli.app, ["set", "my/secret", "--from-file", str(path)]
    )

    assert result.exit_code == 0, result.stdout
    assert client.calls[0] == ("put_secret_value", {"SecretString": '{"from": "file"}'})


def test_set_reads_stdin(client: _FakeClient, monkeypatch) -> None:
    """So a password never has to appear in a shell history or a process list."""
    _answer(monkeypatch, accepted=True)

    result = runner.invoke(
        secrets_cli.app, ["set", "my/secret", "--value-stdin", "-y"], input="s3cret\n"
    )

    assert result.exit_code == 0, result.stdout
    assert client.calls[0][1]["SecretString"] == "s3cret"
    assert "s3cret" not in result.stdout


def test_set_updates_the_description_separately(
    client: _FakeClient, monkeypatch
) -> None:
    """Secrets Manager takes the value and the description through two calls."""
    _answer(monkeypatch, accepted=True)

    result = runner.invoke(
        secrets_cli.app,
        ["set", "my/secret", "--value", "x", "--description", "why", "-y"],
    )

    assert result.exit_code == 0, result.stdout
    assert [name for name, _ in client.calls] == ["put_secret_value", "update_secret"]


# ---------------------------------------------------------------------------
# edit: which keys, from where
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(["edit", "my/secret"], id="neither"),
        pytest.param(["edit", "my/secret", "-k", "a"], id="key-without-value"),
        pytest.param(
            ["edit", "my/secret", "-k", "a", "-v", "1", "--from-file", "p.json"],
            id="pair-and-file",
        ),
    ],
)
def test_edit_needs_a_coherent_patch(argv: list[str], client: _FakeClient) -> None:
    result = runner.invoke(secrets_cli.app, argv)

    assert result.exit_code == 1
    assert client.calls == []


def test_edit_refuses_a_json_file_that_is_not_an_object(
    client: _FakeClient, tmp_path
) -> None:
    """A list of keys is not a patch, and guessing what it meant writes garbage."""
    path = tmp_path / "patch.json"
    path.write_text('["a", "b"]')

    result = runner.invoke(
        secrets_cli.app, ["edit", "my/secret", "--from-file", str(path)]
    )

    assert result.exit_code == 1
    assert "object at the top level" in result.stdout
    assert client.calls == []


def test_edit_refuses_a_json_file_that_does_not_parse(
    client: _FakeClient, tmp_path
) -> None:
    path = tmp_path / "patch.json"
    path.write_text("{oops")

    result = runner.invoke(
        secrets_cli.app, ["edit", "my/secret", "--from-file", str(path)]
    )

    assert result.exit_code == 1
    assert "not valid JSON" in result.stdout
    assert client.calls == []


def test_edit_applies_a_patch_from_a_file(
    client: _FakeClient, monkeypatch, tmp_path
) -> None:
    path = tmp_path / "patch.json"
    path.write_text('{"b": "2", "c": "3"}')
    _answer(monkeypatch, accepted=True)

    result = runner.invoke(
        secrets_cli.app, ["edit", "my/secret", "--from-file", str(path), "-y"]
    )

    assert result.exit_code == 0, result.stdout
    written = json.loads(client.calls[-1][1]["SecretString"])
    # The existing key survives; the patch is a patch, not a replacement.
    assert written == {"a": "1", "b": "2", "c": "3"}


def test_edit_refuses_a_secret_that_is_not_json(monkeypatch) -> None:
    """There is no key to edit in a blob, and overwriting it would lose it."""
    monkeypatch.delenv("DP_AWS_PROFILE_ALIASES", raising=False)
    monkeypatch.setattr(
        secrets_cli, "console", Console(width=400, no_color=True, legacy_windows=False)
    )
    fake = _FakeClient("not json at all")
    monkeypatch.setattr(
        secrets_cli, "_get_client", lambda profile=None, region=None: fake
    )
    monkeypatch.setattr(
        secrets_cli, "_get_sts_client", lambda profile=None, region=None: _FakeSts()
    )
    monkeypatch.setattr(
        secrets_cli, "_resolve_profiles", lambda profiles: ["Admin-Prod"]
    )
    _answer(monkeypatch, accepted=True)

    result = runner.invoke(
        secrets_cli.app, ["edit", "my/secret", "-k", "a", "-v", "2", "-y"]
    )

    assert "not valid JSON" in result.stdout
    assert [name for name, _ in fake.calls] == []


# ---------------------------------------------------------------------------
# rename-key
# ---------------------------------------------------------------------------


def test_rename_key_refuses_when_the_new_name_is_taken(
    client: _FakeClient, monkeypatch
) -> None:
    """Renaming onto an existing key would overwrite a value nobody asked about."""
    client._stored = json.dumps({"a": "1", "b": "2"})
    _answer(monkeypatch, accepted=True)

    result = runner.invoke(
        secrets_cli.app,
        ["rename-key", "my/secret", "--old-key", "a", "--new-key", "b", "-y"],
    )

    assert "already exists" in result.stdout
    assert [name for name, _ in client.calls] == []


def test_rename_key_reports_a_key_that_is_not_there(
    client: _FakeClient, monkeypatch
) -> None:
    _answer(monkeypatch, accepted=True)

    result = runner.invoke(
        secrets_cli.app,
        ["rename-key", "my/secret", "--old-key", "nope", "--new-key", "b", "-y"],
    )

    assert "not found in secret" in result.stdout
    assert [name for name, _ in client.calls] == []


def test_rename_key_keeps_the_keys_in_order(client: _FakeClient, monkeypatch) -> None:
    """A secret is read by people; reordering it makes a diff nobody can review."""
    client._stored = json.dumps({"first": "1", "second": "2", "third": "3"})
    _answer(monkeypatch, accepted=True)

    result = runner.invoke(
        secrets_cli.app,
        ["rename-key", "my/secret", "--old-key", "second", "--new-key", "middle", "-y"],
    )

    assert result.exit_code == 0, result.stdout
    written = json.loads(client.calls[-1][1]["SecretString"])
    assert list(written) == ["first", "middle", "third"]


# ---------------------------------------------------------------------------
# restore and versions
# ---------------------------------------------------------------------------


def test_restore_cancels_a_scheduled_deletion(client: _FakeClient) -> None:
    result = runner.invoke(secrets_cli.app, ["restore", "my/secret"])

    assert result.exit_code == 0, result.stdout
    assert ("restore_secret", {"SecretId": "my/secret"}) in client.calls


def test_restore_reports_a_secret_that_is_not_there(
    client: _FakeClient, monkeypatch
) -> None:
    def missing(SecretId: str) -> None:
        raise _FakeNotFound

    monkeypatch.setattr(client, "restore_secret", missing)

    result = runner.invoke(secrets_cli.app, ["restore", "gone"])

    assert result.exit_code == 1
    assert "not found" in result.stdout


def test_versions_json_is_machine_readable(client: _FakeClient) -> None:
    result = runner.invoke(secrets_cli.app, ["versions", "my/secret", "--json"])

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert {v["version_id"] for v in payload} == {"v-current", "v-previous"}
    assert any("AWSCURRENT" in v["stages"] for v in payload)


def test_versions_renders_a_table(client: _FakeClient) -> None:
    result = runner.invoke(secrets_cli.app, ["versions", "my/secret"])

    assert result.exit_code == 0, result.stdout
    assert "v-current" in result.stdout
