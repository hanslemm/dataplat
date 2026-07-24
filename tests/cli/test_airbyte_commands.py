"""CLI integration tests for Airbyte commands with monkeypatched client."""

from __future__ import annotations

import json

from typer.testing import CliRunner

import dataplat.main as main_module
import dataplat.services.airbyte.client as airbyte_client

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
                    return FakeResponse({"data": [
                        {"sourceId": "s1", "name": "TestSource", "sourceName": "Postgres", "workspaceId": "ws1"}
                    ]})
                return FakeResponse({"data": []})
            if "destinations" in url and "definitions" not in url:
                count = self._increment("destinations")
                if count == 1:
                    return FakeResponse({"data": [
                        {"destinationId": "d1", "name": "TestDest", "destinationName": "BigQuery", "workspaceId": "ws1"}
                    ]})
                return FakeResponse({"data": []})
            if "workspaces" in url:
                count = self._increment("workspaces")
                if count == 1:
                    return FakeResponse({"data": [
                        {"workspaceId": "ws1", "name": "Default"}
                    ]})
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
    result = runner.invoke(main_module.app, ["ingest", "airbyte", "sources", "list", "--format", "json"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data[0]["sourceId"] == "s1"


def test_sources_create_with_config_file(monkeypatch, tmp_path) -> None:
    _disable_envrc(monkeypatch)
    _mock_authenticated_client(monkeypatch)
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({"host": "localhost", "port": 5432}))
    result = runner.invoke(main_module.app, [
        "ingest", "airbyte", "sources", "create",
        "--name", "NewSource", "--definition-id", "def1",
        "--workspace-id", "ws1", "--config", str(config_file),
    ])
    assert result.exit_code == 0
    assert "s1" in result.stdout


def test_sources_delete_with_yes(monkeypatch) -> None:
    _disable_envrc(monkeypatch)
    _mock_authenticated_client(monkeypatch)
    result = runner.invoke(main_module.app, [
        "ingest", "airbyte", "sources", "delete",
        "--source-id", "s1", "--yes",
    ])
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
    result = runner.invoke(main_module.app, ["ingest", "airbyte", "templates", "connection"])
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
            return _SFResponse({"data": [
                {"connectionId": "c1", "name": "Conn1", "status": "active",
                 "sourceId": "s1", "destinationId": "d1", "workspaceId": "ws1"}
            ]})
        if "/api/public/v1/connections/" in url:  # get_connection (single -c)
            return _SFResponse({"connectionId": "c1", "name": "Conn1",
                                "status": "active", "sourceId": "s1",
                                "destinationId": "d1"})
        if url.endswith("/api/public/v1/jobs"):     # list_jobs (busy check)
            data = [{"jobId": 1, "status": "running"}] if self._running else []
            return _SFResponse({"data": data})
        return _SFResponse({"data": []})

    def post(self, url, json=None, **kw):
        self.posts.append((url, json or {}))
        if url.endswith("/api/v1/state/get"):
            return _SFResponse(self._state)
        if url.endswith("/api/v1/state/create_or_update"):
            return _SFResponse({"ok": True})
        if url.endswith("/api/public/v1/jobs"):     # refresh trigger
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
            raise httpx.HTTPStatusError("e", request=httpx.Request("POST", "http://t"), response=self)

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
        {"streamDescriptor": {"name": "orders", "namespace": "public"},
         "streamState": {"updated_at": "2024-06-01T00:00:00Z"}},
        {"streamDescriptor": {"name": "events", "namespace": "public"},
         "streamState": {"id": 987654}},
    ],
}


def test_set_cursor_dry_run_writes_nothing(monkeypatch) -> None:
    _disable_envrc(monkeypatch)
    client = _StateFakeClient(state=dict(_STREAM_STATE))
    _patch_state_client(monkeypatch, client)
    result = runner.invoke(main_module.app, [
        "ingest", "airbyte", "connections", "set-cursor",
        "-c", "c1", "--to", "2024-01-01", "--dry-run",
    ])
    assert result.exit_code == 0
    # planned the rewrite in output, but never wrote state
    assert "orders" in result.stdout
    assert "skip:opaque" in result.stdout or "events" in result.stdout
    assert not any(u.endswith("/api/v1/state/create_or_update") for u, _ in client.posts)


def test_set_cursor_writes_rewritten_state(monkeypatch) -> None:
    _disable_envrc(monkeypatch)
    client = _StateFakeClient(state={
        "connectionId": "c1", "stateType": "stream",
        "streamState": [
            {"streamDescriptor": {"name": "orders", "namespace": "public"},
             "streamState": {"updated_at": "2024-06-01T00:00:00Z"}},
            {"streamDescriptor": {"name": "events", "namespace": "public"},
             "streamState": {"id": 987654}},
        ],
    })
    _patch_state_client(monkeypatch, client)
    result = runner.invoke(main_module.app, [
        "ingest", "airbyte", "connections", "set-cursor",
        "-c", "c1", "--to", "2024-01-01", "--yes",
    ])
    assert result.exit_code == 0
    writes = [p for u, p in client.posts if u.endswith("/api/v1/state/create_or_update")]
    assert len(writes) == 1
    written_state = writes[0]["connectionState"]
    streams = {s["streamDescriptor"]["name"]: s["streamState"] for s in written_state["streamState"]}
    assert streams["orders"]["updated_at"] == "2024-01-01T00:00:00Z"  # rewritten
    assert streams["events"]["id"] == 987654                          # opaque untouched


def test_set_cursor_skips_busy_connection(monkeypatch) -> None:
    _disable_envrc(monkeypatch)
    client = _StateFakeClient(state=dict(_STREAM_STATE), running=True)
    _patch_state_client(monkeypatch, client)
    result = runner.invoke(main_module.app, [
        "ingest", "airbyte", "connections", "set-cursor",
        "-c", "c1", "--to", "2024-01-01", "--yes",
    ])
    assert result.exit_code == 0
    assert "busy" in result.stdout.lower()
    assert not any(u.endswith("/api/v1/state/create_or_update") for u, _ in client.posts)


def test_set_cursor_force_writes_busy_connection(monkeypatch) -> None:
    _disable_envrc(monkeypatch)
    client = _StateFakeClient(state=dict(_STREAM_STATE), running=True)
    _patch_state_client(monkeypatch, client)
    result = runner.invoke(main_module.app, [
        "ingest", "airbyte", "connections", "set-cursor",
        "-c", "c1", "--to", "2024-01-01", "--yes", "--force",
    ])
    assert result.exit_code == 0
    assert any(u.endswith("/api/v1/state/create_or_update") for u, _ in client.posts)


def test_set_cursor_bad_date_exits_2(monkeypatch) -> None:
    _disable_envrc(monkeypatch)
    client = _StateFakeClient(state=dict(_STREAM_STATE))
    _patch_state_client(monkeypatch, client)
    result = runner.invoke(main_module.app, [
        "ingest", "airbyte", "connections", "set-cursor",
        "-c", "c1", "--to", "not-a-date", "--yes",
    ])
    assert result.exit_code == 2


_XMIN_STATE = {
    "connectionId": "c1",
    "stateType": "stream",
    "streamState": [
        {"streamDescriptor": {"name": "orders", "namespace": "public"},
         "streamState": {
             "state_type": "xmin", "version": 2,
             "xmin_xid_value": 1000, "xmin_raw_value": 1000, "num_wraparound": 0,
         }},
    ],
}


def _written_state(client):
    writes = [p for u, p in client.posts if u.endswith("/api/v1/state/create_or_update")]
    assert len(writes) == 1
    return writes[0]["connectionState"]


def test_set_cursor_xmin_factor_scales(monkeypatch) -> None:
    _disable_envrc(monkeypatch)
    client = _StateFakeClient(state=json.loads(json.dumps(_XMIN_STATE)))
    _patch_state_client(monkeypatch, client)
    result = runner.invoke(main_module.app, [
        "ingest", "airbyte", "connections", "set-cursor",
        "-c", "c1", "--xmin-factor", "0.1", "--yes",
    ])
    assert result.exit_code == 0
    inner = _written_state(client)["streamState"][0]["streamState"]
    assert inner["xmin_xid_value"] == 100
    assert inner["xmin_raw_value"] == 100


def test_set_cursor_xmin_absolute(monkeypatch) -> None:
    _disable_envrc(monkeypatch)
    client = _StateFakeClient(state=json.loads(json.dumps(_XMIN_STATE)))
    _patch_state_client(monkeypatch, client)
    result = runner.invoke(main_module.app, [
        "ingest", "airbyte", "connections", "set-cursor",
        "-c", "c1", "--xmin", "0", "--yes",
    ])
    assert result.exit_code == 0
    inner = _written_state(client)["streamState"][0]["streamState"]
    assert inner["xmin_xid_value"] == 0


def test_set_cursor_requires_at_least_one_op(monkeypatch) -> None:
    _disable_envrc(monkeypatch)
    client = _StateFakeClient(state=dict(_STREAM_STATE))
    _patch_state_client(monkeypatch, client)
    result = runner.invoke(main_module.app, [
        "ingest", "airbyte", "connections", "set-cursor", "-c", "c1", "--yes",
    ])
    assert result.exit_code == 2


def test_set_cursor_xmin_flags_mutually_exclusive(monkeypatch) -> None:
    _disable_envrc(monkeypatch)
    client = _StateFakeClient(state=dict(_STREAM_STATE))
    _patch_state_client(monkeypatch, client)
    result = runner.invoke(main_module.app, [
        "ingest", "airbyte", "connections", "set-cursor",
        "-c", "c1", "--xmin", "5", "--xmin-factor", "0.5", "--yes",
    ])
    assert result.exit_code == 2


def test_set_cursor_combined_date_and_xmin(monkeypatch) -> None:
    _disable_envrc(monkeypatch)
    client = _StateFakeClient(state={
        "connectionId": "c1", "stateType": "stream",
        "streamState": [
            {"streamDescriptor": {"name": "orders", "namespace": "public"},
             "streamState": {"updated_at": "2024-06-01T00:00:00Z"}},
            {"streamDescriptor": {"name": "xm", "namespace": "public"},
             "streamState": {
                 "state_type": "xmin", "version": 2,
                 "xmin_xid_value": 1000, "xmin_raw_value": 1000,
             }},
        ],
    })
    _patch_state_client(monkeypatch, client)
    result = runner.invoke(main_module.app, [
        "ingest", "airbyte", "connections", "set-cursor",
        "-c", "c1", "--to", "2024-01-01", "--xmin-factor", "0.1", "--yes",
    ])
    assert result.exit_code == 0
    streams = {s["streamDescriptor"]["name"]: s["streamState"]
               for s in _written_state(client)["streamState"]}
    assert streams["orders"]["updated_at"] == "2024-01-01T00:00:00Z"
    assert streams["xm"]["xmin_xid_value"] == 100


def test_set_cursor_sync_triggers_after_write(monkeypatch) -> None:
    _disable_envrc(monkeypatch)
    client = _StateFakeClient(state=json.loads(json.dumps(_XMIN_STATE)))
    _patch_state_client(monkeypatch, client)
    result = runner.invoke(main_module.app, [
        "ingest", "airbyte", "connections", "set-cursor",
        "-c", "c1", "--xmin", "0", "--sync", "--yes",
    ])
    assert result.exit_code == 0
    # state written AND a sync job triggered for the same connection
    assert any(u.endswith("/api/v1/state/create_or_update") for u, _ in client.posts)
    job_posts = [p for u, p in client.posts if u.endswith("/api/public/v1/jobs")]
    assert {"connectionId": "c1", "jobType": "sync"} in job_posts


def test_set_cursor_sync_dry_run_triggers_nothing(monkeypatch) -> None:
    _disable_envrc(monkeypatch)
    client = _StateFakeClient(state=json.loads(json.dumps(_XMIN_STATE)))
    _patch_state_client(monkeypatch, client)
    result = runner.invoke(main_module.app, [
        "ingest", "airbyte", "connections", "set-cursor",
        "-c", "c1", "--xmin", "0", "--sync", "--dry-run",
    ])
    assert result.exit_code == 0
    assert not any(u.endswith("/api/public/v1/jobs") and p.get("jobType") == "sync"
                   for u, p in client.posts)
    assert not any(u.endswith("/api/v1/state/create_or_update") for u, _ in client.posts)


def test_set_cursor_sync_skipped_when_no_rewrite(monkeypatch) -> None:
    _disable_envrc(monkeypatch)
    # events-only state: nothing matches the xmin op, so no write and no sync
    client = _StateFakeClient(state={
        "connectionId": "c1", "stateType": "stream",
        "streamState": [
            {"streamDescriptor": {"name": "events", "namespace": "public"},
             "streamState": {"id": 987654}},
        ],
    })
    _patch_state_client(monkeypatch, client)
    result = runner.invoke(main_module.app, [
        "ingest", "airbyte", "connections", "set-cursor",
        "-c", "c1", "--xmin", "0", "--sync", "--yes",
    ])
    assert result.exit_code == 0
    assert not any(u.endswith("/api/public/v1/jobs") and p.get("jobType") == "sync"
                   for u, p in client.posts)


def test_set_cursor_backup_writes_prechange_state(monkeypatch, tmp_path) -> None:
    _disable_envrc(monkeypatch)
    original = json.loads(json.dumps(_XMIN_STATE))
    client = _StateFakeClient(state=json.loads(json.dumps(original)))
    _patch_state_client(monkeypatch, client)
    result = runner.invoke(main_module.app, [
        "ingest", "airbyte", "connections", "set-cursor",
        "-c", "c1", "--xmin-factor", "0.1",
        "--backup", "--backup-dir", str(tmp_path), "--yes",
    ])
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
    result = runner.invoke(main_module.app, [
        "ingest", "airbyte", "connections", "set-cursor",
        "-c", "c1", "--xmin-factor", "0.1",
        "--backup", "--backup-dir", str(tmp_path), "--dry-run",
    ])
    assert result.exit_code == 0
    assert not (tmp_path / "c1.json").exists()


def test_refresh_single_triggers_refresh_job(monkeypatch) -> None:
    _disable_envrc(monkeypatch)
    client = _StateFakeClient(state=dict(_STREAM_STATE))
    _patch_state_client(monkeypatch, client)
    result = runner.invoke(main_module.app, [
        "ingest", "airbyte", "connections", "refresh", "-c", "c1",
    ])
    assert result.exit_code == 0
    job_posts = [p for u, p in client.posts if u.endswith("/api/public/v1/jobs")]
    assert job_posts == [{"connectionId": "c1", "jobType": "refresh"}]


def test_refresh_dry_run_triggers_nothing(monkeypatch) -> None:
    _disable_envrc(monkeypatch)
    client = _StateFakeClient(state=dict(_STREAM_STATE))
    _patch_state_client(monkeypatch, client)
    result = runner.invoke(main_module.app, [
        "ingest", "airbyte", "connections", "refresh",
        "--source-id", "s1", "--dry-run",
    ])
    assert result.exit_code == 0
    assert not any(u.endswith("/api/public/v1/jobs") and p.get("jobType") == "refresh"
                   for u, p in client.posts)
    assert "c1" in result.stdout  # listed as a target


def test_refresh_single_dry_run_triggers_nothing(monkeypatch) -> None:
    _disable_envrc(monkeypatch)
    client = _StateFakeClient(state=dict(_STREAM_STATE))
    _patch_state_client(monkeypatch, client)
    result = runner.invoke(main_module.app, [
        "ingest", "airbyte", "connections", "refresh", "-c", "c1", "--dry-run",
    ])
    assert result.exit_code == 0
    assert not any(u.endswith("/api/public/v1/jobs") and p.get("jobType") == "refresh"
                   for u, p in client.posts)


def test_refresh_bulk_triggers_per_connection(monkeypatch) -> None:
    _disable_envrc(monkeypatch)
    client = _StateFakeClient(state=dict(_STREAM_STATE))
    _patch_state_client(monkeypatch, client)
    result = runner.invoke(main_module.app, [
        "ingest", "airbyte", "connections", "refresh",
        "--source-id", "s1", "--yes", "--sleep", "0",
    ])
    assert result.exit_code == 0
    job_posts = [p for u, p in client.posts if u.endswith("/api/public/v1/jobs")]
    assert {"connectionId": "c1", "jobType": "refresh"} in job_posts
