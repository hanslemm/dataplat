"""``dp status`` — stream discipline, per-section degradation, markup safety."""

from __future__ import annotations

import json
import sys
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

import dataplat.main as main_module
from dataplat.cli import status as status_cli

runner = CliRunner()

# Values a warehouse, a driver, docker or an API could hand us. "[/x]" used to
# raise MarkupError mid-render; "[bold]" used to be swallowed silently.
HOSTILE = "relation [/x] missing [bold]"


def _disable_envrc(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main_module, "load_envrc", lambda: None)


def _patch_sections(monkeypatch: pytest.MonkeyPatch, *, aws: bool = True) -> None:
    monkeypatch.setattr(
        status_cli,
        "_db_section",
        lambda: {
            "demo_pg": {"reachable": True, "long_running": 0},
            "demo_rs": {"reachable": False, "error": "connection refused"},
        },
    )
    monkeypatch.setattr(
        status_cli,
        "_airbyte_section",
        lambda: {
            "available": True,
            "jobs_last_24h": 12,
            "failed": [{"jobId": 9, "connectionId": "c1"}],
            "running": 2,
        },
    )
    monkeypatch.setattr(
        status_cli,
        "_runners_section",
        lambda: {
            "available": True,
            "runners": [
                {"name": "gha-runner-x", "status": "Up 2 hours", "running": True}
            ],
        },
    )
    if aws:
        monkeypatch.setattr(
            status_cli,
            "_aws_section",
            lambda: {
                "available": True,
                "instance": "prod-db-1",
                "metrics": {"CPUUtilization": 12.5, "FreeStorageSpace": 200 * 1024**3},
            },
        )


def _configure_target(
    monkeypatch: pytest.MonkeyPatch, engine: str = "postgresql"
) -> None:
    """One fully specified target, so resolve() succeeds and connect is reached."""
    monkeypatch.setenv("DP_TARGETS", "demo")
    monkeypatch.delenv("DP_DEFAULT_TARGET", raising=False)
    monkeypatch.setenv("DEMO_ENGINE", engine)
    monkeypatch.setenv("DEMO_HOST", "db.example.invalid")
    monkeypatch.setenv("DEMO_USER", "svc")
    monkeypatch.setenv("DEMO_DATABASE", "analytics")


class _FakeCursor:
    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class _FakeConn:
    def __enter__(self) -> _FakeConn:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def cursor(self) -> _FakeCursor:
        return _FakeCursor()


class _FakeCloudWatch:
    def get_metric_data(self, **kwargs: Any) -> dict[str, list[dict]]:
        return {"MetricDataResults": []}


class _FakeSession:
    def client(self, name: str) -> _FakeCloudWatch:
        return _FakeCloudWatch()


# rendering


def test_status_renders_all_sections(monkeypatch: pytest.MonkeyPatch) -> None:
    _disable_envrc(monkeypatch)
    _patch_sections(monkeypatch)

    result = runner.invoke(main_module.app, ["status"])

    assert result.exit_code == 0, result.output
    assert "Databases" in result.output
    assert "demo_pg" in result.output
    assert "connection refused" in result.output  # section degrades, not dies
    assert "Airbyte" in result.output
    assert "1 failed" in result.output
    assert "gha-runner-x" in result.output
    assert "CPU 12.5%" in result.output


def test_status_renders_quiet_overview(monkeypatch: pytest.MonkeyPatch) -> None:
    """The all-green branches: no failed jobs, no containers, no datapoints."""
    _disable_envrc(monkeypatch)
    monkeypatch.setattr(
        status_cli,
        "_db_section",
        lambda: {"demo": {"reachable": True, "long_running": 0}},
    )
    monkeypatch.setattr(
        status_cli,
        "_airbyte_section",
        lambda: {"available": True, "jobs_last_24h": 0, "failed": [], "running": 0},
    )
    monkeypatch.setattr(
        status_cli, "_runners_section", lambda: {"available": True, "runners": []}
    )
    monkeypatch.setattr(
        status_cli,
        "_aws_section",
        lambda: {"available": True, "instance": "prod-db-1", "metrics": {}},
    )

    result = runner.invoke(main_module.app, ["status"])

    assert result.exit_code == 0, result.output
    assert "no long-running queries" in result.output
    assert "0 job(s) in 24h" in result.output
    assert "no runner containers" in result.output
    assert "no datapoints" in result.output


def test_status_no_aws_skips_section(monkeypatch: pytest.MonkeyPatch) -> None:
    _disable_envrc(monkeypatch)
    _patch_sections(monkeypatch)

    result = runner.invoke(main_module.app, ["status", "--no-aws"])

    assert result.exit_code == 0, result.output
    assert "AWS (RDS)" not in result.output


def test_status_no_aws_omits_key_from_json(monkeypatch: pytest.MonkeyPatch) -> None:
    _disable_envrc(monkeypatch)
    # _aws_section stays unpatched: --no-aws must not even call it.
    _patch_sections(monkeypatch, aws=False)

    def fail() -> dict[str, Any]:
        raise AssertionError("--no-aws still probed AWS")

    monkeypatch.setattr(status_cli, "_aws_section", fail)

    result = runner.invoke(main_module.app, ["status", "--no-aws", "--json"])

    assert result.exit_code == 0, result.output
    assert "aws" not in json.loads(result.stdout)


def test_status_json(monkeypatch: pytest.MonkeyPatch) -> None:
    _disable_envrc(monkeypatch)
    _patch_sections(monkeypatch)

    result = runner.invoke(main_module.app, ["status", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["databases"]["demo_pg"]["reachable"] is True
    assert payload["airbyte"]["failed"][0]["jobId"] == 9
    assert payload["runners"]["runners"][0]["name"] == "gha-runner-x"
    assert payload["aws"]["available"] is True


def test_status_json_keeps_stdout_pure_when_sso_notice_fires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The notice used to be printed to stdout, in the middle of the payload."""
    import dataplat.services.aws.auth as aws_auth

    _disable_envrc(monkeypatch)
    _patch_sections(monkeypatch, aws=False)

    def fake_get_session(
        *,
        profile: str,
        region: str | None = None,
        notify: Callable[[str], None] | None = None,
    ) -> _FakeSession:
        if notify:
            notify(
                "SSO session expired or missing; running aws sso login "
                f"for profile {profile}"
            )
        return _FakeSession()

    monkeypatch.setattr(aws_auth, "get_session", fake_get_session)
    monkeypatch.setenv("DP_RDS_INSTANCE", "prod-db-1")

    result = runner.invoke(main_module.app, ["status", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)  # raised JSONDecodeError before the fix
    assert payload["aws"]["available"] is True
    assert "aws sso login" in result.stderr
    assert "aws sso login" not in result.stdout


def test_sso_notice_is_escaped(monkeypatch: pytest.MonkeyPatch) -> None:
    """The notice quotes a user-supplied profile name, so it is markup-unsafe."""
    import dataplat.services.aws.auth as aws_auth

    def fake_get_session(
        *,
        profile: str,
        region: str | None = None,
        notify: Callable[[str], None] | None = None,
    ) -> _FakeSession:
        if notify:
            notify(f"running aws sso login for profile {profile}")
        return _FakeSession()

    monkeypatch.setattr(aws_auth, "get_session", fake_get_session)
    monkeypatch.setenv("DP_RDS_INSTANCE", "prod-db-1")
    monkeypatch.setenv("DP_AWS_PROFILE", "sso[/x]")

    with status_cli.err_console.capture() as capture:
        section = status_cli._aws_section()

    assert section["available"] is True
    assert "sso[/x]" in capture.get()


# concurrency, ordering and independence

# Long enough that a loaded machine still gets three threads to the barrier,
# short enough that a serial implementation fails in seconds instead of hanging.
_GATE_TIMEOUT_S = 10


def test_sections_run_concurrently(monkeypatch: pytest.MonkeyPatch) -> None:
    """A barrier, not a stopwatch: three sections must be in flight at once.

    Run serially, every ``wait()`` here times out and the sections come back as
    errors, so this fails on a serial implementation instead of merely being slow.
    """
    _disable_envrc(monkeypatch)
    gate = threading.Barrier(3, timeout=_GATE_TIMEOUT_S)

    def gated(payload: dict[str, Any]) -> Callable[[], dict[str, Any]]:
        def probe() -> dict[str, Any]:
            gate.wait()
            return payload

        return probe

    monkeypatch.setattr(
        status_cli,
        "_db_section",
        gated({"demo": {"reachable": True, "long_running": 0}}),
    )
    monkeypatch.setattr(
        status_cli,
        "_airbyte_section",
        gated({"available": True, "jobs_last_24h": 0, "failed": [], "running": 0}),
    )
    monkeypatch.setattr(
        status_cli, "_runners_section", gated({"available": True, "runners": []})
    )

    result = runner.invoke(main_module.app, ["status", "--no-aws", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["databases"]["demo"]["reachable"] is True
    assert payload["airbyte"]["available"] is True
    assert payload["runners"]["available"] is True


def test_aws_section_stays_serial_after_the_others(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`aws sso login` can take the terminal, so AWS must not overlap anything.

    It may prompt for a device code and open a browser; sharing stdin with a Live
    spinner or with another probe that prompts is the failure this pins.
    """
    _disable_envrc(monkeypatch)
    gate = threading.Barrier(3, timeout=_GATE_TIMEOUT_S)
    lock = threading.Lock()
    events: list[str] = []

    def traced(
        name: str, payload: dict[str, Any], *, concurrent: bool
    ) -> Callable[[], dict[str, Any]]:
        def probe() -> dict[str, Any]:
            with lock:
                events.append(f"{name}:start")
            if concurrent:
                gate.wait()
            with lock:
                events.append(f"{name}:end")
            return payload

        return probe

    monkeypatch.setattr(
        status_cli,
        "_db_section",
        traced("db", {"demo": {"reachable": True, "long_running": 0}}, concurrent=True),
    )
    monkeypatch.setattr(
        status_cli,
        "_airbyte_section",
        traced(
            "airbyte",
            {"available": True, "jobs_last_24h": 0, "failed": [], "running": 0},
            concurrent=True,
        ),
    )
    monkeypatch.setattr(
        status_cli,
        "_runners_section",
        traced("runners", {"available": True, "runners": []}, concurrent=True),
    )
    monkeypatch.setattr(
        status_cli,
        "_aws_section",
        traced(
            "aws",
            {"available": True, "instance": "prod-db-1", "metrics": {}},
            concurrent=False,
        ),
    )

    result = runner.invoke(main_module.app, ["status", "--json"])

    assert result.exit_code == 0, result.output
    # The three concurrent probes all start before any of them finishes...
    assert set(events[:3]) == {"db:start", "airbyte:start", "runners:start"}
    # ...and AWS only begins once every one of them has returned.
    assert events.index("aws:start") == 6
    assert events[-1] == "aws:end"


def test_payload_order_ignores_which_section_finished_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Key order and section order are contracts, not a record of the race."""
    _disable_envrc(monkeypatch)
    runners_done = threading.Event()

    def slow_db() -> dict[str, Any]:
        # Cannot return until the *last* section already has.
        assert runners_done.wait(_GATE_TIMEOUT_S)
        return {"demo": {"reachable": True, "long_running": 0}}

    def fast_runners() -> dict[str, Any]:
        runners_done.set()
        return {"available": True, "runners": []}

    monkeypatch.setattr(status_cli, "_db_section", slow_db)
    monkeypatch.setattr(
        status_cli,
        "_airbyte_section",
        lambda: {"available": True, "jobs_last_24h": 0, "failed": [], "running": 0},
    )
    monkeypatch.setattr(status_cli, "_runners_section", fast_runners)

    as_json = runner.invoke(main_module.app, ["status", "--no-aws", "--json"])
    runners_done.clear()
    human = runner.invoke(main_module.app, ["status", "--no-aws"])

    assert as_json.exit_code == 0, as_json.output
    assert list(json.loads(as_json.stdout)) == ["databases", "airbyte", "runners"]
    assert human.exit_code == 0, human.output
    positions = [human.output.index(h) for h in ("Databases", "Airbyte", "GitHub")]
    assert positions == sorted(positions)


def test_raising_section_degrades_instead_of_crashing_the_overview(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed DP_TARGETS used to end the run in a traceback, before any
    section rendered — and with --json emitting no document at all."""
    from dataplat.core.errors import ConfigError

    _disable_envrc(monkeypatch)
    _patch_sections(monkeypatch)

    def boom() -> dict[str, Any]:
        raise ConfigError("'all' is a reserved target name.")

    monkeypatch.setattr(status_cli, "_db_section", boom)

    result = runner.invoke(main_module.app, ["status", "--json"])

    assert result.exception is None, result.exception
    # status reports; it does not exit non-zero for a section it could not reach.
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["databases"] == {
        "available": False,
        # The class name stays: an unexpected exception's str is often empty.
        "error": "ConfigError: 'all' is a reserved target name.",
    }
    # Every other section is untouched.
    assert payload["airbyte"]["jobs_last_24h"] == 12
    assert payload["runners"]["available"] is True
    assert payload["aws"]["available"] is True


def test_section_level_database_error_renders_escaped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The new whole-section branch of _print_db renders driver text verbatim."""
    _disable_envrc(monkeypatch)
    _patch_sections(monkeypatch)

    def boom() -> dict[str, Any]:
        raise RuntimeError(HOSTILE)

    monkeypatch.setattr(status_cli, "_db_section", boom)

    result = runner.invoke(main_module.app, ["status", "--no-aws"])

    assert result.exception is None, result.exception
    assert result.exit_code == 0, result.output
    assert f"RuntimeError: {HOSTILE}" in result.output


def test_raising_aws_section_degrades_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """AWS runs outside the pool, so it needs the same guard, not a bare call."""
    _disable_envrc(monkeypatch)
    _patch_sections(monkeypatch, aws=False)

    def boom() -> dict[str, Any]:
        raise RuntimeError("botocore blew up in a way nobody catalogued")

    monkeypatch.setattr(status_cli, "_aws_section", boom)

    result = runner.invoke(main_module.app, ["status", "--json"])

    assert result.exception is None, result.exception
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["aws"] == {
        "available": False,
        "error": "RuntimeError: botocore blew up in a way nobody catalogued",
    }


# markup safety


def test_overview_survives_hostile_psycopg_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`dp status` crashed with MarkupError on any error text holding [/x]."""
    _disable_envrc(monkeypatch)
    _patch_sections(monkeypatch)
    monkeypatch.setattr(
        status_cli,
        "_db_section",
        lambda: {"demo": {"reachable": False, "error": HOSTILE}},
    )

    result = runner.invoke(main_module.app, ["status"])

    assert result.exception is None, result.exception
    assert result.exit_code == 0, result.output
    assert HOSTILE in result.output


def test_overview_renders_hostile_values_verbatim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every external value in a healthy overview: names, ids, docker strings."""
    _disable_envrc(monkeypatch)
    monkeypatch.setattr(
        status_cli,
        "_db_section",
        lambda: {"tgt[/x]": {"reachable": True, "long_running": 3}},
    )
    monkeypatch.setattr(
        status_cli,
        "_airbyte_section",
        lambda: {
            "available": True,
            "jobs_last_24h": 1,
            "failed": [{"jobId": "j[/x]", "connectionId": "c[bold]"}],
            "running": 0,
        },
    )
    monkeypatch.setattr(
        status_cli,
        "_runners_section",
        lambda: {
            "available": True,
            "runners": [{"name": "gha[/x]", "status": "Up [bold]", "running": True}],
        },
    )
    monkeypatch.setattr(
        status_cli,
        "_aws_section",
        lambda: {"available": True, "instance": "db[/x]", "metrics": {}},
    )

    result = runner.invoke(main_module.app, ["status"])

    assert result.exception is None, result.exception
    assert result.exit_code == 0, result.output
    for value in ("tgt[/x]", "j[/x]", "c[bold]", "gha[/x]", "Up [bold]", "db[/x]"):
        assert value in result.output


def test_unavailable_sections_render_hostile_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The degraded branches print driver/API/docker text too."""
    _disable_envrc(monkeypatch)
    monkeypatch.setattr(status_cli, "_db_section", lambda: {})
    monkeypatch.setattr(
        status_cli,
        "_airbyte_section",
        lambda: {"available": False, "error": "api [/x] down"},
    )
    monkeypatch.setattr(
        status_cli,
        "_runners_section",
        lambda: {"available": False, "error": "docker [/x] gone"},
    )
    monkeypatch.setattr(
        status_cli,
        "_aws_section",
        lambda: {"available": False, "error": "sso [bold] expired"},
    )

    result = runner.invoke(main_module.app, ["status"])

    assert result.exception is None, result.exception
    assert result.exit_code == 0, result.output
    assert "no targets configured" in result.output
    for value in ("api [/x] down", "docker [/x] gone", "sso [bold] expired"):
        assert value in result.output


def test_json_payload_keeps_hostile_values_raw(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Escaping happens at render time; the machine-readable path is untouched."""
    _disable_envrc(monkeypatch)
    _patch_sections(monkeypatch)
    monkeypatch.setattr(
        status_cli,
        "_db_section",
        lambda: {"demo": {"reachable": False, "error": HOSTILE}},
    )

    result = runner.invoke(main_module.app, ["status", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["databases"]["demo"]["error"] == HOSTILE


# databases section


def test_db_section_without_targets_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DP_TARGETS", raising=False)

    assert status_cli._db_section() == {}


def test_db_section_reports_missing_psycopg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DP_TARGETS", "demo,other")
    monkeypatch.setenv("DEMO_ENGINE", "postgresql")
    monkeypatch.setenv("OTHER_ENGINE", "postgresql")
    # None in sys.modules makes `import psycopg` raise ImportError, which is
    # exactly what an install without the db extra looks like.
    monkeypatch.setitem(sys.modules, "psycopg", None)

    section = status_cli._db_section()

    assert set(section) == {"demo", "other"}
    assert section["demo"]["reachable"] is False
    assert "psycopg not installed" in section["demo"]["error"]


def test_db_section_reports_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DP_TARGETS", "demo")
    monkeypatch.delenv("DP_DEFAULT_TARGET", raising=False)
    for var in (
        "DEMO_HOST",
        "DEMO_USER",
        "DEMO_DATABASE",
        "PGHOST",
        "PGUSER",
        "PGDATABASE",
        "DB_HOST",
        "DB_USER",
        "DB_NAME",
    ):
        monkeypatch.delenv(var, raising=False)

    section = status_cli._db_section()

    assert section["demo"]["reachable"] is False
    assert "Missing required connection settings" in section["demo"]["error"]


def test_db_section_reports_unreachable_target(monkeypatch: pytest.MonkeyPatch) -> None:
    import psycopg

    _configure_target(monkeypatch)

    def fake_connect(**kwargs: Any) -> _FakeConn:
        raise psycopg.OperationalError("connection to server failed")

    monkeypatch.setattr(psycopg, "connect", fake_connect)

    section = status_cli._db_section()

    assert section["demo"]["reachable"] is False
    assert "connection to server failed" in section["demo"]["error"]


def test_db_section_truncates_long_driver_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import psycopg

    _configure_target(monkeypatch)

    def fake_connect(**kwargs: Any) -> _FakeConn:
        raise psycopg.OperationalError("x" * 500)

    monkeypatch.setattr(psycopg, "connect", fake_connect)

    assert len(status_cli._db_section()["demo"]["error"]) == 160


def test_db_section_counts_long_running_queries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import psycopg

    from dataplat.services.db import long_queries

    _configure_target(monkeypatch)
    seen: list[dict[str, Any]] = []

    def fake_connect(**kwargs: Any) -> _FakeConn:
        seen.append(kwargs)
        return _FakeConn()

    monkeypatch.setattr(psycopg, "connect", fake_connect)
    monkeypatch.setattr(
        long_queries, "fetch_long_queries_postgres", lambda *a, **k: [1, 2]
    )

    section = status_cli._db_section()

    assert section == {"demo": {"reachable": True, "long_running": 2}}
    # A hung target must not hang the dashboard.
    assert seen[0]["connect_timeout"] == 10


def test_db_section_probes_targets_concurrently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each target carries its own 10s connect timeout; serially those add up.

    The barrier makes it structural rather than timed: a connect that cannot
    return until the other target's connect has also started can only succeed if
    the probes overlap.
    """
    import psycopg

    from dataplat.services.db import long_queries

    for prefix in ("DEMO_PG", "DEMO_RS"):
        monkeypatch.setenv(f"{prefix}_HOST", "db.example.invalid")
        monkeypatch.setenv(f"{prefix}_USER", "svc")
        monkeypatch.setenv(f"{prefix}_DATABASE", "analytics")
    gate = threading.Barrier(2, timeout=_GATE_TIMEOUT_S)

    def fake_connect(**kwargs: Any) -> _FakeConn:
        gate.wait()
        return _FakeConn()

    monkeypatch.setattr(psycopg, "connect", fake_connect)
    monkeypatch.setattr(long_queries, "fetch_long_queries_postgres", lambda *a, **k: [])
    monkeypatch.setattr(long_queries, "fetch_long_queries", lambda *a, **k: [])

    section = status_cli._db_section()

    # Both reachable proves the barrier opened; the key order is DP_TARGETS order,
    # not the order the connects happened to finish in.
    assert list(section) == ["demo_pg", "demo_rs"]
    assert all(info["reachable"] for info in section.values())


def test_db_section_uses_redshift_query_for_redshift_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import psycopg

    from dataplat.services.db import long_queries

    _configure_target(monkeypatch, engine="redshift")
    calls: list[dict[str, Any]] = []

    def fake_fetch(cursor: Any, **kwargs: Any) -> list[tuple]:
        calls.append(kwargs)
        return []

    monkeypatch.setattr(psycopg, "connect", lambda **kwargs: _FakeConn())
    monkeypatch.setattr(long_queries, "fetch_long_queries", fake_fetch)

    section = status_cli._db_section()

    assert section == {"demo": {"reachable": True, "long_running": 0}}
    assert calls[0]["running_only"] is True


# airbyte section


def test_airbyte_section_reports_missing_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # httpx/croniter missing shows up as an ImportError on the client module,
    # which is already in sys.modules here — so stub the module itself.
    monkeypatch.setitem(sys.modules, "dataplat.services.airbyte.client", None)

    section = status_cli._airbyte_section()

    assert section["available"] is False
    assert "ingest dependencies not installed" in section["error"]


def test_airbyte_section_reports_missing_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AIRBYTE_BASE_URL", raising=False)

    section = status_cli._airbyte_section()

    assert section == {"available": False, "error": "Set AIRBYTE_BASE_URL"}


def test_airbyte_section_reports_service_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from dataplat.core.errors import ServiceError
    from dataplat.services.airbyte import client as airbyte_client
    from dataplat.services.airbyte import jobs as airbyte_jobs

    closed: list[bool] = []

    class _FakeClient:
        def close(self) -> None:
            closed.append(True)

    monkeypatch.setattr(
        airbyte_client,
        "build_authenticated_client",
        lambda: (_FakeClient(), "https://airbyte.example"),
    )

    def boom(*a: Any, **k: Any) -> list[dict]:
        raise ServiceError("list jobs failed: 502 Bad Gateway")

    monkeypatch.setattr(airbyte_jobs, "list_jobs", boom)

    section = status_cli._airbyte_section()

    assert section["available"] is False
    assert "502 Bad Gateway" in section["error"]
    assert closed == [True]  # the client closes even when the call fails


def test_airbyte_section_counts_only_recent_jobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataplat.services.airbyte import client as airbyte_client
    from dataplat.services.airbyte import jobs as airbyte_jobs

    now = datetime.now(UTC)
    jobs = [
        {
            "jobId": 1,
            "connectionId": "c1",
            "status": "FAILED",
            "startTime": now.isoformat().replace("+00:00", "Z"),
        },
        {"jobId": 2, "status": "running", "createdAt": now.isoformat()},
        {
            "jobId": 3,
            "status": "succeeded",
            "startTime": (now - timedelta(days=2)).isoformat(),
        },
        {"jobId": 4, "status": "succeeded"},  # no timestamp at all
        {"jobId": 5, "status": "succeeded", "startTime": "not-a-timestamp"},
    ]

    monkeypatch.setattr(
        airbyte_client,
        "build_authenticated_client",
        lambda: (SimpleNamespace(close=lambda: None), "https://airbyte.example"),
    )
    monkeypatch.setattr(airbyte_jobs, "list_jobs", lambda *a, **k: jobs)

    section = status_cli._airbyte_section()

    assert section["jobs_last_24h"] == 2
    assert section["failed"] == [{"jobId": 1, "connectionId": "c1"}]
    assert section["running"] == 1


# runners section


def test_runners_section_reports_missing_docker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(status_cli.shutil, "which", lambda name: None)

    assert status_cli._runners_section() == {
        "available": False,
        "error": "docker not found on PATH",
    }


def test_runners_section_reports_unreachable_daemon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(status_cli.shutil, "which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(
        status_cli.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(
            returncode=1, stdout="", stderr="Cannot connect to the Docker daemon\n"
        ),
    )

    assert status_cli._runners_section() == {
        "available": False,
        "error": "Cannot connect to the Docker daemon",
    }


def test_runners_section_falls_back_when_docker_is_silent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(status_cli.shutil, "which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(
        status_cli.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr=""),
    )

    assert status_cli._runners_section()["error"] == "docker daemon unreachable"


def test_runners_section_reports_oserror(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(status_cli.shutil, "which", lambda name: "/usr/bin/docker")

    def boom(*a: Any, **k: Any) -> None:
        raise OSError("Exec format error")

    monkeypatch.setattr(status_cli.subprocess, "run", boom)

    section = status_cli._runners_section()

    assert section["available"] is False
    assert "Exec format error" in section["error"]


def test_runners_section_parses_docker_output(monkeypatch: pytest.MonkeyPatch) -> None:
    stdout = (
        "gha-runner-1\tUp 2 hours\n"
        "gha-runner-2\tExited (0) 3 minutes ago\n"
        "malformed-line-without-tab\n"
    )
    monkeypatch.setattr(status_cli.shutil, "which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(
        status_cli.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout=stdout, stderr=""),
    )

    section = status_cli._runners_section()

    assert section["available"] is True
    assert [r["name"] for r in section["runners"]] == ["gha-runner-1", "gha-runner-2"]
    assert [r["running"] for r in section["runners"]] == [True, False]


# aws section


def test_aws_section_requires_instance_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DP_RDS_INSTANCE", raising=False)

    assert status_cli._aws_section() == {
        "available": False,
        "error": "DP_RDS_INSTANCE not set",
    }


def test_aws_section_reports_missing_boto(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DP_RDS_INSTANCE", "prod-db-1")
    monkeypatch.setitem(sys.modules, "botocore.exceptions", None)

    assert status_cli._aws_section() == {
        "available": False,
        "error": "boto3 not installed",
    }


def test_aws_section_uses_sso_login_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """The AWS section goes through get_session, which auto-runs `aws sso login`."""
    import dataplat.services.aws.auth as aws_auth

    calls: list[str] = []

    def fake_get_session(
        *,
        profile: str,
        region: str | None = None,
        notify: Callable[[str], None] | None = None,
    ) -> _FakeSession:
        calls.append(profile)
        return _FakeSession()

    monkeypatch.setattr(aws_auth, "get_session", fake_get_session)
    monkeypatch.setenv("DP_RDS_INSTANCE", "prod-db-1")
    monkeypatch.setenv("DP_AWS_PROFILE", "my-sso-profile")

    section = status_cli._aws_section()

    assert calls == ["my-sso-profile"]
    assert section["available"] is True


def test_aws_section_degrades_on_failed_login(monkeypatch: pytest.MonkeyPatch) -> None:
    import dataplat.services.aws.auth as aws_auth
    from dataplat.core.errors import AuthError

    def fake_get_session(
        *,
        profile: str,
        region: str | None = None,
        notify: Callable[[str], None] | None = None,
    ) -> _FakeSession:
        raise AuthError(f"SSO login failed for profile {profile}")

    monkeypatch.setattr(aws_auth, "get_session", fake_get_session)
    monkeypatch.setenv("DP_RDS_INSTANCE", "prod-db-1")

    section = status_cli._aws_section()

    assert section["available"] is False
    assert "SSO login failed" in section["error"]


def test_aws_section_degrades_on_botocore_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from botocore.exceptions import ClientError

    import dataplat.services.aws.auth as aws_auth
    from dataplat.cli.cloud.aws import rds

    def fake_get_session(
        *,
        profile: str,
        region: str | None = None,
        notify: Callable[[str], None] | None = None,
    ) -> _FakeSession:
        return _FakeSession()

    def boom(*a: Any, **k: Any) -> None:
        raise ClientError(
            {"Error": {"Code": "Throttling", "Message": "Rate exceeded"}},
            "GetMetricData",
        )

    monkeypatch.setattr(aws_auth, "get_session", fake_get_session)
    monkeypatch.setattr(rds, "_fetch_metric_summaries", boom)
    monkeypatch.setenv("DP_RDS_INSTANCE", "prod-db-1")

    section = status_cli._aws_section()

    assert section["available"] is False
    assert "Rate exceeded" in section["error"]


def test_aws_section_keeps_only_the_two_headline_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import dataplat.services.aws.auth as aws_auth
    from dataplat.cli.cloud.aws import rds

    summaries = [
        ("CPUUtilization", {"latest": 12.5}),
        ("FreeStorageSpace", {"latest": 200.0}),
        ("DatabaseConnections", {"latest": 7.0}),
        ("FreeableMemory", None),
    ]

    def fake_get_session(
        *,
        profile: str,
        region: str | None = None,
        notify: Callable[[str], None] | None = None,
    ) -> _FakeSession:
        return _FakeSession()

    monkeypatch.setattr(aws_auth, "get_session", fake_get_session)
    monkeypatch.setattr(rds, "_fetch_metric_summaries", lambda *a, **k: summaries)
    monkeypatch.setenv("DP_RDS_INSTANCE", "prod-db-1")

    section = status_cli._aws_section()

    assert section["metrics"] == {"CPUUtilization": 12.5, "FreeStorageSpace": 200.0}


# a DuckDB target
#
# Every test here uses a real DuckDB database. Opening the file *is* the health
# check, so a stub would be testing nothing: what is under test is precisely
# whether the file opens.


def _flat(text: str) -> str:
    """One long line: Rich wraps at the terminal width, assertions are wording."""
    return " ".join(text.split())


def _duckdb_target(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, create: bool = True
) -> Path:
    """Declare a DuckDB target, with a real database behind it by default."""
    import duckdb

    path = tmp_path / "warehouse.duckdb"
    if create:
        connection = duckdb.connect(str(path))
        connection.execute("CREATE TABLE orders(id INTEGER)")
        connection.close()
    monkeypatch.setenv("DP_TARGETS", "ddb")
    monkeypatch.setenv("DDB_ENGINE", "duckdb")
    monkeypatch.setenv("DDB_PATH", str(path))
    monkeypatch.delenv("DP_DEFAULT_TARGET", raising=False)
    return path


def test_db_section_reports_a_duckdb_target_as_reachable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Opening the file is the whole probe: there is no socket and no session."""
    _duckdb_target(monkeypatch, tmp_path)

    section = status_cli._db_section()

    assert section["ddb"]["reachable"] is True
    # None, not 0. A count of 0 is what a healthy *server* reports, so it would
    # read as "all clear" about a question this engine cannot be asked at all.
    assert section["ddb"]["long_running"] is None
    assert section["ddb"]["long_running"] != 0
    # And the reason travels with the gap, in the capability matrix's own words.
    note = section["ddb"]["long_running_note"]
    assert "DuckDB" in note
    assert "no other sessions to inspect or cancel" in note


def test_duckdb_note_comes_from_the_capability_matrix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """One fact, one wording: `dp db long-queries` refuses with the same reason.

    Two hand-written explanations of the same engine property is exactly how a
    dashboard ends up contradicting the command it points at.
    """
    from dataplat.services.db.capabilities import capabilities_for
    from dataplat.services.db.connection import SqlEngine

    _duckdb_target(monkeypatch, tmp_path)
    reason = capabilities_for(SqlEngine.duckdb).concurrent_sessions.reason

    section = status_cli._db_section()

    assert reason in section["ddb"]["long_running_note"]


def test_db_section_json_keys_keep_their_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`reachable` first, as for every other target; the note is an addition."""
    _duckdb_target(monkeypatch, tmp_path)

    info = status_cli._db_section()["ddb"]

    assert list(info) == ["reachable", "long_running", "long_running_note"]


def test_db_section_reports_a_missing_duckdb_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A mistyped path is unreachable, and the message names the path.

    dataplat does not create the file — an empty database would report an empty
    warehouse instead of a wrong path — so this is the common misconfiguration.
    """
    path = _duckdb_target(monkeypatch, tmp_path, create=False)

    section = status_cli._db_section()

    error = section["ddb"]["error"]
    assert section["ddb"]["reachable"] is False
    assert error.startswith("DuckDB database not found:")
    # Truncated to 160 like every error in this section, so a deep tmp_path
    # leaves only the head of the path — naming it at all is the point.
    assert str(path)[:40] in error
    assert len(error) <= 160
    assert not path.exists()  # the probe did not create it either


def test_db_section_reports_a_file_that_is_not_a_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The driver's own refusal, which is the other half of "reachable"."""
    path = _duckdb_target(monkeypatch, tmp_path, create=False)
    path.write_bytes(b"not a duckdb file")

    section = status_cli._db_section()

    assert section["ddb"]["reachable"] is False
    # duckdb.Error, caught and reported rather than allowed to escape into
    # _guarded and take the whole section down with it.
    assert section["ddb"]["error"].startswith("IO Error:")


def test_db_section_reports_a_duckdb_target_with_no_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _duckdb_target(monkeypatch, tmp_path)
    monkeypatch.delenv("DDB_PATH")
    monkeypatch.delenv("DDB_DATABASE", raising=False)

    section = status_cli._db_section()

    assert section["ddb"]["reachable"] is False
    assert "Missing the DuckDB database path" in section["ddb"]["error"]


def test_db_section_refuses_a_duckdb_target_carrying_a_host(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A target configured both ways is a mistake, not something to half-use."""
    _duckdb_target(monkeypatch, tmp_path)
    monkeypatch.setenv("DDB_HOST", "db.example.invalid")

    section = status_cli._db_section()

    assert section["ddb"]["reachable"] is False
    assert "DDB_HOST" in section["ddb"]["error"]


def test_missing_psycopg_no_longer_reddens_a_duckdb_target(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """psycopg's absence says nothing about a database opened in-process.

    The section used to answer "psycopg not installed" for *every* target, which
    for a DuckDB target is both false and unactionable.
    """
    # Imported before psycopg is broken: _probe_target imports this module
    # lazily, and _common imports psycopg at its top.
    import dataplat.cli.db._common  # noqa: F401

    _duckdb_target(monkeypatch, tmp_path)
    monkeypatch.setenv("DP_TARGETS", "ddb,demo_pg")
    monkeypatch.setitem(sys.modules, "psycopg", None)

    section = status_cli._db_section()

    # Key order is the configured order, whichever path each target took.
    assert list(section) == ["ddb", "demo_pg"]
    assert section["ddb"]["reachable"] is True
    assert section["demo_pg"]["reachable"] is False
    assert "psycopg not installed" in section["demo_pg"]["error"]


def test_db_section_probes_a_mixed_target_list(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """One unreachable server must not affect the DuckDB target, or the order."""
    import psycopg

    _duckdb_target(monkeypatch, tmp_path)
    monkeypatch.setenv("DP_TARGETS", "ddb,demo")
    monkeypatch.setenv("DEMO_ENGINE", "postgresql")
    monkeypatch.setenv("DEMO_HOST", "db.example.invalid")
    monkeypatch.setenv("DEMO_USER", "svc")
    monkeypatch.setenv("DEMO_DATABASE", "analytics")

    def fake_connect(**kwargs: Any) -> _FakeConn:
        raise psycopg.OperationalError("connection to server failed")

    monkeypatch.setattr(psycopg, "connect", fake_connect)

    section = status_cli._db_section()

    assert list(section) == ["ddb", "demo"]
    assert section["ddb"]["reachable"] is True
    assert section["demo"]["reachable"] is False


def test_overview_never_calls_an_unanswerable_count_all_clear(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The rendered line is where "not applicable" has to beat "0"."""
    _disable_envrc(monkeypatch)
    _patch_sections(monkeypatch)
    monkeypatch.setattr(
        status_cli,
        "_db_section",
        lambda: {
            "ddb": {
                "reachable": True,
                "long_running": None,
                "long_running_note": "not applicable on DuckDB (no other sessions)",
            }
        },
    )

    result = runner.invoke(main_module.app, ["status"])

    out = _flat(result.output)
    assert result.exit_code == 0, result.output
    assert "✓ ddb" in out  # reachable, and marked as healthy
    assert "not applicable on DuckDB (no other sessions)" in out
    assert "no long-running queries" not in out


def test_overview_still_defaults_a_missing_count_to_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only an explicit None means "unanswerable"; an absent key is unchanged."""
    _disable_envrc(monkeypatch)
    _patch_sections(monkeypatch)
    monkeypatch.setattr(
        status_cli, "_db_section", lambda: {"demo": {"reachable": True}}
    )

    result = runner.invoke(main_module.app, ["status"])

    assert result.exit_code == 0, result.output
    assert "no long-running queries" in _flat(result.output)


def test_overview_escapes_a_hostile_not_applicable_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The note reaches Rich as markup, so it takes the same treatment as errors."""
    _disable_envrc(monkeypatch)
    _patch_sections(monkeypatch)
    monkeypatch.setattr(
        status_cli,
        "_db_section",
        lambda: {
            "tgt": {
                "reachable": True,
                "long_running": None,
                "long_running_note": HOSTILE,
            }
        },
    )

    result = runner.invoke(main_module.app, ["status"])

    assert result.exception is None, result.exception
    assert result.exit_code == 0, result.output
    assert HOSTILE in _flat(result.output)


def test_the_duckdb_probe_lets_go_of_the_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A health check must not sit on DuckDB's single-writer lock.

    Probed on duckdb 1.5.5: while one connection to a file is alive, opening the
    same file with a *different* configuration raises ConnectionException. So a
    read-only open succeeding right after the probe is proof the probe closed.
    """
    import duckdb

    path = _duckdb_target(monkeypatch, tmp_path)

    assert status_cli._db_section()["ddb"]["reachable"] is True

    duckdb.connect(str(path), read_only=True).close()  # raises if still held


def test_the_duckdb_probe_opens_the_target_as_configured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`reachable` has to mean "a db command would connect", flags included.

    A read-only target opens read-only, and the pair that cannot work — an
    in-memory database asked for read-only, which DuckDB refuses outright — is
    reported unreachable rather than smoothed over by opening it some other way.
    Every command would fail on that target; the dashboard says so first.
    """
    _duckdb_target(monkeypatch, tmp_path)
    monkeypatch.setenv("DDB_READ_ONLY", "1")

    assert status_cli._db_section()["ddb"]["reachable"] is True

    monkeypatch.setenv("DDB_PATH", ":memory:")
    section = status_cli._db_section()

    assert section["ddb"]["reachable"] is False
    assert "read-only" in section["ddb"]["error"]
