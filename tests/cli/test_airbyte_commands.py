"""CLI integration tests for Airbyte commands with monkeypatched client."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest
from typer.testing import CliRunner

import dataplat.main as main_module
import dataplat.services.airbyte.client as airbyte_client
from dataplat.core.errors import AuthError, ConfigError, ServiceError

runner = CliRunner()


def _disable_envrc(monkeypatch) -> None:
    monkeypatch.setattr(main_module, "load_envrc", lambda: None)


def _mock_authenticated_client(monkeypatch):
    """Monkeypatch build_authenticated_client to return a fake client.

    The CLI modules import build_authenticated_client directly, so we patch
    in each CLI module's namespace as well as the service module.
    """
    import httpx

    import dataplat.cli.ingest.airbyte.connections as _airbyte_connections_cli
    import dataplat.cli.ingest.airbyte.definitions as _airbyte_definitions_cli
    import dataplat.cli.ingest.airbyte.workspaces as _airbyte_workspaces_cli

    class FakeResponse:
        def __init__(self, data, status_code=200):
            self.status_code = status_code
            self._data = data
            self.text = json.dumps(data) if data is not None else ""
            self.headers = {"content-type": "application/json"}

        def raise_for_status(self):
            if self.status_code >= 400:
                raise httpx.HTTPStatusError(
                    "error",
                    request=httpx.Request("GET", "http://test"),
                    response=self,
                )

        def json(self):
            return self._data

    class FakeClient:
        def __init__(self):
            # Track call counts per endpoint to avoid infinite pagination loops
            self._call_counts: dict[str, int] = {}

        def _increment(self, key: str) -> int:
            self._call_counts[key] = self._call_counts.get(key, 0) + 1
            return self._call_counts[key]

        def get(self, url, **kwargs):
            if "sources" in url and "definitions" not in url:
                count = self._increment("sources")
                if count == 1:
                    return FakeResponse(
                        {
                            "data": [
                                {
                                    "sourceId": "s1",
                                    "name": "TestSource",
                                    "sourceName": "Postgres",
                                    "workspaceId": "ws1",
                                }
                            ]
                        }
                    )
                return FakeResponse({"data": []})
            if "destinations" in url and "definitions" not in url:
                count = self._increment("destinations")
                if count == 1:
                    return FakeResponse(
                        {
                            "data": [
                                {
                                    "destinationId": "d1",
                                    "name": "TestDest",
                                    "destinationName": "BigQuery",
                                    "workspaceId": "ws1",
                                }
                            ]
                        }
                    )
                return FakeResponse({"data": []})
            if "workspaces" in url:
                count = self._increment("workspaces")
                if count == 1:
                    return FakeResponse(
                        {"data": [{"workspaceId": "ws1", "name": "Default"}]}
                    )
                return FakeResponse({"data": []})
            return FakeResponse({"data": []})

        def post(self, url, **kwargs):
            return FakeResponse({"sourceId": "s1", "name": "Created"})

        def patch(self, url, **kwargs):
            return FakeResponse({"sourceId": "s1", "name": "Updated"})

        def delete(self, url, **kwargs):
            return FakeResponse(None, status_code=204)

        def close(self):
            pass

        @property
        def headers(self):
            return {}

    factory = lambda: (FakeClient(), "http://test")  # noqa: E731

    import dataplat.cli.ingest.airbyte._common as _airbyte_common_cli

    # Patch the service module and every CLI module that imported it directly
    monkeypatch.setattr(airbyte_client, "build_authenticated_client", factory)
    monkeypatch.setattr(_airbyte_common_cli, "build_authenticated_client", factory)
    monkeypatch.setattr(_airbyte_connections_cli, "build_authenticated_client", factory)
    monkeypatch.setattr(_airbyte_definitions_cli, "build_authenticated_client", factory)
    monkeypatch.setattr(_airbyte_workspaces_cli, "build_authenticated_client", factory)


def test_sources_list_table(monkeypatch) -> None:
    _disable_envrc(monkeypatch)
    _mock_authenticated_client(monkeypatch)
    result = runner.invoke(main_module.app, ["ingest", "airbyte", "sources", "list"])
    assert result.exit_code == 0
    assert "TestSource" in result.stdout


def test_sources_list_json(monkeypatch) -> None:
    _disable_envrc(monkeypatch)
    _mock_authenticated_client(monkeypatch)
    result = runner.invoke(
        main_module.app, ["ingest", "airbyte", "sources", "list", "--format", "json"]
    )
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data[0]["sourceId"] == "s1"


def test_sources_create_with_config_file(monkeypatch, tmp_path) -> None:
    _disable_envrc(monkeypatch)
    _mock_authenticated_client(monkeypatch)
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({"host": "localhost", "port": 5432}))
    result = runner.invoke(
        main_module.app,
        [
            "ingest",
            "airbyte",
            "sources",
            "create",
            "--name",
            "NewSource",
            "--definition-id",
            "def1",
            "--workspace-id",
            "ws1",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 0
    assert "s1" in result.stdout


def test_sources_delete_with_yes(monkeypatch) -> None:
    _disable_envrc(monkeypatch)
    _mock_authenticated_client(monkeypatch)
    result = runner.invoke(
        main_module.app,
        [
            "ingest",
            "airbyte",
            "sources",
            "delete",
            "--source-id",
            "s1",
            "--yes",
        ],
    )
    assert result.exit_code == 0
    assert "deleted" in result.stdout


def test_workspaces_list(monkeypatch) -> None:
    _disable_envrc(monkeypatch)
    _mock_authenticated_client(monkeypatch)
    result = runner.invoke(main_module.app, ["ingest", "airbyte", "workspaces", "list"])
    assert result.exit_code == 0
    assert "Default" in result.stdout


def test_templates_connection(monkeypatch) -> None:
    _disable_envrc(monkeypatch)
    result = runner.invoke(
        main_module.app, ["ingest", "airbyte", "templates", "connection"]
    )
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert "sourceId" in data
    assert "destinationId" in data


class _StateFakeClient:
    """Fake Airbyte client for set-cursor / refresh tests.

    Routes the endpoints those commands touch and records every POST payload
    so tests can assert what was written.
    """

    def __init__(self, *, state: dict, running: bool = False):
        self._state = state
        self._running = running
        self._conn_listed = False
        self.posts: list[tuple[str, dict]] = []

    def get(self, url, params=None, **kw):
        if url.endswith("/api/public/v1/connections"):
            # one page then empty, to stop pagination
            if self._conn_listed:
                return _SFResponse({"data": []})
            self._conn_listed = True
            return _SFResponse(
                {
                    "data": [
                        {
                            "connectionId": "c1",
                            "name": "Conn1",
                            "status": "active",
                            "sourceId": "s1",
                            "destinationId": "d1",
                            "workspaceId": "ws1",
                        }
                    ]
                }
            )
        if "/api/public/v1/connections/" in url:  # get_connection (single -c)
            return _SFResponse(
                {
                    "connectionId": "c1",
                    "name": "Conn1",
                    "status": "active",
                    "sourceId": "s1",
                    "destinationId": "d1",
                }
            )
        if url.endswith("/api/public/v1/jobs"):  # list_jobs (busy check)
            data = [{"jobId": 1, "status": "running"}] if self._running else []
            return _SFResponse({"data": data})
        return _SFResponse({"data": []})

    def post(self, url, json=None, **kw):
        self.posts.append((url, json or {}))
        if url.endswith("/api/v1/state/get"):
            return _SFResponse(self._state)
        if url.endswith("/api/v1/state/create_or_update"):
            return _SFResponse({"ok": True})
        if url.endswith("/api/public/v1/jobs"):  # refresh trigger
            return _SFResponse({"jobId": 42})
        return _SFResponse({})

    def close(self):
        pass

    @property
    def headers(self):
        return {}


class _SFResponse:
    def __init__(self, data, status_code=200):
        import json as _json

        self.status_code = status_code
        self._data = data
        self.text = _json.dumps(data) if data is not None else ""
        self.headers = {"content-type": "application/json"}

    def raise_for_status(self):
        import httpx

        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "e", request=httpx.Request("POST", "http://t"), response=self
            )

    def json(self):
        return self._data


def _patch_state_client(monkeypatch, client) -> None:
    import dataplat.cli.ingest.airbyte._common as _common
    import dataplat.cli.ingest.airbyte.connections as _conns
    import dataplat.services.airbyte.client as _svc

    factory = lambda: (client, "http://test")  # noqa: E731
    monkeypatch.setattr(_svc, "build_authenticated_client", factory)
    monkeypatch.setattr(_common, "build_authenticated_client", factory)
    monkeypatch.setattr(_conns, "build_authenticated_client", factory)


_STREAM_STATE = {
    "connectionId": "c1",
    "stateType": "stream",
    "streamState": [
        {
            "streamDescriptor": {"name": "orders", "namespace": "public"},
            "streamState": {"updated_at": "2024-06-01T00:00:00Z"},
        },
        {
            "streamDescriptor": {"name": "events", "namespace": "public"},
            "streamState": {"id": 987654},
        },
    ],
}


def test_set_cursor_dry_run_writes_nothing(monkeypatch) -> None:
    _disable_envrc(monkeypatch)
    client = _StateFakeClient(state=dict(_STREAM_STATE))
    _patch_state_client(monkeypatch, client)
    result = runner.invoke(
        main_module.app,
        [
            "ingest",
            "airbyte",
            "connections",
            "set-cursor",
            "-c",
            "c1",
            "--to",
            "2024-01-01",
            "--dry-run",
        ],
    )
    assert result.exit_code == 0
    # planned the rewrite in output, but never wrote state
    assert "orders" in result.stdout
    assert "skip:opaque" in result.stdout or "events" in result.stdout
    assert not any(
        u.endswith("/api/v1/state/create_or_update") for u, _ in client.posts
    )


def test_set_cursor_writes_rewritten_state(monkeypatch) -> None:
    _disable_envrc(monkeypatch)
    client = _StateFakeClient(
        state={
            "connectionId": "c1",
            "stateType": "stream",
            "streamState": [
                {
                    "streamDescriptor": {"name": "orders", "namespace": "public"},
                    "streamState": {"updated_at": "2024-06-01T00:00:00Z"},
                },
                {
                    "streamDescriptor": {"name": "events", "namespace": "public"},
                    "streamState": {"id": 987654},
                },
            ],
        }
    )
    _patch_state_client(monkeypatch, client)
    result = runner.invoke(
        main_module.app,
        [
            "ingest",
            "airbyte",
            "connections",
            "set-cursor",
            "-c",
            "c1",
            "--to",
            "2024-01-01",
            "--yes",
        ],
    )
    assert result.exit_code == 0
    writes = [
        p for u, p in client.posts if u.endswith("/api/v1/state/create_or_update")
    ]
    assert len(writes) == 1
    written_state = writes[0]["connectionState"]
    streams = {
        s["streamDescriptor"]["name"]: s["streamState"]
        for s in written_state["streamState"]
    }
    assert streams["orders"]["updated_at"] == "2024-01-01T00:00:00Z"  # rewritten
    assert streams["events"]["id"] == 987654  # opaque untouched


def test_set_cursor_skips_busy_connection(monkeypatch) -> None:
    _disable_envrc(monkeypatch)
    client = _StateFakeClient(state=dict(_STREAM_STATE), running=True)
    _patch_state_client(monkeypatch, client)
    result = runner.invoke(
        main_module.app,
        [
            "ingest",
            "airbyte",
            "connections",
            "set-cursor",
            "-c",
            "c1",
            "--to",
            "2024-01-01",
            "--yes",
        ],
    )
    assert result.exit_code == 0
    assert "busy" in result.stdout.lower()
    assert not any(
        u.endswith("/api/v1/state/create_or_update") for u, _ in client.posts
    )


def test_set_cursor_force_writes_busy_connection(monkeypatch) -> None:
    _disable_envrc(monkeypatch)
    client = _StateFakeClient(state=dict(_STREAM_STATE), running=True)
    _patch_state_client(monkeypatch, client)
    result = runner.invoke(
        main_module.app,
        [
            "ingest",
            "airbyte",
            "connections",
            "set-cursor",
            "-c",
            "c1",
            "--to",
            "2024-01-01",
            "--yes",
            "--force",
        ],
    )
    assert result.exit_code == 0
    assert any(u.endswith("/api/v1/state/create_or_update") for u, _ in client.posts)


def test_set_cursor_bad_date_exits_2(monkeypatch) -> None:
    _disable_envrc(monkeypatch)
    client = _StateFakeClient(state=dict(_STREAM_STATE))
    _patch_state_client(monkeypatch, client)
    result = runner.invoke(
        main_module.app,
        [
            "ingest",
            "airbyte",
            "connections",
            "set-cursor",
            "-c",
            "c1",
            "--to",
            "not-a-date",
            "--yes",
        ],
    )
    assert result.exit_code == 2


_XMIN_STATE = {
    "connectionId": "c1",
    "stateType": "stream",
    "streamState": [
        {
            "streamDescriptor": {"name": "orders", "namespace": "public"},
            "streamState": {
                "state_type": "xmin",
                "version": 2,
                "xmin_xid_value": 1000,
                "xmin_raw_value": 1000,
                "num_wraparound": 0,
            },
        },
    ],
}


def _written_state(client):
    writes = [
        p for u, p in client.posts if u.endswith("/api/v1/state/create_or_update")
    ]
    assert len(writes) == 1
    return writes[0]["connectionState"]


def test_set_cursor_xmin_factor_scales(monkeypatch) -> None:
    _disable_envrc(monkeypatch)
    client = _StateFakeClient(state=json.loads(json.dumps(_XMIN_STATE)))
    _patch_state_client(monkeypatch, client)
    result = runner.invoke(
        main_module.app,
        [
            "ingest",
            "airbyte",
            "connections",
            "set-cursor",
            "-c",
            "c1",
            "--xmin-factor",
            "0.1",
            "--yes",
        ],
    )
    assert result.exit_code == 0
    inner = _written_state(client)["streamState"][0]["streamState"]
    assert inner["xmin_xid_value"] == 100
    assert inner["xmin_raw_value"] == 100


def test_set_cursor_xmin_absolute(monkeypatch) -> None:
    _disable_envrc(monkeypatch)
    client = _StateFakeClient(state=json.loads(json.dumps(_XMIN_STATE)))
    _patch_state_client(monkeypatch, client)
    result = runner.invoke(
        main_module.app,
        [
            "ingest",
            "airbyte",
            "connections",
            "set-cursor",
            "-c",
            "c1",
            "--xmin",
            "0",
            "--yes",
        ],
    )
    assert result.exit_code == 0
    inner = _written_state(client)["streamState"][0]["streamState"]
    assert inner["xmin_xid_value"] == 0


def test_set_cursor_requires_at_least_one_op(monkeypatch) -> None:
    _disable_envrc(monkeypatch)
    client = _StateFakeClient(state=dict(_STREAM_STATE))
    _patch_state_client(monkeypatch, client)
    result = runner.invoke(
        main_module.app,
        [
            "ingest",
            "airbyte",
            "connections",
            "set-cursor",
            "-c",
            "c1",
            "--yes",
        ],
    )
    assert result.exit_code == 2


def test_set_cursor_xmin_flags_mutually_exclusive(monkeypatch) -> None:
    _disable_envrc(monkeypatch)
    client = _StateFakeClient(state=dict(_STREAM_STATE))
    _patch_state_client(monkeypatch, client)
    result = runner.invoke(
        main_module.app,
        [
            "ingest",
            "airbyte",
            "connections",
            "set-cursor",
            "-c",
            "c1",
            "--xmin",
            "5",
            "--xmin-factor",
            "0.5",
            "--yes",
        ],
    )
    assert result.exit_code == 2


def test_set_cursor_combined_date_and_xmin(monkeypatch) -> None:
    _disable_envrc(monkeypatch)
    client = _StateFakeClient(
        state={
            "connectionId": "c1",
            "stateType": "stream",
            "streamState": [
                {
                    "streamDescriptor": {"name": "orders", "namespace": "public"},
                    "streamState": {"updated_at": "2024-06-01T00:00:00Z"},
                },
                {
                    "streamDescriptor": {"name": "xm", "namespace": "public"},
                    "streamState": {
                        "state_type": "xmin",
                        "version": 2,
                        "xmin_xid_value": 1000,
                        "xmin_raw_value": 1000,
                    },
                },
            ],
        }
    )
    _patch_state_client(monkeypatch, client)
    result = runner.invoke(
        main_module.app,
        [
            "ingest",
            "airbyte",
            "connections",
            "set-cursor",
            "-c",
            "c1",
            "--to",
            "2024-01-01",
            "--xmin-factor",
            "0.1",
            "--yes",
        ],
    )
    assert result.exit_code == 0
    streams = {
        s["streamDescriptor"]["name"]: s["streamState"]
        for s in _written_state(client)["streamState"]
    }
    assert streams["orders"]["updated_at"] == "2024-01-01T00:00:00Z"
    assert streams["xm"]["xmin_xid_value"] == 100


def test_set_cursor_sync_triggers_after_write(monkeypatch) -> None:
    _disable_envrc(monkeypatch)
    client = _StateFakeClient(state=json.loads(json.dumps(_XMIN_STATE)))
    _patch_state_client(monkeypatch, client)
    result = runner.invoke(
        main_module.app,
        [
            "ingest",
            "airbyte",
            "connections",
            "set-cursor",
            "-c",
            "c1",
            "--xmin",
            "0",
            "--sync",
            "--yes",
        ],
    )
    assert result.exit_code == 0
    # state written AND a sync job triggered for the same connection
    assert any(u.endswith("/api/v1/state/create_or_update") for u, _ in client.posts)
    job_posts = [p for u, p in client.posts if u.endswith("/api/public/v1/jobs")]
    assert {"connectionId": "c1", "jobType": "sync"} in job_posts


def test_set_cursor_sync_dry_run_triggers_nothing(monkeypatch) -> None:
    _disable_envrc(monkeypatch)
    client = _StateFakeClient(state=json.loads(json.dumps(_XMIN_STATE)))
    _patch_state_client(monkeypatch, client)
    result = runner.invoke(
        main_module.app,
        [
            "ingest",
            "airbyte",
            "connections",
            "set-cursor",
            "-c",
            "c1",
            "--xmin",
            "0",
            "--sync",
            "--dry-run",
        ],
    )
    assert result.exit_code == 0
    assert not any(
        u.endswith("/api/public/v1/jobs") and p.get("jobType") == "sync"
        for u, p in client.posts
    )
    assert not any(
        u.endswith("/api/v1/state/create_or_update") for u, _ in client.posts
    )


def test_set_cursor_sync_skipped_when_no_rewrite(monkeypatch) -> None:
    _disable_envrc(monkeypatch)
    # events-only state: nothing matches the xmin op, so no write and no sync
    client = _StateFakeClient(
        state={
            "connectionId": "c1",
            "stateType": "stream",
            "streamState": [
                {
                    "streamDescriptor": {"name": "events", "namespace": "public"},
                    "streamState": {"id": 987654},
                },
            ],
        }
    )
    _patch_state_client(monkeypatch, client)
    result = runner.invoke(
        main_module.app,
        [
            "ingest",
            "airbyte",
            "connections",
            "set-cursor",
            "-c",
            "c1",
            "--xmin",
            "0",
            "--sync",
            "--yes",
        ],
    )
    assert result.exit_code == 0
    assert not any(
        u.endswith("/api/public/v1/jobs") and p.get("jobType") == "sync"
        for u, p in client.posts
    )


def test_set_cursor_backup_writes_prechange_state(monkeypatch, tmp_path) -> None:
    _disable_envrc(monkeypatch)
    original = json.loads(json.dumps(_XMIN_STATE))
    client = _StateFakeClient(state=json.loads(json.dumps(original)))
    _patch_state_client(monkeypatch, client)
    result = runner.invoke(
        main_module.app,
        [
            "ingest",
            "airbyte",
            "connections",
            "set-cursor",
            "-c",
            "c1",
            "--xmin-factor",
            "0.1",
            "--backup",
            "--backup-dir",
            str(tmp_path),
            "--yes",
        ],
    )
    assert result.exit_code == 0
    backup_file = tmp_path / "c1.json"
    assert backup_file.exists()
    saved = json.loads(backup_file.read_text())
    # backup holds the ORIGINAL (pre-change) xmin value, not the rewritten one
    assert saved["streamState"][0]["streamState"]["xmin_xid_value"] == 1000


def test_set_cursor_backup_dry_run_writes_no_file(monkeypatch, tmp_path) -> None:
    _disable_envrc(monkeypatch)
    client = _StateFakeClient(state=json.loads(json.dumps(_XMIN_STATE)))
    _patch_state_client(monkeypatch, client)
    result = runner.invoke(
        main_module.app,
        [
            "ingest",
            "airbyte",
            "connections",
            "set-cursor",
            "-c",
            "c1",
            "--xmin-factor",
            "0.1",
            "--backup",
            "--backup-dir",
            str(tmp_path),
            "--dry-run",
        ],
    )
    assert result.exit_code == 0
    assert not (tmp_path / "c1.json").exists()


def test_refresh_single_triggers_refresh_job(monkeypatch) -> None:
    _disable_envrc(monkeypatch)
    client = _StateFakeClient(state=dict(_STREAM_STATE))
    _patch_state_client(monkeypatch, client)
    result = runner.invoke(
        main_module.app,
        [
            "ingest",
            "airbyte",
            "connections",
            "refresh",
            "-c",
            "c1",
        ],
    )
    assert result.exit_code == 0
    job_posts = [p for u, p in client.posts if u.endswith("/api/public/v1/jobs")]
    assert job_posts == [{"connectionId": "c1", "jobType": "refresh"}]


def test_refresh_dry_run_triggers_nothing(monkeypatch) -> None:
    _disable_envrc(monkeypatch)
    client = _StateFakeClient(state=dict(_STREAM_STATE))
    _patch_state_client(monkeypatch, client)
    result = runner.invoke(
        main_module.app,
        [
            "ingest",
            "airbyte",
            "connections",
            "refresh",
            "--source-id",
            "s1",
            "--dry-run",
        ],
    )
    assert result.exit_code == 0
    assert not any(
        u.endswith("/api/public/v1/jobs") and p.get("jobType") == "refresh"
        for u, p in client.posts
    )
    assert "c1" in result.stdout  # listed as a target


def test_refresh_single_dry_run_triggers_nothing(monkeypatch) -> None:
    _disable_envrc(monkeypatch)
    client = _StateFakeClient(state=dict(_STREAM_STATE))
    _patch_state_client(monkeypatch, client)
    result = runner.invoke(
        main_module.app,
        [
            "ingest",
            "airbyte",
            "connections",
            "refresh",
            "-c",
            "c1",
            "--dry-run",
        ],
    )
    assert result.exit_code == 0
    assert not any(
        u.endswith("/api/public/v1/jobs") and p.get("jobType") == "refresh"
        for u, p in client.posts
    )


def test_refresh_bulk_triggers_per_connection(monkeypatch) -> None:
    _disable_envrc(monkeypatch)
    client = _StateFakeClient(state=dict(_STREAM_STATE))
    _patch_state_client(monkeypatch, client)
    result = runner.invoke(
        main_module.app,
        [
            "ingest",
            "airbyte",
            "connections",
            "refresh",
            "--source-id",
            "s1",
            "--yes",
            "--sleep",
            "0",
        ],
    )
    assert result.exit_code == 0
    job_posts = [p for u, p in client.posts if u.endswith("/api/public/v1/jobs")]
    assert {"connectionId": "c1", "jobType": "refresh"} in job_posts


# --- markup safety -------------------------------------------------------
#
# Two shapes of hostile value, both reachable from any warehouse or API field:
# an unbalanced closing tag aborts a raw-str render with MarkupError, and a
# real style name is silently consumed so the output lies about the data.
CLOSING_TAG = "closes [/issue] 42"
STYLE_TAG = "[bold]not-styled[/bold]"

# A width wide enough that no assertion below can fail on column wrapping.
WIDE = {"COLUMNS": "200"}


def _assert_literal(stdout: str, *values: str) -> None:
    """Every value must appear character-for-character in the rendered output."""
    for value in values:
        assert value in stdout, f"{value!r} missing from:\n{stdout}"


class _AirbyteFake:
    """httpx.Client stand-in routing by method plus URL fragment.

    ``routes`` maps ``"<METHOD> <url fragment>"`` to the payload that endpoint
    returns. A given URL is answered from ``routes`` once and with an empty
    page afterwards, which is what terminates the paginated list generators.
    """

    def __init__(self, routes: dict[str, object]):
        self.routes = routes
        self.posted: list[tuple[str, object]] = []
        self.deleted: list[str] = []
        self._seen: set[str] = set()

    def _payload(self, method: str, url: str) -> object:
        for key, payload in self.routes.items():
            verb, _, fragment = key.partition(" ")
            if verb == method and fragment in url:
                return payload
        return {"data": []}

    def get(self, url, **kwargs):
        if url in self._seen:
            return _SFResponse({"data": []})
        self._seen.add(url)
        return _SFResponse(self._payload("GET", url))

    def post(self, url, json=None, **kwargs):
        self.posted.append((url, json))
        return _SFResponse(self._payload("POST", url))

    def delete(self, url, **kwargs):
        self.deleted.append(url)
        return _SFResponse(self._payload("DELETE", url))

    def close(self) -> None:
        pass

    @property
    def headers(self) -> dict:
        return {}


def _patch_airbyte_client(monkeypatch, client) -> None:
    """Point every namespace holding build_authenticated_client at ``client``.

    Each CLI module imports the factory by value, so patching the service
    module alone would not be seen.
    """
    import dataplat.cli.ingest.airbyte._common as _common
    import dataplat.cli.ingest.airbyte.connections as _conns
    import dataplat.cli.ingest.airbyte.definitions as _defs
    import dataplat.cli.ingest.airbyte.tags as _tags
    import dataplat.cli.ingest.airbyte.templates as _templates
    import dataplat.cli.ingest.airbyte.workspaces as _ws
    import dataplat.services.airbyte.client as _svc

    factory = lambda: (client, "http://test")  # noqa: E731
    for module in (_svc, _common, _conns, _defs, _tags, _templates, _ws):
        monkeypatch.setattr(module, "build_authenticated_client", factory)


def _invoke(monkeypatch, client, args: list[str]):
    _disable_envrc(monkeypatch)
    _patch_airbyte_client(monkeypatch, client)
    return runner.invoke(main_module.app, args, env=WIDE)


# --- definitions ---------------------------------------------------------

_SOURCE_DEFINITION = {
    "sourceDefinitionId": "sd1",
    "name": "Postgres",
    "dockerRepository": "airbyte/source-postgres",
    "dockerImageTag": "3.4.0",
    "documentationUrl": "https://docs.airbyte.com/pg",
}

_DESTINATION_DEFINITION = {
    "destinationDefinitionId": "dd1",
    "name": "BigQuery",
    "dockerRepository": "airbyte/destination-bigquery",
    "dockerImageTag": "2.1.0",
    "documentationUrl": "https://docs.airbyte.com/bq",
}


def test_definitions_list_sources_table(monkeypatch) -> None:
    client = _AirbyteFake({"GET /definitions/sources": {"data": [_SOURCE_DEFINITION]}})
    result = _invoke(
        monkeypatch,
        client,
        ["ingest", "airbyte", "definitions", "list-sources", "-w", "ws1"],
    )
    assert result.exit_code == 0, result.stdout
    _assert_literal(result.stdout, "Postgres", "airbyte/source-postgres:3.4.0", "sd1")


def test_definitions_list_destinations_table(monkeypatch) -> None:
    client = _AirbyteFake(
        {"GET /definitions/destinations": {"data": [_DESTINATION_DEFINITION]}}
    )
    result = _invoke(
        monkeypatch,
        client,
        ["ingest", "airbyte", "definitions", "list-destinations", "-w", "ws1"],
    )
    assert result.exit_code == 0, result.stdout
    _assert_literal(result.stdout, "BigQuery", "airbyte/destination-bigquery:2.1.0")


def test_definitions_list_sources_json(monkeypatch) -> None:
    client = _AirbyteFake({"GET /definitions/sources": {"data": [_SOURCE_DEFINITION]}})
    result = _invoke(
        monkeypatch,
        client,
        [
            "ingest",
            "airbyte",
            "definitions",
            "list-sources",
            "-w",
            "ws1",
            "--format",
            "json",
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout)[0]["sourceDefinitionId"] == "sd1"


def test_definitions_table_renders_markup_literally(monkeypatch) -> None:
    """Regression: a connector name carrying markup must not abort the render."""
    client = _AirbyteFake(
        {
            "GET /definitions/sources": {
                "data": [
                    {
                        **_SOURCE_DEFINITION,
                        "name": CLOSING_TAG,
                        "documentationUrl": STYLE_TAG,
                    },
                ]
            },
        }
    )
    result = _invoke(
        monkeypatch,
        client,
        ["ingest", "airbyte", "definitions", "list-sources", "-w", "ws1"],
    )
    assert result.exit_code == 0, result.stdout
    _assert_literal(result.stdout, CLOSING_TAG, STYLE_TAG)


def test_definitions_json_keeps_markup_bytes(monkeypatch) -> None:
    client = _AirbyteFake(
        {
            "GET /definitions/sources": {
                "data": [
                    {**_SOURCE_DEFINITION, "name": CLOSING_TAG},
                ]
            },
        }
    )
    result = _invoke(
        monkeypatch,
        client,
        [
            "ingest",
            "airbyte",
            "definitions",
            "list-sources",
            "-w",
            "ws1",
            "--format",
            "json",
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout)[0]["name"] == CLOSING_TAG


# --- templates -----------------------------------------------------------

_SPEC_DEFINITION = {
    "sourceDefinitionId": "sd1",
    "name": "Postgres",
    "spec": {
        "properties": {
            "host": {"type": "string"},
            "port": {"type": "integer"},
            "ssl": {"type": "boolean"},
            "mode": {"enum": ["cdc", "xmin"]},
        }
    },
}


def test_templates_source_emits_skeleton(monkeypatch) -> None:
    client = _AirbyteFake({"GET /definitions/sources": {"data": [_SPEC_DEFINITION]}})
    result = _invoke(
        monkeypatch,
        client,
        [
            "ingest",
            "airbyte",
            "templates",
            "source",
            "-d",
            "sd1",
            "-w",
            "ws1",
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout) == {
        "host": "<host>",
        "port": 0,
        "ssl": False,
        "mode": "cdc",
    }


def test_templates_destination_emits_skeleton(monkeypatch) -> None:
    client = _AirbyteFake(
        {
            "GET /definitions/destinations": {
                "data": [
                    {
                        "destinationDefinitionId": "dd1",
                        "spec": {"properties": {"dataset": {"type": "string"}}},
                    },
                ]
            },
        }
    )
    result = _invoke(
        monkeypatch,
        client,
        [
            "ingest",
            "airbyte",
            "templates",
            "destination",
            "-d",
            "dd1",
            "-w",
            "ws1",
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout) == {"dataset": "<dataset>"}


def test_templates_skeleton_keeps_markup_property_names(monkeypatch) -> None:
    """The skeleton is machine-readable output: markup must survive verbatim."""
    client = _AirbyteFake(
        {
            "GET /definitions/sources": {
                "data": [
                    {
                        "sourceDefinitionId": "sd1",
                        "spec": {"properties": {STYLE_TAG: {"type": "string"}}},
                    },
                ]
            },
        }
    )
    result = _invoke(
        monkeypatch,
        client,
        ["ingest", "airbyte", "templates", "source", "-d", "sd1", "-w", "ws1"],
    )
    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout) == {STYLE_TAG: f"<{STYLE_TAG}>"}


def test_templates_missing_definition_renders_id_literally(monkeypatch) -> None:
    client = _AirbyteFake({"GET /definitions/sources": {"data": [_SPEC_DEFINITION]}})
    result = _invoke(
        monkeypatch,
        client,
        [
            "ingest",
            "airbyte",
            "templates",
            "source",
            "-d",
            CLOSING_TAG,
            "-w",
            STYLE_TAG,
        ],
    )
    assert result.exit_code == 1
    _assert_literal(result.stdout, CLOSING_TAG, STYLE_TAG)


def test_templates_output_path_with_markup(monkeypatch, tmp_path) -> None:
    client = _AirbyteFake({"GET /definitions/sources": {"data": [_SPEC_DEFINITION]}})
    # A path can hold no "/" per component, but the joined string still can:
    # "closes [" + "/" + "issue] 42.json" is an unbalanced closing tag.
    target = tmp_path / "closes [" / "issue] 42.json"
    target.parent.mkdir()
    result = _invoke(
        monkeypatch,
        client,
        [
            "ingest",
            "airbyte",
            "templates",
            "source",
            "-d",
            "sd1",
            "-w",
            "ws1",
            "-o",
            str(target),
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert json.loads(target.read_text())["host"] == "<host>"
    _assert_literal(result.stdout, CLOSING_TAG)


# --- tags ----------------------------------------------------------------

_TAGS_URL = "GET /api/public/v1/tags"


def test_tags_list_emits_json(monkeypatch) -> None:
    client = _AirbyteFake({_TAGS_URL: {"data": [{"tagId": "t1", "name": "hourly"}]}})
    result = _invoke(monkeypatch, client, ["ingest", "airbyte", "tags", "list"])
    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout) == [{"tagId": "t1", "name": "hourly"}]


def test_tags_create_posts_and_echoes(monkeypatch) -> None:
    client = _AirbyteFake(
        {
            "POST /api/public/v1/tags": {"tagId": "t2", "name": "nightly"},
        }
    )
    result = _invoke(
        monkeypatch,
        client,
        ["ingest", "airbyte", "tags", "create", "--name", "nightly"],
    )
    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout) == {"tagId": "t2", "name": "nightly"}
    assert client.posted[0][1] == {"name": "nightly"}


def test_tags_list_keeps_markup_bytes(monkeypatch) -> None:
    """Regression: a tag named "[/issue]" used to kill `tags list` outright."""
    client = _AirbyteFake(
        {
            _TAGS_URL: {
                "data": [
                    {"tagId": "t1", "name": CLOSING_TAG},
                    {"tagId": "t2", "name": STYLE_TAG},
                ]
            },
        }
    )
    result = _invoke(monkeypatch, client, ["ingest", "airbyte", "tags", "list"])
    assert result.exit_code == 0, result.stdout
    names = [tag["name"] for tag in json.loads(result.stdout)]
    assert names == [CLOSING_TAG, STYLE_TAG]


# --- jobs ----------------------------------------------------------------

_JOB = {
    "jobId": 91,
    "connectionId": "c1",
    "jobType": "sync",
    "status": "succeeded",
    "startTime": "2024-06-01T00:00:00Z",
    "duration": "PT2M",
    "rowsSynced": 1234,
}


def test_jobs_list_table(monkeypatch) -> None:
    client = _AirbyteFake({"GET /api/public/v1/jobs": {"data": [_JOB]}})
    result = _invoke(monkeypatch, client, ["ingest", "airbyte", "jobs", "list"])
    assert result.exit_code == 0, result.stdout
    _assert_literal(result.stdout, "91", "succeeded", "1234")


def test_jobs_list_json(monkeypatch) -> None:
    client = _AirbyteFake({"GET /api/public/v1/jobs": {"data": [_JOB]}})
    result = _invoke(
        monkeypatch, client, ["ingest", "airbyte", "jobs", "list", "--json"]
    )
    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout)[0]["jobId"] == 91


def test_jobs_list_empty(monkeypatch) -> None:
    client = _AirbyteFake({})
    result = _invoke(monkeypatch, client, ["ingest", "airbyte", "jobs", "list"])
    assert result.exit_code == 0, result.stdout
    assert "No jobs found" in result.stdout


def test_jobs_get_emits_json(monkeypatch) -> None:
    client = _AirbyteFake({"GET /api/public/v1/jobs/": _JOB})
    result = _invoke(monkeypatch, client, ["ingest", "airbyte", "jobs", "get", "91"])
    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout) == _JOB


def test_jobs_cancel_with_yes(monkeypatch) -> None:
    client = _AirbyteFake({"DELETE /api/public/v1/jobs/": {"status": "cancelled"}})
    result = _invoke(
        monkeypatch, client, ["ingest", "airbyte", "jobs", "cancel", "91", "--yes"]
    )
    assert result.exit_code == 0, result.stdout
    assert client.deleted == ["http://test/api/public/v1/jobs/91"]
    assert "Cancellation requested for job 91" in result.stdout


def test_jobs_table_renders_markup_literally(monkeypatch) -> None:
    """A failure status or connection id carrying markup must render verbatim."""
    client = _AirbyteFake(
        {
            "GET /api/public/v1/jobs": {
                "data": [
                    {**_JOB, "connectionId": CLOSING_TAG, "status": STYLE_TAG},
                ]
            },
        }
    )
    result = _invoke(monkeypatch, client, ["ingest", "airbyte", "jobs", "list"])
    assert result.exit_code == 0, result.stdout
    _assert_literal(result.stdout, CLOSING_TAG, STYLE_TAG)


# --- connections / resource tables ---------------------------------------


def test_connections_table_renders_markup_literally(monkeypatch) -> None:
    """Regression for the crash: `[/issue]` in a name aborted `connections list`."""
    client = _AirbyteFake(
        {
            "GET /api/public/v1/connections": {
                "data": [
                    {
                        "connectionId": "c1",
                        "name": CLOSING_TAG,
                        "status": "active",
                        "schedule": {
                            "scheduleType": "cron",
                            "cronExpression": STYLE_TAG,
                        },
                    },
                ]
            },
        }
    )
    result = _invoke(monkeypatch, client, ["ingest", "airbyte", "connections", "list"])
    assert result.exit_code == 0, result.stdout
    _assert_literal(result.stdout, CLOSING_TAG, STYLE_TAG)


def test_connections_all_columns_headers_render_markup_literally(monkeypatch) -> None:
    """--all-columns turns Airbyte's JSON keys into headers, so keys are hostile too.

    Rich parses markup in a header exactly as in a cell: before the fix the
    unbalanced tag raised MarkupError and the style tag was swallowed.
    """
    client = _AirbyteFake(
        {
            "GET /api/public/v1/connections": {
                "data": [
                    {
                        "connectionId": "c1",
                        CLOSING_TAG: "header-was-hostile",
                        STYLE_TAG: "header-was-styled",
                    },
                ]
            },
        }
    )
    result = _invoke(
        monkeypatch,
        client,
        ["ingest", "airbyte", "connections", "list", "--all-columns"],
    )
    assert result.exit_code == 0, result.stdout
    _assert_literal(result.stdout, CLOSING_TAG, STYLE_TAG)


def test_sources_table_renders_markup_literally(monkeypatch) -> None:
    client = _AirbyteFake(
        {
            "GET /api/public/v1/sources": {
                "data": [
                    {
                        "sourceId": "s1",
                        "name": CLOSING_TAG,
                        "sourceName": STYLE_TAG,
                        "workspaceId": "ws1",
                    },
                ]
            },
        }
    )
    result = _invoke(monkeypatch, client, ["ingest", "airbyte", "sources", "list"])
    assert result.exit_code == 0, result.stdout
    _assert_literal(result.stdout, CLOSING_TAG, STYLE_TAG)


def test_sources_get_keeps_markup_bytes(monkeypatch) -> None:
    client = _AirbyteFake(
        {
            "GET /api/public/v1/sources/": {"sourceId": "s1", "name": CLOSING_TAG},
        }
    )
    result = _invoke(
        monkeypatch,
        client,
        ["ingest", "airbyte", "sources", "get", "--source-id", "s1"],
    )
    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout)["name"] == CLOSING_TAG


def test_workspaces_table_renders_markup_literally(monkeypatch) -> None:
    client = _AirbyteFake(
        {
            "GET /api/public/v1/workspaces": {
                "data": [
                    {"workspaceId": "ws1", "name": CLOSING_TAG},
                ]
            },
        }
    )
    result = _invoke(monkeypatch, client, ["ingest", "airbyte", "workspaces", "list"])
    assert result.exit_code == 0, result.stdout
    _assert_literal(result.stdout, CLOSING_TAG)


def test_set_cursor_plan_renders_markup_literally(monkeypatch) -> None:
    """Stream names and cursor values are connector-defined, so also hostile."""
    _disable_envrc(monkeypatch)
    client = _StateFakeClient(
        state={
            "connectionId": "c1",
            "stateType": "stream",
            "streamState": [
                {
                    "streamDescriptor": {"name": CLOSING_TAG, "namespace": "public"},
                    "streamState": {"updated_at": "2024-06-01T00:00:00Z"},
                },
                {
                    "streamDescriptor": {"name": "events", "namespace": "public"},
                    "streamState": {"cdc_lsn": STYLE_TAG},
                },
            ],
        }
    )
    _patch_state_client(monkeypatch, client)
    result = runner.invoke(
        main_module.app,
        [
            "ingest",
            "airbyte",
            "connections",
            "set-cursor",
            "-c",
            "c1",
            "--to",
            "2024-01-01",
            "--dry-run",
        ],
        env=WIDE,
    )
    assert result.exit_code == 0, result.stdout
    _assert_literal(result.stdout, CLOSING_TAG, STYLE_TAG)


# --- shared error funnel -------------------------------------------------
#
# Every airbyte resource command reports failures through airbyte_client(), so
# its two handlers decide whether a hostile provider message reaches the user
# intact or takes the process down with a MarkupError mid-render.

_FUNNEL_MESSAGE = f"{CLOSING_TAG} / {STYLE_TAG}"


def _raiser(exc: Exception):
    """A stand-in that raises ``exc`` whatever it is called with."""

    def fail(*args, **kwargs):
        raise exc

    return fail


@pytest.mark.parametrize("error_type", [ConfigError, AuthError])
def test_airbyte_client_reports_startup_failures_literally(
    monkeypatch, error_type
) -> None:
    """A ConfigError/AuthError message is provider text: escaped, exit 1."""
    import dataplat.cli.ingest.airbyte._common as _common

    _disable_envrc(monkeypatch)
    monkeypatch.setattr(
        _common, "build_authenticated_client", _raiser(error_type(_FUNNEL_MESSAGE))
    )

    result = runner.invoke(
        main_module.app,
        ["ingest", "airbyte", "connections", "refresh", "-c", "c1"],
        env=WIDE,
    )
    assert result.exit_code == 1
    _assert_literal(result.stdout, CLOSING_TAG, STYLE_TAG)


def test_airbyte_client_reports_service_errors_literally(monkeypatch) -> None:
    """ServiceError carries the API response body verbatim, so it is hostile."""
    import dataplat.services.airbyte.jobs as _jobs_svc

    client = _AirbyteFake({})
    _disable_envrc(monkeypatch)
    _patch_airbyte_client(monkeypatch, client)
    monkeypatch.setattr(
        _jobs_svc, "trigger_job", _raiser(ServiceError(_FUNNEL_MESSAGE))
    )

    result = runner.invoke(
        main_module.app,
        ["ingest", "airbyte", "connections", "refresh", "-c", "c1"],
        env=WIDE,
    )
    assert result.exit_code == 1
    _assert_literal(result.stdout, CLOSING_TAG, STYLE_TAG)


# --- startup cost --------------------------------------------------------


def test_importing_connections_does_not_load_textual() -> None:
    """textual costs ~58 ms, so no `dp` run may pay for it before --tui.

    Asserted in a subprocess: another test opening the TUI module would
    otherwise leave textual in this interpreter's sys.modules.
    """
    probe = (
        "import sys, importlib;"
        "importlib.import_module('dataplat.cli.ingest.airbyte.connections');"
        "loaded = sorted(m for m in sys.modules if m.split('.')[0] == 'textual');"
        "print(loaded);"
        "sys.exit(1 if loaded else 0)"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True
    )
    assert result.returncode == 0, f"textual was imported: {result.stdout}"
