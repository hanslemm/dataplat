from __future__ import annotations

import json

from botocore.exceptions import ClientError
from typer.testing import CliRunner

from dataplat.cli.cloud.aws import secrets as secrets_cli

runner = CliRunner()


class _FakeNotFound(Exception):
    pass


class _FakeExceptions:
    ResourceNotFoundException = _FakeNotFound


class _FakeClient:
    def __init__(self, initial: dict | str) -> None:
        self._stored = (
            initial if isinstance(initial, str) else json.dumps(initial)
        )
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

    result = runner.invoke(
        secrets_cli.app, ["set", "my/secret", "--value", "x"]
    )

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

    result = runner.invoke(
        secrets_cli.app, ["delete", "my/secret", "--force"]
    )

    assert result.exit_code == 1
    assert client.deleted == []
