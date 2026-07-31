"""Tests for the Redshift harness itself, run without a Redshift cluster.

The harness has two jobs: refuse to mutate a cluster somebody depends on, and
refuse to pretend it tested anything when no cluster answered. Both are
verifiable here and now, because neither depends on the dialect:

* the read-only guard is a lexical analyser over statement text — pure
  functions, no server involved at all;
* the availability and disposability gates read environment variables;
* the server-side ``READ ONLY`` transaction is standard SQL, so PostgreSQL is a
  faithful stand-in for *whether the mechanism engages*.

Where the stand-in stops being valid: PostgreSQL cannot tell us what Redshift's
catalog contains, whether Redshift honours ``BEGIN READ ONLY``, or how its
transactional DDL behaves. Those are the questions the ``redshift`` marker
exists for, and nothing in this file claims to answer them. What this file does
claim is that pointing the read-only tier at a production warehouse cannot
damage it — which has to be true *before* anyone is asked for a cluster.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from tests.integration.redshift.conftest import (
    AFFIRMATIVE,
    DISPOSABLE_ENV_VAR,
    DSN_ENV_VAR,
    REQUIRED_ENV_VAR,
    TARGET_ENV_VAR,
    ConformanceLog,
    ReadOnlyCursor,
    ReadOnlyViolation,
    assert_read_only,
    enable_read_only,
    explicit_yes,
    redact,
    require_disposable,
    resolve_rs_source,
    truthy,
)

# The PostgreSQL container the rest of the integration suite uses. Only the
# statements-reach-the-server tests need it; everything else is pure.
_PG_DSN = os.environ.get(
    "DP_TEST_PG_DSN", "postgresql://postgres:postgres@127.0.0.1:55432/dataplat_test"
)


# --- the read-only guard: what it allows ------------------------------------


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT 1",
        "select 1",
        "   \n\t SELECT 1",
        "SELECT 1;",
        "SELECT 1 ;  ",
        "/* leading comment */ SELECT 1",
        "-- leading comment\nSELECT 1",
        "WITH x AS (SELECT 1) SELECT * FROM x",
        "EXPLAIN SELECT 1",
        "SHOW search_path",
        # Catalog reads that contain forbidden *substrings* inside identifiers:
        # last_analyze and stl_load_errors must stay single tokens.
        "SELECT last_analyze FROM svv_table_info",
        "SELECT * FROM stl_load_errors",
        # CASE ... END is unavoidable in catalog queries, so `end` is allowed.
        "SELECT CASE WHEN true THEN 1 ELSE 2 END",
    ],
)
def test_guard_allows_reads(statement: str) -> None:
    assert assert_read_only(statement)


# --- the read-only guard: what it refuses -----------------------------------


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO t VALUES (1)",
        "UPDATE t SET x = 1",
        "DELETE FROM t",
        "TRUNCATE t",
        "DROP TABLE t",
        "CREATE TABLE t (id int)",
        "ALTER TABLE t ADD COLUMN c int",
        "GRANT SELECT ON t TO r",
        "REVOKE SELECT ON t FROM r",
        "COPY t FROM 's3://b/k'",
        "UNLOAD ('select 1') TO 's3://b/k'",
        "VACUUM",
        "ANALYZE t",
        "SET search_path = x",
        "BEGIN",
        "COMMIT",
        "CALL some_proc()",
        "DO $$ BEGIN END $$",
        "LOCK TABLE t",
        "REFRESH MATERIALIZED VIEW m",
        "COMMENT ON TABLE t IS 'x'",
    ],
)
def test_guard_refuses_writes(statement: str) -> None:
    with pytest.raises(ReadOnlyViolation):
        assert_read_only(statement)


@pytest.mark.parametrize(
    ("statement", "why"),
    [
        ("/* x */ DROP TABLE t", "a comment must not disguise the first keyword"),
        ("-- x\nDROP TABLE t", "nor a line comment"),
        ("SELECT 1; DROP TABLE t", "a second statement rides the simple protocol"),
        ("; DROP TABLE t", "a leading semicolon must not shift the first keyword"),
        (
            "SELECT '--' ; DROP TABLE t",
            "the splitter must respect string literals: naive comment stripping "
            "deletes the rest of the line and hides the DROP",
        ),
        (
            "SELECT '/*' ; DROP TABLE t",
            "same trap with a block-comment opener inside a literal",
        ),
        (
            'SELECT "--" ; DROP TABLE t',
            "and with a quoted identifier",
        ),
        (
            "WITH x AS (DELETE FROM t RETURNING *) SELECT * FROM x",
            "a data-modifying CTE is a write wearing a SELECT",
        ),
        (
            "WITH x AS (INSERT INTO t VALUES (1) RETURNING *) SELECT * FROM x",
            "likewise INSERT",
        ),
        ("SELECT * INTO new_t FROM t", "SELECT ... INTO creates a table"),
        ("SELECT pg_terminate_backend(1)", "dp db kill does this for a living"),
        ("SELECT pg_cancel_backend(1)", "and this"),
        ("SELECT nextval('s')", "a sequence advance is a write"),
        ("SELECT setval('s', 1)", "so is a sequence reset"),
        ("SELECT pg_stat_reset()", "resetting statistics is a write"),
        ("EXPLAIN ANALYZE SELECT 1", "EXPLAIN ANALYZE executes the statement"),
        ("EXPLAIN DELETE FROM t", "EXPLAIN of a non-SELECT"),
        ("", "an empty statement is not a read"),
        ("/* only a comment */", "nor is a comment alone"),
    ],
)
def test_guard_refuses_evasions(statement: str, why: str) -> None:
    with pytest.raises(ReadOnlyViolation):
        assert_read_only(statement)


def test_guard_message_names_the_escape_hatch() -> None:
    """A tripped guard must tell the author what to do instead."""
    with pytest.raises(ReadOnlyViolation) as excinfo:
        assert_read_only("DROP TABLE t")
    message = str(excinfo.value)
    assert "rs_ddl_cursor" in message
    assert DISPOSABLE_ENV_VAR in message
    assert "DROP TABLE t" in message


def test_violation_is_not_a_psycopg_error() -> None:
    """Code under test catches psycopg.Error; it must not swallow this."""
    psycopg = pytest.importorskip("psycopg")
    assert not issubclass(ReadOnlyViolation, psycopg.Error)
    assert issubclass(ReadOnlyViolation, AssertionError)


# --- the cursor wrapper -----------------------------------------------------


class _RecordingCursor:
    """Stands in for a psycopg cursor, recording what reached the server."""

    def __init__(self) -> None:
        self.executed: list[str] = []

    def execute(self, query: object, params: object = None, **kwargs: Any) -> None:
        self.executed.append(str(query))

    def close(self) -> None:  # pragma: no cover - exercised via __exit__
        pass


def test_refused_statement_never_reaches_the_server() -> None:
    """The point of a client-side guard: the write is never sent.

    A server-side check would also refuse it, but only after the statement had
    crossed the network to a cluster that may be production.
    """
    inner = _RecordingCursor()
    cursor = ReadOnlyCursor(inner)  # type: ignore[arg-type]

    with pytest.raises(ReadOnlyViolation):
        cursor.execute("DROP TABLE t")

    assert inner.executed == []


def test_allowed_statement_is_forwarded_once() -> None:
    inner = _RecordingCursor()
    cursor = ReadOnlyCursor(inner)  # type: ignore[arg-type]

    cursor.execute("SELECT 1")

    assert inner.executed == ["SELECT 1"]


@pytest.mark.parametrize("method", ["executemany", "copy"])
def test_write_only_methods_are_refused_outright(method: str) -> None:
    """Wrapping rather than subclassing makes these opt-in; they stay refused."""
    inner = _RecordingCursor()
    cursor = ReadOnlyCursor(inner)  # type: ignore[arg-type]

    with pytest.raises(ReadOnlyViolation):
        getattr(cursor, method)("INSERT INTO t VALUES (1)", [(1,)])

    assert inner.executed == []


def test_private_attributes_are_not_forwarded() -> None:
    cursor = ReadOnlyCursor(_RecordingCursor())  # type: ignore[arg-type]
    with pytest.raises(AttributeError):
        _ = cursor._not_a_real_attribute


# --- environment gates: the asymmetry is the safety property ----------------


@pytest.mark.parametrize("raw", ["1", "true", "yes", "on", "TRUE", "ture", "banana"])
def test_required_is_lenient(raw: str) -> None:
    """A typo must not silently downgrade CI's hard failure into a skip."""
    assert truthy(raw) is True


@pytest.mark.parametrize("raw", [None, "", "0", "false", "no", "off", "  OFF  "])
def test_required_negatives(raw: str | None) -> None:
    assert truthy(raw) is False


@pytest.mark.parametrize("raw", list(AFFIRMATIVE) + ["  YES  ", "True"])
def test_disposable_accepts_only_explicit_affirmatives(raw: str) -> None:
    assert explicit_yes(raw) is True


@pytest.mark.parametrize("raw", [None, "", "0", "false", "ture", "banana", "maybe"])
def test_disposable_reads_anything_unclear_as_no(raw: str | None) -> None:
    """A typo must never be read as permission to mutate someone's cluster."""
    assert explicit_yes(raw) is False


def test_the_two_readers_disagree_on_a_typo() -> None:
    """Spelled out because it looks like a bug until you know it is the design."""
    assert truthy("ture") is True
    assert explicit_yes("ture") is False


# --- availability gate ------------------------------------------------------

_UNREACHABLE = "postgresql://nobody:secret@127.0.0.1:1/nope"


def test_unconfigured_skips(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (TARGET_ENV_VAR, DSN_ENV_VAR, REQUIRED_ENV_VAR):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(pytest.skip.Exception):
        resolve_rs_source()


def test_unreachable_skips_without_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(TARGET_ENV_VAR, raising=False)
    monkeypatch.delenv(REQUIRED_ENV_VAR, raising=False)
    monkeypatch.setenv(DSN_ENV_VAR, _UNREACHABLE)
    with pytest.raises(pytest.skip.Exception):
        resolve_rs_source()


def test_unreachable_fails_when_required(monkeypatch: pytest.MonkeyPatch) -> None:
    """The line that stops a broken cluster from reporting success."""
    monkeypatch.delenv(TARGET_ENV_VAR, raising=False)
    monkeypatch.setenv(DSN_ENV_VAR, _UNREACHABLE)
    monkeypatch.setenv(REQUIRED_ENV_VAR, "1")
    with pytest.raises(pytest.fail.Exception):
        resolve_rs_source()


def test_unreachable_message_redacts_the_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(TARGET_ENV_VAR, raising=False)
    monkeypatch.setenv(DSN_ENV_VAR, _UNREACHABLE)
    monkeypatch.setenv(REQUIRED_ENV_VAR, "1")
    with pytest.raises(pytest.fail.Exception) as excinfo:
        resolve_rs_source()
    assert "secret" not in str(excinfo.value)


# --- disposability gate -----------------------------------------------------


def test_ddl_is_refused_without_the_disposable_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reachable and required is still not permission to mutate."""
    monkeypatch.setenv(REQUIRED_ENV_VAR, "1")
    monkeypatch.delenv(DISPOSABLE_ENV_VAR, raising=False)
    with pytest.raises(pytest.skip.Exception) as excinfo:
        require_disposable()
    assert DISPOSABLE_ENV_VAR in str(excinfo.value)


def test_ddl_is_permitted_with_the_disposable_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(DISPOSABLE_ENV_VAR, "1")
    require_disposable()


# --- password hygiene -------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "postgresql://user:hunter2@host:5439/dev",
        "host=h user=u password=hunter2 dbname=d",
        "host=h user=u password='hunter2' dbname=d",
        "host=h sslpassword=hunter2",
        "connection failed for password=hunter2",
    ],
)
def test_redact_removes_passwords(text: str) -> None:
    assert "hunter2" not in redact(text)


def test_redact_keeps_the_rest_legible() -> None:
    out = redact("postgresql://user:hunter2@cluster.example.com:5439/dev")
    assert "cluster.example.com" in out
    assert "user" in out


# --- conformance collector --------------------------------------------------


def test_conformance_records_and_renders() -> None:
    log = ConformanceLog()
    log.record("pg_user.passwd masked", True, detail="returned '********'")
    log.record("aclexplode present", False)

    rendered = log.render()

    assert "pg_user.passwd masked" in rendered
    assert "aclexplode present" in rendered
    assert "returned '********'" in rendered
    assert len(log.entries) == 2


# --- the server-side layer, against a real server ---------------------------


@pytest.mark.integration
def test_server_side_read_only_blocks_a_bypass() -> None:
    """The hole the client-side guard cannot close, closed by the server.

    ``cursor.connection.execute(...)`` sidesteps ReadOnlyCursor entirely, which
    the wrapper's own docstring admits. PostgreSQL is a faithful stand-in for
    whether ``READ ONLY`` engages at all; whether Redshift honours it is a
    question for the conformance tier, which records the answer.
    """
    psycopg = pytest.importorskip("psycopg")
    try:
        conn = psycopg.connect(_PG_DSN, connect_timeout=5)
    except psycopg.Error as exc:  # pragma: no cover - depends on the container
        pytest.skip(f"no PostgreSQL stand-in available: {redact(str(exc))}")

    with conn:
        state = enable_read_only(conn)
        # accepted and confirmed are separate facts by design; PostgreSQL should
        # give us both, and the conformance tier records whatever Redshift gives.
        assert state.accepted, f"READ ONLY was refused: {state.detail}"
        assert state.confirmed is True, f"not enforced: {state.answer}"
        with conn.cursor() as raw, pytest.raises(psycopg.Error) as excinfo:
            raw.execute("CREATE TEMP TABLE dp_rs_harness_probe (id int)")
        assert "read-only" in str(excinfo.value).lower()
    conn.close()


@pytest.mark.integration
def test_guarded_cursor_still_reads_against_a_real_server() -> None:
    """The guard must not be so strict that real catalog queries stop working."""
    psycopg = pytest.importorskip("psycopg")
    try:
        conn = psycopg.connect(_PG_DSN, connect_timeout=5)
    except psycopg.Error as exc:  # pragma: no cover - depends on the container
        pytest.skip(f"no PostgreSQL stand-in available: {redact(str(exc))}")

    with conn:
        enable_read_only(conn)
        with conn.cursor() as raw:
            cursor = ReadOnlyCursor(raw)
            cursor.execute("SELECT 1")
            assert cursor.fetchone() == (1,)
            cursor.execute(
                "SELECT nspname FROM pg_namespace WHERE nspname = %s", ("public",)
            )
            assert cursor.fetchone() == ("public",)
    conn.close()


def test_setup_hint_dsn_example_is_actually_parseable() -> None:
    """The hint is the only instruction a stuck user gets; it must be runnable.

    It advertised a redshift:// URL, which libpq rejects outright as an invalid
    connection option — so following the harness's own guidance produced a
    parse error rather than a connection. Redshift speaks the PostgreSQL wire
    protocol and libpq knows only postgresql:// and postgres://.
    """
    import re

    from psycopg.conninfo import conninfo_to_dict

    from tests.integration.redshift.conftest import _SETUP_HINT

    # Only quoted URLs: the hint also mentions schemes in prose, and a sentence
    # is not a connection string.
    examples = re.findall(r"'([a-z][a-z0-9+.-]*://[^']+)'", _SETUP_HINT)
    assert examples, f"no quoted DSN example found in the hint:\n{_SETUP_HINT}"
    for dsn in examples:
        # Raises psycopg.ProgrammingError if the scheme is one libpq refuses.
        assert conninfo_to_dict(dsn), dsn


def test_a_named_redshift_target_survives_the_suite_isolation() -> None:
    """DP_TEST_RS_TARGET has to reach the target registry to mean anything.

    tests/conftest.py assigns DP_TARGETS so the suite's fixtures cannot be
    changed by a developer's shell, and that assignment silently erased a real
    cluster's target name — making the target form the hint documents impossible
    under pytest. It now appends instead.
    """
    import os

    declared = os.environ.get("DP_TARGETS", "").split(",")
    assert "demo_pg" in declared and "demo_rs" in declared
    named = os.environ.get("DP_TEST_RS_TARGET", "").strip()
    if named:
        assert named in declared, (
            f"DP_TEST_RS_TARGET={named!r} was not appended to DP_TARGETS: {declared}"
        )
