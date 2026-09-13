"""`dp people` across two warehouses and Superset, without any of them.

The plan is the product here: three systems, no transaction spanning them, and
the only chance to see the whole intent is before the first account exists. So
most of these tests assert on what the plan says and on the fact that nothing
was written.
"""

from __future__ import annotations

import contextlib
import csv
import json
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from dataplat.cli import _credentials
from dataplat.cli.people import offboard as offboard_cli
from dataplat.cli.people import onboard as onboard_cli
from dataplat.cli.people.app import app as people_app
from dataplat.core.errors import ExitCode

runner = CliRunner()
WIDE = {"COLUMNS": "200"}

SUPERSET_USERS: list[dict[str, Any]] = [
    {
        "id": 7,
        "username": "hans.lemm",
        "first_name": "Hans",
        "last_name": "Lemm",
        "email": "hans.lemm@betterdoc.de",
        "active": True,
        "roles": [{"id": 2, "name": "Gamma"}],
        "groups": [{"id": 10, "name": "analysts"}],
    }
]
# Eleven ordinary colleagues, because the rule is "few accounts have this" and
# a fixture of three cannot express one in ten. Gamma is universal here, the
# analysts group is a third of the company, and anything held by one account
# out of twelve is what the warning exists for.
SUPERSET_USERS.extend(
    [
        {
            "id": 100 + n,
            "username": f"colleague{n}",
            "roles": [{"id": 2, "name": "Gamma"}],
            "groups": [{"id": 10, "name": "analysts"}] if n < 3 else [],
        }
        for n in range(11)
    ]
)
SUPERSET_ROLES = [{"id": 1, "name": "Admin"}, {"id": 2, "name": "Gamma"}]
SUPERSET_GROUPS = [{"id": 10, "name": "analysts"}]


class FakeSuperset:
    def __init__(self) -> None:
        self.created: list[dict] = []
        self.updated: list[tuple[str, dict]] = []
        self.deleted: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        if method == "POST" and path.endswith("/security/login"):
            return httpx.Response(200, json={"access_token": "tok"})
        if method == "GET" and path.endswith("/security/users/"):
            return httpx.Response(
                200, json={"result": SUPERSET_USERS, "count": len(SUPERSET_USERS)}
            )
        if method == "GET" and path.endswith("/security/roles/"):
            return httpx.Response(
                200, json={"result": SUPERSET_ROLES, "count": len(SUPERSET_ROLES)}
            )
        if method == "GET" and path.endswith("/security/groups/"):
            return httpx.Response(
                200, json={"result": SUPERSET_GROUPS, "count": len(SUPERSET_GROUPS)}
            )
        if method == "POST" and path.endswith("/security/users/"):
            self.created.append(json.loads(request.content))
            return httpx.Response(201, json={"id": 99})
        if method == "PUT" and "/security/users/" in path:
            self.updated.append((path.rsplit("/", 1)[-1], json.loads(request.content)))
            return httpx.Response(200, json={})
        if method == "DELETE" and "/security/users/" in path:
            self.deleted.append(path.rsplit("/", 1)[-1])
            return httpx.Response(200, json={})
        raise AssertionError(f"unexpected request: {method} {path}")


class _Cursor:
    """Answers the two reads `access.py` makes, and records every statement."""

    def __init__(self, *, users: dict[str, int], memberships: dict[str, list[str]]):
        self._users = users
        self._memberships = memberships
        self._result: list[tuple] = []
        self.statements: list[str] = []

    def execute(self, query: Any, params: Any = None) -> None:
        if hasattr(query, "as_string"):  # a psycopg Composed: this is a write
            self.statements.append(query.as_string(None))
            self._result = []
            return
        text = str(query)
        self.statements.append(text)
        name = params[0] if params else None
        if "rolcanlogin" in text or "FROM pg_user" in text:
            oid = self._users.get(str(name))
            self._result = [(oid, True, False)] if oid is not None else []
        elif "FROM pg_group" in text and "groname = " in text:
            self._result = []
        elif "pg_auth_members" in text or "FROM pg_group g" in text:
            by_oid = {oid: n for n, oid in self._users.items()}
            owner = by_oid.get(int(name)) if name is not None else None
            self._result = [
                (role, True, 1, "") for role in self._memberships.get(owner or "", [])
            ]
        else:
            self._result = []

    def fetchall(self) -> list[tuple]:
        return self._result

    def fetchone(self):
        return self._result[0] if self._result else None


class _Conn:
    def __init__(self, cursor: _Cursor) -> None:
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def cursor(self):
        return self._cursor

    def commit(self) -> None:
        return None


@pytest.fixture(autouse=True)
def env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DP_TARGETS", "dataocean,betterdata")
    for prefix, engine, template in (
        ("DATAOCEAN", "postgresql", "bd_{first_initial}{last}"),
        ("BETTERDATA", "redshift", "{first_initial}_{last}"),
    ):
        monkeypatch.setenv(f"{prefix}_HOST", f"{prefix.lower()}.test")
        monkeypatch.setenv(f"{prefix}_DATABASE", prefix.lower())
        monkeypatch.setenv(f"{prefix}_USER", "admin")
        monkeypatch.setenv(f"{prefix}_PASSWORD", "secret")
        monkeypatch.setenv(f"{prefix}_ENGINE", engine)
        monkeypatch.setenv(f"{prefix}_USERNAME_TEMPLATE", template)
    monkeypatch.setenv("SUPERSET_BASE_URL", "https://superset.test")
    monkeypatch.setenv("SUPERSET_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("SUPERSET_ADMIN_PASSWORD", "secret")


@pytest.fixture
def warehouses(monkeypatch: pytest.MonkeyPatch) -> dict[str, _Cursor]:
    """A cursor per target, routed by the database the params name."""
    cursors = {
        "dataocean": _Cursor(
            users={"bd_hlemm": 16384},
            memberships={"bd_hlemm": ["team_dna", "pii_users"]},
        ),
        "betterdata": _Cursor(
            users={"h_lemm": 110}, memberships={"h_lemm": ["pii_users"]}
        ),
    }

    @contextlib.contextmanager
    def _session(params):
        yield _Conn(cursors[params.dbname])

    # Both commands import the session funnel, so both are swapped: a test
    # that patched one would silently dial a real warehouse from the other.
    monkeypatch.setattr(onboard_cli, "db_session", _session)
    monkeypatch.setattr(offboard_cli, "db_session", _session)
    return cursors


@pytest.fixture
def superset(monkeypatch: pytest.MonkeyPatch) -> FakeSuperset:
    fake = FakeSuperset()
    transport = httpx.MockTransport(fake.handler)
    real_client = httpx.Client
    monkeypatch.setattr(
        httpx, "Client", lambda *a, **kw: real_client(transport=transport)
    )
    return fake


def _onboard(*args: str):
    return runner.invoke(
        people_app,
        [
            "onboard",
            "eva.germeshausen@betterdoc.de",
            "--like",
            "hans.lemm@betterdoc.de",
            *args,
        ],
        env=WIDE,
    )


def test_the_plan_names_each_area_and_its_own_convention(
    warehouses: dict[str, _Cursor], superset: FakeSuperset
) -> None:
    result = _onboard("--dry-run")

    assert result.exit_code == 0, result.output
    assert "bd_egermeshausen" in result.output  # dataocean's convention
    assert "e_germeshausen" in result.output  # betterdata's
    assert "eva.germeshausen" in result.output  # superset's


def test_the_plan_shows_what_each_account_would_copy(
    warehouses: dict[str, _Cursor], superset: FakeSuperset
) -> None:
    result = _onboard("--dry-run")

    assert "team_dna" in result.output
    assert "Gamma" in result.output


def test_a_dry_run_writes_nothing_anywhere(
    warehouses: dict[str, _Cursor], superset: FakeSuperset
) -> None:
    """The whole point of a plan: it costs nothing to be wrong about."""
    result = _onboard("--dry-run")

    assert result.exit_code == 0, result.output
    assert superset.created == []
    for cursor in warehouses.values():
        assert not any(
            sql.strip().upper().startswith(("CREATE", "GRANT", "ALTER", "DROP"))
            for sql in cursor.statements
        )


def test_target_narrows_the_plan(
    warehouses: dict[str, _Cursor], superset: FakeSuperset
) -> None:
    result = _onboard("--dry-run", "--target", "dataocean")

    assert "bd_egermeshausen" in result.output
    assert "e_germeshausen" not in result.output


def test_an_unknown_target_is_refused_before_anything_is_read(
    warehouses: dict[str, _Cursor], superset: FakeSuperset
) -> None:
    result = _onboard("--dry-run", "--target", "nope")

    assert result.exit_code == ExitCode.INVALID_INPUT
    assert "nope" in result.output


def test_no_superset_drops_that_area(
    warehouses: dict[str, _Cursor], superset: FakeSuperset
) -> None:
    result = _onboard("--dry-run", "--no-superset")

    assert result.exit_code == 0, result.output
    assert "bd_egermeshausen" in result.output
    assert "superset" not in result.output.lower()


def test_a_target_without_a_template_is_skipped_and_says_why(
    monkeypatch: pytest.MonkeyPatch,
    warehouses: dict[str, _Cursor],
    superset: FakeSuperset,
) -> None:
    """An SSO-managed warehouse opts out by configuring nothing."""
    monkeypatch.delenv("BETTERDATA_USERNAME_TEMPLATE")

    result = _onboard("--dry-run")

    assert result.exit_code == 0, result.output
    assert "no username template" in result.output
    assert "e_germeshausen" not in result.output


def test_a_reference_person_absent_from_a_target_skips_it(
    warehouses: dict[str, _Cursor], superset: FakeSuperset
) -> None:
    """Copying nobody's access would make an account that grants nothing."""
    warehouses["betterdata"]._users = {}

    result = _onboard("--dry-run")

    assert result.exit_code == 0, result.output
    assert "has no account here" in result.output


def test_an_email_that_cannot_fill_the_template_is_refused(
    warehouses: dict[str, _Cursor], superset: FakeSuperset
) -> None:
    """`bd_e` is plausible and wrong; the command refuses rather than inventing it."""
    result = runner.invoke(
        people_app,
        [
            "onboard",
            "eva@betterdoc.de",
            "--like",
            "hans.lemm@betterdoc.de",
            "--dry-run",
        ],
        env=WIDE,
    )

    assert result.exit_code == ExitCode.INVALID_INPUT
    assert "last" in result.output


def test_a_grant_hardly_anyone_holds_is_flagged_with_its_count(
    warehouses: dict[str, _Cursor], superset: FakeSuperset
) -> None:
    """`--like` copies a privilege level, and nobody reads the Copies column."""
    SUPERSET_USERS[0]["roles"] = [
        {"id": 2, "name": "Gamma"},
        {"id": 1, "name": "Admin"},
    ]
    try:
        result = _onboard("--dry-run")
    finally:
        SUPERSET_USERS[0]["roles"] = [{"id": 2, "name": "Gamma"}]

    assert result.exit_code == 0, result.output
    assert "Admin (1 of 12 accounts)" in result.output
    assert "check it is intended" in result.output


def test_an_ordinary_grant_is_not_flagged(
    warehouses: dict[str, _Cursor], superset: FakeSuperset
) -> None:
    """A warning that fires on the normal case is one people learn to skip."""
    result = _onboard("--dry-run", "--no-db")

    assert result.exit_code == 0, result.output
    assert "check it is intended" not in result.output
    assert "check them is intended" not in result.output


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------
# Three systems and no transaction between them, so the rules are: record a
# credential the instant its account exists, never roll back across areas, and
# report every area's outcome.


def _written(tmp_path) -> list[list[str]]:
    files = list((tmp_path / "credentials").glob("dp-onboard-*.csv"))
    assert len(files) == 1, files
    return list(csv.reader(files[0].read_text().splitlines()))


@pytest.fixture
def creds(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setattr(_credentials, "CREDENTIALS_DIR", tmp_path / "credentials")
    return tmp_path


def test_every_area_gets_its_account(
    warehouses: dict[str, _Cursor], superset: FakeSuperset, creds
) -> None:
    result = _onboard("--yes")

    assert result.exit_code == 0, result.output
    for cursor in warehouses.values():
        assert any("CREATE USER" in s or "CREATE ROLE" in s for s in cursor.statements)
    assert superset.created[0]["username"] == "eva.germeshausen"


def test_the_copied_memberships_are_granted(
    warehouses: dict[str, _Cursor], superset: FakeSuperset, creds
) -> None:
    _onboard("--yes")

    granted = " ".join(warehouses["dataocean"].statements)
    assert "team_dna" in granted
    assert "pii_users" in granted
    # Superset resolves its names to the ids the API takes.
    assert superset.created[0]["roles"] == [2]


def test_each_area_gets_a_password_of_its_own(
    warehouses: dict[str, _Cursor], superset: FakeSuperset, creds
) -> None:
    """Three systems that can be compromised separately get three secrets."""
    _onboard("--yes")

    rows = _written(creds)[1:]
    passwords = {row[1] for row in rows}
    assert len(passwords) == 3
    assert all(len(p) == 32 for p in passwords)


def test_the_credentials_file_records_every_area_and_stays_private(
    warehouses: dict[str, _Cursor], superset: FakeSuperset, creds
) -> None:
    result = _onboard("--yes")

    rows = _written(creds)
    assert rows[0] == ["username", "password", "created_at", "scope"]
    assert {row[0] for row in rows[1:]} == {
        "bd_egermeshausen",
        "e_germeshausen",
        "eva.germeshausen",
    }
    assert {row[3] for row in rows[1:]} == {"dataocean", "betterdata", "superset"}

    written = next((creds / "credentials").glob("dp-onboard-*.csv"))
    assert written.stat().st_mode & 0o077 == 0
    for row in rows[1:]:
        assert row[1] not in result.output


def test_an_area_that_fails_does_not_take_the_others_with_it(
    warehouses: dict[str, _Cursor], superset: FakeSuperset, creds
) -> None:
    """No rollback across areas: what succeeded stays, and is reported."""
    failing = warehouses["betterdata"]
    original = failing.execute

    def boom(query, params=None):
        if hasattr(query, "as_string"):
            raise RuntimeError("cluster is read-only")
        original(query, params)

    failing.execute = boom  # type: ignore[method-assign]

    result = _onboard("--yes")

    assert result.exit_code != 0
    assert "betterdata" in result.output
    recorded = {row[3] for row in _written(creds)[1:]}
    assert recorded == {"dataocean", "superset"}
    assert superset.created  # the area after the failure still ran


def test_nothing_is_created_without_a_confirmation(
    warehouses: dict[str, _Cursor], superset: FakeSuperset, creds
) -> None:
    result = _onboard()

    assert result.exit_code != 0
    assert superset.created == []
    assert not (creds / "credentials").exists()


# ---------------------------------------------------------------------------
# offboard
# ---------------------------------------------------------------------------
# Disable, do not destroy. A dropped role takes the ownership of everything it
# owned with it -- one real account owns 459 relations -- and no CLI summary
# makes that recoverable.


def _offboard(*args: str):
    return runner.invoke(
        people_app,
        ["offboard", "hans.lemm@betterdoc.de", *args],
        env=WIDE,
    )


def test_offboard_turns_every_account_off(
    warehouses: dict[str, _Cursor], superset: FakeSuperset
) -> None:
    result = _offboard("--yes")

    assert result.exit_code == 0, result.output
    assert any("NOLOGIN" in s for s in warehouses["dataocean"].statements)
    assert any("PASSWORD DISABLE" in s for s in warehouses["betterdata"].statements)
    assert superset.updated == [("7", {"active": False})]


def test_offboard_revokes_the_memberships(
    warehouses: dict[str, _Cursor], superset: FakeSuperset
) -> None:
    _offboard("--yes")

    revoked = " ".join(warehouses["dataocean"].statements)
    assert "REVOKE" in revoked
    assert "team_dna" in revoked


def test_offboard_destroys_nothing(
    warehouses: dict[str, _Cursor], superset: FakeSuperset
) -> None:
    """The whole point of the default: everything it owns keeps its owner."""
    _offboard("--yes")

    assert superset.deleted == []
    for cursor in warehouses.values():
        assert not any("DROP ROLE" in s or "DROP USER" in s for s in cursor.statements)


def test_offboard_dry_run_changes_nothing(
    warehouses: dict[str, _Cursor], superset: FakeSuperset
) -> None:
    result = _offboard("--dry-run")

    assert result.exit_code == 0, result.output
    assert superset.updated == []
    for cursor in warehouses.values():
        assert not any(
            s.strip().upper().startswith(("ALTER", "REVOKE")) for s in cursor.statements
        )


def test_offboard_skips_an_area_the_person_is_not_on(
    warehouses: dict[str, _Cursor], superset: FakeSuperset
) -> None:
    warehouses["betterdata"]._users = {}

    result = _offboard("--dry-run")

    assert result.exit_code == 0, result.output
    assert "no account here" in result.output


def test_offboard_needs_a_confirmation(
    warehouses: dict[str, _Cursor], superset: FakeSuperset
) -> None:
    result = _offboard()

    assert result.exit_code != 0
    assert superset.updated == []


def test_offboard_narrows_to_one_target(
    warehouses: dict[str, _Cursor], superset: FakeSuperset
) -> None:
    result = _offboard("--dry-run", "--target", "dataocean")

    assert result.exit_code == 0, result.output
    assert "bd_hlemm" in result.output
    assert "h_lemm" not in result.output


def test_offboard_rejects_an_unknown_target(
    warehouses: dict[str, _Cursor], superset: FakeSuperset
) -> None:
    result = _offboard("--dry-run", "--target", "nope")

    assert result.exit_code == ExitCode.INVALID_INPUT
    assert "nope" in result.output


def test_offboard_can_skip_the_warehouses(
    warehouses: dict[str, _Cursor], superset: FakeSuperset
) -> None:
    result = _offboard("--dry-run", "--no-db")

    assert result.exit_code == 0, result.output
    assert "superset" in result.output
    assert "bd_hlemm" not in result.output


def test_offboard_says_when_there_is_nothing_to_disable(
    warehouses: dict[str, _Cursor], superset: FakeSuperset
) -> None:
    """Someone who was never onboarded, or already gone."""
    for cursor in warehouses.values():
        cursor._users = {}

    result = runner.invoke(
        people_app, ["offboard", "nobody.here@betterdoc.de", "--dry-run"], env=WIDE
    )

    assert result.exit_code == 0, result.output
    assert "Nothing to disable" in result.output


def test_offboard_reports_an_area_that_failed_and_keeps_going(
    warehouses: dict[str, _Cursor], superset: FakeSuperset
) -> None:
    """The account that could be disabled still is; the exit code says one wasn't."""
    failing = warehouses["dataocean"]
    original = failing.execute

    def boom(query, params=None):
        if hasattr(query, "as_string"):
            raise RuntimeError("cluster is read-only")
        original(query, params)

    failing.execute = boom  # type: ignore[method-assign]

    result = _offboard("--yes")

    assert result.exit_code != 0
    assert superset.updated == [("7", {"active": False})]
