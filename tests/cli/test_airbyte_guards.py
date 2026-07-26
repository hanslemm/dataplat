"""Guard-rail tests for every destructive gate in the airbyte area.

CliRunner's stdin is never a TTY, so an un-confirmed destructive command must
exit 1 while naming the flag that would have worked — never block, never guess.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from typer.main import get_group
from typer.testing import CliRunner

import dataplat.cli.ingest.airbyte._common as common_cli
import dataplat.cli.ingest.airbyte.connections as conn_cli
import dataplat.cli.ingest.airbyte.jobs as jobs_cli
import dataplat.cli.ingest.airbyte.sources as sources_cli
import dataplat.services.airbyte.jobs as jobs_svc

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


def _patch_common_client(monkeypatch, built: list[bool] | None = None) -> None:
    """Commands going through airbyte_client() resolve the factory in _common."""

    def factory():
        if built is not None:
            built.append(True)
        return SimpleNamespace(close=lambda: None), "http://airbyte"

    monkeypatch.setattr(common_cli, "build_authenticated_client", factory)


def test_connections_delete_refuses_without_yes(monkeypatch) -> None:
    triggered: list[str] = []
    _patch(monkeypatch, triggered)
    deleted: list[str] = []
    monkeypatch.setattr(
        conn_cli,
        "delete_connection",
        lambda client, base_url, cid: deleted.append(cid),
    )

    result = runner.invoke(conn_cli.app, ["delete", "-c", "c1"])

    assert result.exit_code == 1
    assert "--yes" in result.stdout
    assert deleted == []


def test_connections_delete_with_yes_deletes(monkeypatch) -> None:
    triggered: list[str] = []
    _patch(monkeypatch, triggered)
    deleted: list[str] = []
    monkeypatch.setattr(
        conn_cli,
        "delete_connection",
        lambda client, base_url, cid: deleted.append(cid),
    )

    result = runner.invoke(conn_cli.app, ["delete", "-c", "c1", "--yes"])

    assert result.exit_code == 0, result.stdout
    assert deleted == ["c1"]


def test_connections_reset_refuses_without_yes(monkeypatch) -> None:
    _patch_common_client(monkeypatch)
    started: list[tuple[str, str]] = []
    monkeypatch.setattr(
        jobs_svc,
        "trigger_job",
        lambda client, base_url, cid, job_type: started.append((cid, job_type)) or {},
    )

    result = runner.invoke(conn_cli.app, ["reset", "-c", "c1"])

    assert result.exit_code == 1
    assert "--yes" in result.stdout
    assert started == []


def test_connections_reset_with_yes_triggers_job(monkeypatch) -> None:
    _patch_common_client(monkeypatch)
    started: list[tuple[str, str]] = []
    monkeypatch.setattr(
        jobs_svc,
        "trigger_job",
        lambda client, base_url, cid, job_type: (
            started.append((cid, job_type)) or {"jobId": 7}
        ),
    )

    result = runner.invoke(conn_cli.app, ["reset", "-c", "c1", "--yes"])

    assert result.exit_code == 0, result.stdout
    assert started == [("c1", "reset")]


def test_bulk_refresh_refuses_without_yes(monkeypatch) -> None:
    _patch_common_client(monkeypatch)
    monkeypatch.setattr(
        conn_cli, "list_connections", lambda client, base_url: list(_CONNECTIONS)
    )
    started: list[tuple[str, str]] = []
    monkeypatch.setattr(
        jobs_svc,
        "trigger_job",
        lambda client, base_url, cid, job_type: started.append((cid, job_type)) or {},
    )

    result = runner.invoke(conn_cli.app, ["refresh"])

    assert result.exit_code == 1
    assert "--yes" in result.stdout
    assert started == []


def test_bulk_set_cursor_refuses_without_yes(monkeypatch) -> None:
    _patch_common_client(monkeypatch)
    monkeypatch.setattr(
        conn_cli, "list_connections", lambda client, base_url: list(_CONNECTIONS)
    )
    written: list[dict] = []
    monkeypatch.setattr(
        conn_cli,
        "update_connection_state",
        lambda client, base_url, cid, state: written.append(state),
    )

    result = runner.invoke(conn_cli.app, ["set-cursor", "--to", "2024-01-01"])

    assert result.exit_code == 1
    assert "--yes" in result.stdout
    assert written == []


def test_jobs_cancel_refuses_without_yes(monkeypatch) -> None:
    _patch_common_client(monkeypatch)
    cancelled: list[str] = []
    monkeypatch.setattr(
        jobs_cli, "cancel_job", lambda client, base_url, jid: cancelled.append(jid)
    )

    result = runner.invoke(jobs_cli.app, ["cancel", "91"])

    assert result.exit_code == 1
    assert "--yes" in result.stdout
    assert cancelled == []


def test_resource_delete_refuses_before_building_a_client(monkeypatch) -> None:
    """The gate runs first, so an unconfirmed delete never even authenticates."""
    built: list[bool] = []
    _patch_common_client(monkeypatch, built)

    result = runner.invoke(sources_cli.app, ["delete", "--source-id", "s1"])

    assert result.exit_code == 1
    assert "--yes" in result.stdout
    assert built == []


@pytest.mark.parametrize(
    ("app", "command"),
    [
        (conn_cli.app, "delete"),
        (conn_cli.app, "reset"),
        (conn_cli.app, "sync"),
        (conn_cli.app, "update"),
        (conn_cli.app, "refresh"),
        (conn_cli.app, "set-cursor"),
        (jobs_cli.app, "cancel"),
        (sources_cli.app, "delete"),
    ],
)
def test_destructive_commands_offer_both_yes_spellings(app, command) -> None:
    """Sharing one option object must not drop any site's ``-y`` spelling.

    Asserted against the click parameter that generates the help, because
    ``"-y" in result.stdout`` can never fail: ``-y`` is a substring of
    ``--yes``. Exact equality also catches a third spelling creeping in.
    """
    params = get_group(app).commands[command].params
    yes_params = [param for param in params if param.name == "yes"]

    assert len(yes_params) == 1, f"{command} has no single --yes parameter"
    assert set(yes_params[0].opts) == {"--yes", "-y"}

    # The declaration must still render, too: a malformed option fails here.
    result = runner.invoke(app, [command, "--help"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.stdout
    assert "--yes" in result.stdout
