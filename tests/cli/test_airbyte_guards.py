"""Guard-rail tests for bulk airbyte connection mutations."""

from __future__ import annotations

from types import SimpleNamespace

from typer.testing import CliRunner

import dataplat.cli.ingest.airbyte.connections as conn_cli

runner = CliRunner()

_CONNECTIONS = [
    {
        "connectionId": "c1",
        "name": "conn-one",
        "status": "active",
        "sourceId": "s1",
        "destinationId": "d1",
    },
    {
        "connectionId": "c2",
        "name": "conn-two",
        "status": "inactive",
        "sourceId": "s1",
        "destinationId": "d1",
    },
]


def _patch(monkeypatch, triggered: list[str]) -> None:
    monkeypatch.setattr(
        conn_cli,
        "build_authenticated_client",
        lambda: (SimpleNamespace(close=lambda: None), "http://airbyte"),
    )
    monkeypatch.setattr(
        conn_cli, "list_connections", lambda client, base_url: list(_CONNECTIONS)
    )
    monkeypatch.setattr(
        conn_cli,
        "trigger_sync_job",
        lambda client, base_url, conn_id: triggered.append(conn_id) or {"id": "j1"},
    )


def test_bulk_sync_refuses_without_yes(monkeypatch) -> None:
    triggered: list[str] = []
    _patch(monkeypatch, triggered)

    result = runner.invoke(conn_cli.app, ["sync"])

    assert result.exit_code == 1
    assert "--yes" in result.stdout
    assert triggered == []


def test_bulk_sync_with_yes_triggers_only_active(monkeypatch) -> None:
    triggered: list[str] = []
    _patch(monkeypatch, triggered)

    result = runner.invoke(conn_cli.app, ["sync", "--yes", "--sleep", "0"])

    assert result.exit_code == 0, result.stdout
    assert triggered == ["c1"]


def test_bulk_sync_dry_run_lists_without_triggering(monkeypatch) -> None:
    triggered: list[str] = []
    _patch(monkeypatch, triggered)

    result = runner.invoke(conn_cli.app, ["sync", "--dry-run"])

    assert result.exit_code == 0, result.stdout
    assert "conn-one" in result.stdout
    assert triggered == []


def test_single_sync_needs_no_confirmation(monkeypatch) -> None:
    triggered: list[str] = []
    _patch(monkeypatch, triggered)

    result = runner.invoke(conn_cli.app, ["sync", "-c", "c1"])

    assert result.exit_code == 0, result.stdout
    assert triggered == ["c1"]


def test_update_single_skip_is_clean_exit(monkeypatch) -> None:
    """Regression: a filtered-out single connection used to exit 1 with
    'Error updating connection: 0' because typer.Exit was swallowed."""
    triggered: list[str] = []
    _patch(monkeypatch, triggered)
    monkeypatch.setattr(
        conn_cli,
        "get_connection",
        lambda client, base_url, cid: {
            "connectionId": "c1",
            "name": "conn-one",
            "sourceId": "s1",
        },
    )

    result = runner.invoke(
        conn_cli.app,
        ["update", "-c", "c1", "--source-id", "OTHER", "--name", "x"],
    )

    assert result.exit_code == 0, result.stdout
    assert "Skipping" in result.stdout
    assert "Error updating connection" not in result.stdout


def test_bulk_update_refuses_without_yes(monkeypatch) -> None:
    triggered: list[str] = []
    _patch(monkeypatch, triggered)
    patched: list[str] = []
    monkeypatch.setattr(
        conn_cli,
        "patch_connection",
        lambda client, base_url, cid, updates: patched.append(cid),
    )

    result = runner.invoke(conn_cli.app, ["update", "--name", "renamed"])

    assert result.exit_code == 1
    assert patched == []
