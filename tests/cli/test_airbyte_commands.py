"""CLI integration tests for Airbyte commands with monkeypatched client."""

from __future__ import annotations

import json
import subprocess
import sys

import httpx
import pytest
from typer.testing import CliRunner

import dataplat.main as main_module
import dataplat.services.airbyte.client as airbyte_client
from dataplat.core.errors import AuthError, ConfigError, ExitCode, ServiceError

runner = CliRunner()


def _response(data, status_code: int = 200) -> httpx.Response:
    """A real ``httpx.Response``, not a stand-in for one.

    Two hand-rolled fakes used to live here, each re-implementing json(), text,
    headers and raise_for_status. They drifted: one returned ``None`` from
    json() for a body that was not JSON, where a real response raises -- which
    hid error bodies from the code under test and from the tests asserting on
    them. A real response cannot drift from itself.

    The request is supplied because ``raise_for_status`` needs one to build its
    error; httpx raises a RuntimeError without it.
    """
    return httpx.Response(
        status_code,
        json=data if data is not None else {},
        request=httpx.Request("GET", "http://test"),
    )


def _disable_envrc(monkeypatch) -> None:
    monkeypatch.setattr(main_module, "load_envrc", lambda: None)


def _mock_authenticated_client(monkeypatch):
    """Monkeypatch build_authenticated_client to return a fake client.

    The CLI modules import build_authenticated_client directly, so we patch
    in each CLI module's namespace as well as the service module.
    """

    import dataplat.cli.ingest.airbyte.connections as _airbyte_connections_cli
    import dataplat.cli.ingest.airbyte.definitions as _airbyte_definitions_cli
    import dataplat.cli.ingest.airbyte.workspaces as _airbyte_workspaces_cli

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
                    return _response(
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
                return _response({"data": []})
            if "destinations" in url and "definitions" not in url:
                count = self._increment("destinations")
                if count == 1:
                    return _response(
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
                return _response({"data": []})
            if "workspaces" in url:
                count = self._increment("workspaces")
                if count == 1:
                    return _response(
                        {"data": [{"workspaceId": "ws1", "name": "Default"}]}
                    )
                return _response({"data": []})
            return _response({"data": []})

        def post(self, url, **kwargs):
            return _response({"sourceId": "s1", "name": "Created"})

        def patch(self, url, **kwargs):
            return _response({"sourceId": "s1", "name": "Updated"})

        def delete(self, url, **kwargs):
            return _response(None, status_code=204)

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
        self.patches: list[tuple[str, dict]] = []

    def get(self, url, params=None, **kw):
        if url.endswith("/api/public/v1/connections"):
            # one page then empty, to stop pagination
            if self._conn_listed:
                return _response({"data": []})
            self._conn_listed = True
            return _response(
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
            return _response(
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
            return _response({"data": data})
        return _response({"data": []})

    def post(self, url, json=None, **kw):
        self.posts.append((url, json or {}))
        if url.endswith("/api/v1/state/get"):
            return _response(self._state)
        if url.endswith("/api/v1/state/create_or_update"):
            return _response({"ok": True})
        if url.endswith("/api/public/v1/jobs"):  # refresh trigger
            return _response({"jobId": 42})
        return _response({})

    def patch(self, url, json=None, **kw):
        self.patches.append((url, json or {}))
        return _response({"connectionId": "c1"})

    def close(self):
        pass

    @property
    def headers(self):
        return {}


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
            return _response({"data": []})
        self._seen.add(url)
        return _response(self._payload("GET", url))

    def post(self, url, json=None, **kwargs):
        self.posted.append((url, json))
        return _response(self._payload("POST", url))

    def delete(self, url, **kwargs):
        self.deleted.append(url)
        return _response(self._payload("DELETE", url))

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


@pytest.mark.parametrize(
    ("error_type", "code"),
    [(ConfigError, ExitCode.CONFIG), (AuthError, ExitCode.AUTH)],
)
def test_airbyte_client_reports_startup_failures_literally(
    monkeypatch, error_type, code
) -> None:
    """A ConfigError/AuthError message is provider text: escaped, own code.

    The code is what the funnel used to get wrong: a missing AIRBYTE_BASE_URL
    and a rejected credential both exited 1, so a script could tell that
    something broke and nothing else. They are 3 and 4 now.
    """
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
    assert result.exit_code == code
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
    assert result.exit_code == ExitCode.SERVICE
    _assert_literal(result.stdout, CLOSING_TAG, STYLE_TAG)


def test_broad_handler_still_reads_the_code_off_the_exception(monkeypatch) -> None:
    """`connections get` reports through `except Exception`, not the funnel.

    It has to: the prefix it prints is the only thing that identifies the call
    when what escapes is an untyped KeyError. That breadth also swallowed every
    ServiceError, so a 500 from the API exited 1 there while the same failure
    exited 5 everywhere else. Both cases are pinned, because the fix is only
    correct if the untyped one did not move.
    """
    import dataplat.cli.ingest.airbyte.connections as _conn

    _disable_envrc(monkeypatch)
    _patch_airbyte_client(monkeypatch, _AirbyteFake({}))

    monkeypatch.setattr(_conn, "get_connection", _raiser(ServiceError("502 upstream")))
    typed = runner.invoke(
        main_module.app,
        ["ingest", "airbyte", "connections", "get", "-c", "c1"],
        env=WIDE,
    )
    assert typed.exit_code == ExitCode.SERVICE
    assert "Error getting connection: 502 upstream" in typed.stdout

    monkeypatch.setattr(_conn, "get_connection", _raiser(KeyError("streams")))
    untyped = runner.invoke(
        main_module.app,
        ["ingest", "airbyte", "connections", "get", "-c", "c1"],
        env=WIDE,
    )
    assert untyped.exit_code == ExitCode.FAILURE
    assert "Error getting connection:" in untyped.stdout


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


# ---------------------------------------------------------------------------
# --format json must survive the terminal it is printed to
# ---------------------------------------------------------------------------
# Rich wraps at the console width, and a token longer than that width is folded
# mid-token -- which puts a newline inside a JSON string literal. The output
# then looks right on screen and is unparseable the moment it is piped, which
# is the only reason --format json exists. Airbyte payloads carry exactly such
# tokens: connector config URLs, tokens and uuids.

LONG_TOKEN = (
    "https://example.com/very/long/path/that/exceeds/any/sane/console/width"
    "?token=abcdefghijklmnopqrstuvwxyz"
)


def _narrow(monkeypatch, module) -> None:
    """Force the module console to a width the token cannot fit in.

    Not ``CliRunner(env={"COLUMNS": ...})``: tests/conftest.py pins COLUMNS=200
    for the whole process and that wins, so an env-based version of this test
    passes without ever rendering narrow — which is exactly how this bug
    survived having tests around it.
    """
    monkeypatch.setattr(module.console, "width", 40)


def _serve_workspaces(monkeypatch, workspaces: list[dict]) -> None:
    import dataplat.cli.ingest.airbyte.workspaces as cli

    _disable_envrc(monkeypatch)
    _mock_authenticated_client(monkeypatch)
    _narrow(monkeypatch, cli)
    monkeypatch.setattr(cli, "list_workspaces", lambda client, base_url: workspaces)
    monkeypatch.setattr(
        cli, "get_workspace", lambda client, base_url, ws_id: workspaces[0]
    )


def test_workspaces_list_json_is_parseable_in_a_narrow_terminal(monkeypatch) -> None:
    payload = [{"workspaceId": "ws1", "name": LONG_TOKEN}]
    _serve_workspaces(monkeypatch, payload)

    result = runner.invoke(
        main_module.app,
        ["ingest", "airbyte", "workspaces", "list", "--format", "json"],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == payload


def test_workspaces_list_json_is_byte_identical(monkeypatch) -> None:
    """--json is a pipe, not a view: no styling, no wrapping, no repr quoting."""
    payload = [{"workspaceId": "ws1", "name": LONG_TOKEN}]
    _serve_workspaces(monkeypatch, payload)

    result = runner.invoke(
        main_module.app,
        ["ingest", "airbyte", "workspaces", "list", "--format", "json"],
    )

    assert result.stdout == json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def test_workspaces_get_json_is_parseable_in_a_narrow_terminal(monkeypatch) -> None:
    """``workspaces get`` emits JSON only, and had no test at all."""
    payload = [{"workspaceId": "ws1", "name": LONG_TOKEN}]
    _serve_workspaces(monkeypatch, payload)

    result = runner.invoke(
        main_module.app,
        ["ingest", "airbyte", "workspaces", "get", "-w", "ws1"],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == payload[0]


def test_workspaces_list_reports_an_empty_listing(monkeypatch) -> None:
    _serve_workspaces(monkeypatch, [])

    result = runner.invoke(main_module.app, ["ingest", "airbyte", "workspaces", "list"])

    assert result.exit_code == 0, result.output
    assert "No workspaces found" in result.output


def test_workspaces_list_reports_a_service_failure(monkeypatch) -> None:
    import dataplat.cli.ingest.airbyte.workspaces as cli

    _disable_envrc(monkeypatch)
    _mock_authenticated_client(monkeypatch)

    def boom(client, base_url):
        raise ServiceError("Failed to list workspaces (503 Service Unavailable): down")

    monkeypatch.setattr(cli, "list_workspaces", boom)

    result = runner.invoke(main_module.app, ["ingest", "airbyte", "workspaces", "list"])

    assert result.exit_code == ExitCode.SERVICE
    assert "503 Service Unavailable" in result.output


def test_workspaces_get_reports_a_service_failure(monkeypatch) -> None:
    import dataplat.cli.ingest.airbyte.workspaces as cli

    _disable_envrc(monkeypatch)
    _mock_authenticated_client(monkeypatch)

    def boom(client, base_url, workspace_id):
        raise ServiceError("Failed to get workspace (404 Not Found): no such workspace")

    monkeypatch.setattr(cli, "get_workspace", boom)

    result = runner.invoke(
        main_module.app, ["ingest", "airbyte", "workspaces", "get", "-w", "nope"]
    )

    assert result.exit_code == ExitCode.SERVICE
    assert "404 Not Found" in result.output


# ---------------------------------------------------------------------------
# The mutating connection commands
# ---------------------------------------------------------------------------
# delete, reset and sync had no tests at all. Two of them destroy data.


def test_sync_refuses_wait_without_a_connection_before_authenticating(
    monkeypatch,
) -> None:
    """An argument error must not cost a round trip to Airbyte.

    --wait without --connection-id cannot work whatever Airbyte says, and the
    check used to run *after* the client was built -- so a contradiction
    between two flags was reported only once authentication had succeeded, and
    the client that authenticated was left unclosed on the way out.
    """
    _disable_envrc(monkeypatch)
    opened: list[bool] = []

    def factory():
        opened.append(True)
        raise AssertionError("should not authenticate for an argument error")

    import dataplat.cli.ingest.airbyte.connections as _conns

    monkeypatch.setattr(_conns, "build_authenticated_client", factory)

    result = runner.invoke(
        main_module.app, ["ingest", "airbyte", "connections", "sync", "--wait"]
    )

    assert result.exit_code == ExitCode.INVALID_INPUT
    assert opened == []


def test_sync_triggers_a_job_for_one_connection(monkeypatch) -> None:
    _disable_envrc(monkeypatch)
    client = _StateFakeClient(state=dict(_STREAM_STATE))
    _patch_state_client(monkeypatch, client)

    result = runner.invoke(
        main_module.app,
        ["ingest", "airbyte", "connections", "sync", "-c", "c1"],
    )

    assert result.exit_code == 0, result.output
    assert [p for u, p in client.posts if u.endswith("/api/public/v1/jobs")] == [
        {"connectionId": "c1", "jobType": "sync"}
    ]


def test_sync_dry_run_triggers_nothing(monkeypatch) -> None:
    _disable_envrc(monkeypatch)
    client = _StateFakeClient(state=dict(_STREAM_STATE))
    _patch_state_client(monkeypatch, client)

    result = runner.invoke(
        main_module.app,
        ["ingest", "airbyte", "connections", "sync", "-c", "c1", "--dry-run"],
    )

    assert result.exit_code == 0, result.output
    assert [p for u, p in client.posts if u.endswith("/api/public/v1/jobs")] == []


def test_reset_needs_a_confirmation(monkeypatch) -> None:
    """It drops the destination's data for the connection."""
    _disable_envrc(monkeypatch)
    client = _StateFakeClient(state=dict(_STREAM_STATE))
    _patch_state_client(monkeypatch, client)

    result = runner.invoke(
        main_module.app,
        ["ingest", "airbyte", "connections", "reset", "-c", "c1"],
    )

    assert result.exit_code != 0
    assert client.posts == []


def test_reset_triggers_a_reset_job(monkeypatch) -> None:
    _disable_envrc(monkeypatch)
    client = _StateFakeClient(state=dict(_STREAM_STATE))
    _patch_state_client(monkeypatch, client)

    result = runner.invoke(
        main_module.app,
        ["ingest", "airbyte", "connections", "reset", "-c", "c1", "-y"],
    )

    assert result.exit_code == 0, result.output
    assert [p for u, p in client.posts if u.endswith("/api/public/v1/jobs")] == [
        {"connectionId": "c1", "jobType": "reset"}
    ]


def test_clear_is_a_different_job_type(monkeypatch) -> None:
    _disable_envrc(monkeypatch)
    client = _StateFakeClient(state=dict(_STREAM_STATE))
    _patch_state_client(monkeypatch, client)

    result = runner.invoke(
        main_module.app,
        ["ingest", "airbyte", "connections", "reset", "-c", "c1", "--clear", "-y"],
    )

    assert result.exit_code == 0, result.output
    assert [p for u, p in client.posts if u.endswith("/api/public/v1/jobs")] == [
        {"connectionId": "c1", "jobType": "clear"}
    ]


def test_delete_needs_a_confirmation(monkeypatch) -> None:
    _disable_envrc(monkeypatch)
    _mock_authenticated_client(monkeypatch)

    result = runner.invoke(
        main_module.app,
        ["ingest", "airbyte", "connections", "delete", "-c", "c1"],
    )

    assert result.exit_code != 0
    assert "deleted" not in result.output


def test_delete_removes_the_connection(monkeypatch) -> None:
    _disable_envrc(monkeypatch)
    _mock_authenticated_client(monkeypatch)

    result = runner.invoke(
        main_module.app,
        ["ingest", "airbyte", "connections", "delete", "-c", "c1", "-y"],
    )

    assert result.exit_code == 0, result.output
    assert "deleted" in result.output


# ---------------------------------------------------------------------------
# connections update
# ---------------------------------------------------------------------------
# The largest command in the CLI and the least covered. Its validation runs
# before any client is built, so most of these need no Airbyte at all -- which
# is the point: a contradiction between flags should cost nothing to report.


def _update(*args: str, monkeypatch, client=None):
    _disable_envrc(monkeypatch)
    if client is not None:
        _patch_state_client(monkeypatch, client)
    return runner.invoke(
        main_module.app, ["ingest", "airbyte", "connections", "update", *args]
    )


def test_cron_schedule_without_a_cron_expression_is_refused(monkeypatch) -> None:
    result = _update("-c", "c1", "--schedule-type", "cron", monkeypatch=monkeypatch)

    assert result.exit_code == ExitCode.INVALID_INPUT
    assert "--cron required" in result.output


def test_an_unparseable_cron_expression_is_refused(monkeypatch) -> None:
    result = _update("-c", "c1", "--cron", "not a cron", monkeypatch=monkeypatch)

    assert result.exit_code == ExitCode.INVALID_INPUT
    assert "Invalid cron" in result.output


def test_an_unknown_timezone_is_refused(monkeypatch) -> None:
    result = _update(
        "-c",
        "c1",
        "--cron",
        "0 0 12 ? * *",
        "--cron-timezone",
        "Mars/Olympus_Mons",
        monkeypatch=monkeypatch,
    )

    assert result.exit_code == ExitCode.INVALID_INPUT
    assert "Invalid cron timezone" in result.output


def test_a_timezone_needs_the_web_backend(monkeypatch) -> None:
    """The public API has nowhere to put it, so asking for it is refused."""
    result = _update(
        "-c",
        "c1",
        "--cron",
        "0 0 12 ? * * Europe/Berlin",
        monkeypatch=monkeypatch,
    )

    assert result.exit_code == ExitCode.INVALID_INPUT
    assert "--use-web-backend" in result.output


def test_a_cron_expression_implies_a_cron_schedule(monkeypatch) -> None:
    """--schedule-type would be the only thing --cron could mean."""
    client = _StateFakeClient(state=dict(_STREAM_STATE))

    result = _update(
        "-c", "c1", "--cron", "0 0 12 ? * *", monkeypatch=monkeypatch, client=client
    )

    assert result.exit_code == 0, result.output
    assert client.patches[0][1]["schedule"] == {
        "scheduleType": "cron",
        "cronExpression": "0 0 12 ? * *",
    }


def test_an_empty_prefix_clears_it_rather_than_being_ignored(monkeypatch) -> None:
    """`--prefix ""` is a value, not an absent option."""
    client = _StateFakeClient(state=dict(_STREAM_STATE))

    result = _update("-c", "c1", "--prefix", "", monkeypatch=monkeypatch, client=client)

    assert result.exit_code == 0, result.output
    assert client.patches[0][1] == {"prefix": ""}


def test_a_status_change_is_sent_as_itself(monkeypatch) -> None:
    client = _StateFakeClient(state=dict(_STREAM_STATE))

    result = _update(
        "-c", "c1", "--status", "inactive", monkeypatch=monkeypatch, client=client
    )

    assert result.exit_code == 0, result.output
    assert client.patches[0][1] == {"status": "inactive"}


def test_a_dry_run_updates_nothing(monkeypatch) -> None:
    client = _StateFakeClient(state=dict(_STREAM_STATE))

    result = _update(
        "-c",
        "c1",
        "--status",
        "inactive",
        "--dry-run",
        monkeypatch=monkeypatch,
        client=client,
    )

    assert result.exit_code == 0, result.output
    assert client.patches == []


def test_tag_from_cron_without_a_cron_is_refused(monkeypatch) -> None:
    result = _update("-c", "c1", "--tag-from-cron", monkeypatch=monkeypatch)

    assert result.exit_code == ExitCode.INVALID_INPUT
    assert "--tag-from-cron requires --cron" in result.output


# ---------------------------------------------------------------------------
# connections create
# ---------------------------------------------------------------------------


def _create(*args: str, monkeypatch):
    _disable_envrc(monkeypatch)
    _mock_authenticated_client(monkeypatch)
    return runner.invoke(
        main_module.app,
        [
            "ingest",
            "airbyte",
            "connections",
            "create",
            "--source-id",
            "s1",
            "--destination-id",
            "d1",
            *args,
        ],
    )


def test_create_refuses_a_cron_schedule_with_no_expression(monkeypatch) -> None:
    result = _create("--schedule-type", "cron", monkeypatch=monkeypatch)

    assert result.exit_code == ExitCode.INVALID_INPUT
    assert "--cron required" in result.output


def test_create_refuses_an_unparseable_cron(monkeypatch) -> None:
    result = _create("--cron", "whenever", monkeypatch=monkeypatch)

    assert result.exit_code == ExitCode.INVALID_INPUT
    assert "Invalid cron" in result.output


def test_create_emits_the_connection_as_json(monkeypatch) -> None:
    """The command's whole output is JSON, so it has to parse."""
    result = _create("--name", "new one", monkeypatch=monkeypatch)

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)


def test_create_sends_what_it_was_given(monkeypatch) -> None:
    import dataplat.cli.ingest.airbyte.connections as _conns

    _disable_envrc(monkeypatch)
    _mock_authenticated_client(monkeypatch)
    sent: dict = {}

    def spy(client, base_url, **kwargs):
        sent.update(kwargs)
        return {"connectionId": "c9"}

    monkeypatch.setattr(_conns, "create_connection", spy)

    result = runner.invoke(
        main_module.app,
        [
            "ingest",
            "airbyte",
            "connections",
            "create",
            "--source-id",
            "s1",
            "--destination-id",
            "d1",
            "--name",
            "nightly",
            "--cron",
            "0 0 3 ? * * Europe/Berlin",
            "--status",
            "inactive",
        ],
    )

    assert result.exit_code == 0, result.output
    assert sent["source_id"] == "s1"
    assert sent["destination_id"] == "d1"
    assert sent["name"] == "nightly"
    assert sent["status"] == "inactive"
    # The timezone travels in its own field, never inline in the expression.
    assert sent["schedule"] == {
        "scheduleType": "cron",
        "cronExpression": "0 0 3 ? * *",
        "cronTimeZone": "Europe/Berlin",
    }


# ---------------------------------------------------------------------------
# tags, and the shared source/destination resource app
# ---------------------------------------------------------------------------


def test_tags_create_passes_the_colour_and_workspace(monkeypatch) -> None:
    _disable_envrc(monkeypatch)
    import dataplat.cli.ingest.airbyte.tags as _tags

    client = _StateFakeClient(state={})
    sent: dict = {}

    def spy(c, b, name, workspace_id=None, color=None):
        sent.update(name=name, workspace_id=workspace_id, color=color)
        return {"tagId": "t9", "name": name}

    monkeypatch.setattr(
        _tags, "build_authenticated_client", lambda: (client, "http://t")
    )
    monkeypatch.setattr(_tags, "create_tag", spy)

    result = runner.invoke(
        main_module.app,
        [
            "ingest",
            "airbyte",
            "tags",
            "create",
            "--name",
            "nightly",
            "--workspace-id",
            "ws1",
            "--color",
            "75DCFF",
        ],
    )

    assert result.exit_code == 0, result.output
    assert sent == {"name": "nightly", "workspace_id": "ws1", "color": "75DCFF"}
    assert json.loads(result.stdout)["tagId"] == "t9"


def test_a_resource_update_needs_something_to_change(monkeypatch) -> None:
    """Neither --name nor --config is a request to do nothing."""
    _disable_envrc(monkeypatch)
    _mock_authenticated_client(monkeypatch)

    result = runner.invoke(
        main_module.app,
        ["ingest", "airbyte", "sources", "update", "--source-id", "s1"],
    )

    assert result.exit_code == ExitCode.INVALID_INPUT
    assert "at least one of" in result.output


def test_a_resource_config_file_that_is_missing_is_named(monkeypatch) -> None:
    _disable_envrc(monkeypatch)
    _mock_authenticated_client(monkeypatch)

    result = runner.invoke(
        main_module.app,
        [
            "ingest",
            "airbyte",
            "sources",
            "update",
            "--source-id",
            "s1",
            "--config",
            "/no/such/config.json",
        ],
    )

    assert result.exit_code == ExitCode.INVALID_INPUT
    assert "config file not found" in result.output


def test_a_resource_config_that_is_not_json_is_named(monkeypatch, tmp_path) -> None:
    _disable_envrc(monkeypatch)
    _mock_authenticated_client(monkeypatch)
    path = tmp_path / "config.json"
    path.write_text("{oops")

    result = runner.invoke(
        main_module.app,
        [
            "ingest",
            "airbyte",
            "sources",
            "update",
            "--source-id",
            "s1",
            "--config",
            str(path),
        ],
    )

    assert result.exit_code == ExitCode.INVALID_INPUT
    assert "invalid JSON" in result.output


def test_a_resource_update_sends_name_and_config(monkeypatch, tmp_path) -> None:
    _disable_envrc(monkeypatch)
    _mock_authenticated_client(monkeypatch)
    path = tmp_path / "config.json"
    path.write_text('{"host": "db.internal"}')

    result = runner.invoke(
        main_module.app,
        [
            "ingest",
            "airbyte",
            "sources",
            "update",
            "--source-id",
            "s1",
            "--name",
            "renamed",
            "--config",
            str(path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)


def test_tags_list_reports_a_service_failure(monkeypatch) -> None:
    _disable_envrc(monkeypatch)
    import dataplat.cli.ingest.airbyte.tags as _tags

    def boom(c, b):
        raise ServiceError("Failed to list tags (503 Service Unavailable): down")

    monkeypatch.setattr(
        _tags,
        "build_authenticated_client",
        lambda: (_StateFakeClient(state={}), "http://t"),
    )
    monkeypatch.setattr(_tags, "list_tags", boom)

    result = runner.invoke(main_module.app, ["ingest", "airbyte", "tags", "list"])

    assert result.exit_code == ExitCode.SERVICE
    assert "503" in result.output


def test_tags_create_reports_a_rejected_tag(monkeypatch) -> None:
    _disable_envrc(monkeypatch)
    import dataplat.cli.ingest.airbyte.tags as _tags

    def boom(c, b, name, workspace_id=None, color=None):
        raise ServiceError("Failed to create tag (422 Unprocessable Entity): dupe")

    monkeypatch.setattr(
        _tags,
        "build_authenticated_client",
        lambda: (_StateFakeClient(state={}), "http://t"),
    )
    monkeypatch.setattr(_tags, "create_tag", boom)

    result = runner.invoke(
        main_module.app, ["ingest", "airbyte", "tags", "create", "--name", "prod"]
    )

    assert result.exit_code == ExitCode.SERVICE
    assert "422" in result.output


def test_tags_list_reports_missing_configuration(monkeypatch) -> None:
    _disable_envrc(monkeypatch)
    import dataplat.cli.ingest.airbyte.tags as _tags

    def unconfigured():
        raise ConfigError("Set AIRBYTE_BASE_URL")

    monkeypatch.setattr(_tags, "build_authenticated_client", unconfigured)

    result = runner.invoke(main_module.app, ["ingest", "airbyte", "tags", "list"])

    assert result.exit_code == ExitCode.CONFIG
    assert "AIRBYTE_BASE_URL" in result.output


# --- bulk update ----------------------------------------------------------
# Without --connection-id, update walks every active connection. The gate and
# the per-connection failure handling are what make that safe to run.


def test_bulk_update_needs_a_confirmation(monkeypatch) -> None:
    client = _StateFakeClient(state=dict(_STREAM_STATE))

    result = _update("--status", "inactive", monkeypatch=monkeypatch, client=client)

    assert result.exit_code != 0
    assert client.patches == []


def test_bulk_update_walks_every_matching_connection(monkeypatch) -> None:
    client = _StateFakeClient(state=dict(_STREAM_STATE))

    result = _update(
        "--status", "inactive", "-y", monkeypatch=monkeypatch, client=client
    )

    assert result.exit_code == 0, result.output
    assert [payload for _, payload in client.patches] == [{"status": "inactive"}]
    assert "Updated: 1" in result.output


def test_bulk_dry_run_changes_nothing(monkeypatch) -> None:
    client = _StateFakeClient(state=dict(_STREAM_STATE))

    result = _update(
        "--status", "inactive", "--dry-run", monkeypatch=monkeypatch, client=client
    )

    assert result.exit_code == 0, result.output
    assert client.patches == []
    assert "dry run" in result.output


def test_a_connection_that_fails_does_not_stop_the_rest(monkeypatch) -> None:
    """One connection's failure is a warning, not the end of the run."""
    client = _StateFakeClient(state=dict(_STREAM_STATE))

    def boom(url, json=None, **kw):
        raise RuntimeError("connection is locked")

    monkeypatch.setattr(client, "patch", boom)

    result = _update(
        "--status", "inactive", "-y", monkeypatch=monkeypatch, client=client
    )

    assert result.exit_code == 0, result.output
    assert "Warning: Failed to update" in result.output
    assert "Updated: 0" in result.output


def test_a_source_filter_that_matches_nothing_says_so(monkeypatch) -> None:
    client = _StateFakeClient(state=dict(_STREAM_STATE))

    result = _update(
        "--status",
        "inactive",
        "--source-id",
        "other",
        "-y",
        monkeypatch=monkeypatch,
        client=client,
    )

    assert result.exit_code == 0, result.output
    assert "No matching active connections" in result.output
    assert client.patches == []


def test_a_single_update_skips_a_connection_the_filter_excludes(monkeypatch) -> None:
    """-c names one connection; --source-id can still veto it."""
    client = _StateFakeClient(state=dict(_STREAM_STATE))

    result = _update(
        "-c",
        "c1",
        "--status",
        "inactive",
        "--source-id",
        "other",
        monkeypatch=monkeypatch,
        client=client,
    )

    assert result.exit_code == 0, result.output
    assert "Skipping" in result.output
    assert client.patches == []


# --- list filters ---------------------------------------------------------
# Every filter is applied client-side over the paginated listing, so each one
# is a chance to hide a connection that should have been shown.

_CONNECTIONS_PAGE = {
    "data": [
        {
            "connectionId": "c1",
            "name": "active one",
            "status": "active",
            "sourceId": "s1",
            "destinationId": "d1",
            "workspaceId": "ws1",
        },
        {
            "connectionId": "c2",
            "name": "paused one",
            "status": "inactive",
            "sourceId": "s2",
            "destinationId": "d2",
            "workspaceId": "ws2",
        },
    ]
}


@pytest.mark.parametrize(
    ("flag", "value", "kept", "dropped"),
    [
        ("--status", "active", "active one", "paused one"),
        ("--source-id", "s2", "paused one", "active one"),
        ("--destination-id", "d1", "active one", "paused one"),
        ("--workspace-id", "ws2", "paused one", "active one"),
    ],
    ids=["status", "source", "destination", "workspace"],
)
def test_each_list_filter_keeps_only_what_it_names(
    monkeypatch, flag: str, value: str, kept: str, dropped: str
) -> None:
    client = _AirbyteFake({"GET /connections": _CONNECTIONS_PAGE})

    result = _invoke(
        monkeypatch,
        client,
        ["ingest", "airbyte", "connections", "list", flag, value],
    )

    assert result.exit_code == 0, result.stdout
    assert kept in result.stdout
    assert dropped not in result.stdout


def test_list_json_is_machine_readable(monkeypatch) -> None:
    client = _AirbyteFake({"GET /connections": _CONNECTIONS_PAGE})

    result = _invoke(
        monkeypatch, client, ["ingest", "airbyte", "connections", "list", "--json"]
    )

    assert result.exit_code == 0, result.stdout
    assert {c["connectionId"] for c in json.loads(result.stdout)} == {"c1", "c2"}
