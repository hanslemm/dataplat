"""What Redshift's catalog actually contains, asked rather than assumed.

Every fix on the Redshift path has so far rested on the evidence rules in
CONTRIBUTING.md — internal precedent, or a change that touches no Redshift SQL —
because no cluster was reachable. These tests convert the reasoning into
observation. Each one records its answer through the ``conformance`` fixture, so
a run prints a table of facts rather than only a pass count: the point is
learning what the engine does.

Read-only throughout: every statement goes through ``rs_cursor``, which refuses
anything that is not plainly a read. Safe to point at a warehouse in use.

**A refutation here is a success.** Two shipped fixes rest on assumptions this
file interrogates — that ``pg_user.passwd`` is masked, and that ``pg_roles`` is
present and ``has_schema_privilege`` works. If the cluster disagrees, the fix
was wrong and the assertion that fails is doing its job.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.integration.redshift.conftest import ConformanceLog, ReadOnlyCursor

pytestmark = pytest.mark.redshift


def _scalar(cursor: ReadOnlyCursor, sql: str, params: tuple[Any, ...] = ()) -> Any:
    cursor.execute(sql, params or None)
    row = cursor.fetchone()
    return None if row is None else row[0]


def _relation_readable(cursor: ReadOnlyCursor, relation: str) -> tuple[bool, str]:
    """Whether ``SELECT`` against ``relation`` works, and what happened if not.

    A missing relation is a finding, not an error: the whole question is which
    catalog objects this engine exposes.
    """
    psycopg = pytest.importorskip("psycopg")
    try:
        cursor.execute(f"SELECT 1 FROM {relation} LIMIT 1")  # noqa: S608
        cursor.fetchall()
    except psycopg.Error as exc:
        cursor.connection.rollback()
        return False, str(exc).strip().splitlines()[0]
    return True, ""


# --- the assumption behind password_set=None --------------------------------


def test_pg_user_passwd_is_masked(
    rs_cursor: ReadOnlyCursor, conformance: ConformanceLog
) -> None:
    """dataplat reports password_set as unknown on Redshift because of this.

    On PostgreSQL, ``pg_roles.rolpassword`` is the literal ``'********'`` and can
    never be NULL, which is why reading it produced "every role has a password".
    The Redshift path was changed to report unknown on the assumption that
    ``pg_user.passwd`` behaves the same way. If it does not — if a real hash or a
    NULL comes back — then unknown is over-cautious and the truth is knowable
    here.
    """
    readable, error = _relation_readable(rs_cursor, "pg_user")
    if not readable:
        conformance.record("is pg_user readable?", "no", error)
        pytest.skip(f"pg_user is not readable by this user: {error}")

    rs_cursor.execute(
        "SELECT usename, passwd FROM pg_user WHERE passwd IS NOT NULL LIMIT 5"
    )
    rows = rs_cursor.fetchall()
    if not rows:
        conformance.record(
            "pg_user.passwd masked?",
            "no rows with a non-NULL passwd",
            "either no user has a password or the column is NULL to this role",
        )
        pytest.skip("no non-NULL pg_user.passwd visible to this user")

    values = {str(row[1]) for row in rows}
    masked = values == {"********"}
    conformance.record(
        "pg_user.passwd masked?",
        "yes" if masked else "NO - real values visible",
        f"{len(rows)} row(s); distinct values: {sorted(values)}",
    )
    assert masked, (
        "pg_user.passwd is NOT masked on this cluster, so password_set could be "
        "reported truthfully instead of as unknown. See CONTRIBUTING.md — this "
        "refutes the assumption the current Redshift behaviour rests on."
    )


# --- the assumptions behind the schema-privileges query ---------------------


@pytest.mark.parametrize("relation", ["pg_roles", "pg_user", "pg_group"])
def test_role_catalogs_are_readable(
    rs_cursor: ReadOnlyCursor, conformance: ConformanceLog, relation: str
) -> None:
    """dataplat's Redshift schema-privileges query reads pg_roles.

    If that relation is absent on Redshift then the query has been broken for
    every user since it was written, and the recent USAGE fix inherited the
    problem. pg_user and pg_group are the 8.0-era relations the role service
    uses directly.
    """
    readable, error = _relation_readable(rs_cursor, relation)
    conformance.record(f"is {relation} readable?", "yes" if readable else "no", error)
    assert readable, f"{relation} is not readable: {error}"


@pytest.mark.parametrize("privilege", ["USAGE", "CREATE"])
def test_has_schema_privilege_works(
    rs_cursor: ReadOnlyCursor,
    conformance: ConformanceLog,
    rs_probe_schema: str,
    privilege: str,
) -> None:
    """The USAGE fix mirrors the CREATE half, which uses this function.

    Evidence class 2 in CONTRIBUTING.md was "the construct already runs on this
    path". This is the check that upgrades it to class 0.
    """
    psycopg = pytest.importorskip("psycopg")
    try:
        answer = _scalar(
            rs_cursor,
            "SELECT has_schema_privilege(current_user, %s, %s)",
            (rs_probe_schema, privilege),
        )
    except psycopg.Error as exc:
        rs_cursor.connection.rollback()
        detail = str(exc).strip().splitlines()[0]
        conformance.record(f"has_schema_privilege(…, {privilege!r})?", "no", detail)
        pytest.fail(f"has_schema_privilege with {privilege} failed: {detail}")

    conformance.record(
        f"has_schema_privilege(…, {privilege!r})?",
        "yes",
        f"current_user on {rs_probe_schema} -> {answer}",
    )
    assert answer in (True, False)


def test_information_schema_usage_privileges_has_no_schema_rows(
    rs_cursor: ReadOnlyCursor, conformance: ConformanceLog
) -> None:
    """The bug the USAGE fix replaced, confirmed on the engine itself.

    The old query filtered this view to ``object_type = 'SCHEMA'``. The SQL
    standard defines it over domains, collations and sequences, so the filter
    matched nothing and schema USAGE grants were invisible. If Redshift *does*
    expose SCHEMA rows here, the old query was salvageable and the replacement
    deserves a second look.
    """
    readable, error = _relation_readable(
        rs_cursor, "information_schema.usage_privileges"
    )
    if not readable:
        conformance.record("usage_privileges readable?", "no", error)
        pytest.skip(f"information_schema.usage_privileges unreadable: {error}")

    rs_cursor.execute(
        "SELECT DISTINCT object_type FROM information_schema.usage_privileges"
    )
    kinds = sorted({str(row[0]) for row in rs_cursor.fetchall()})
    conformance.record(
        "object_type values in usage_privileges",
        ", ".join(kinds) or "(none)",
        "'SCHEMA' absent means the pre-fix query could never work",
    )
    assert "SCHEMA" not in kinds, (
        "Redshift DOES expose SCHEMA rows in information_schema.usage_privileges,"
        " so the query replaced by the has_schema_privilege scan was salvageable."
    )


def test_aclexplode_is_absent(
    rs_cursor: ReadOnlyCursor, conformance: ConformanceLog
) -> None:
    """Why the PostgreSQL and Redshift schema-privilege paths diverge at all.

    If aclexplode turns out to exist here, the two paths could converge on the
    ACL-reading query, which reports a real grantor and grant option — strictly
    better than a privilege scan.
    """
    psycopg = pytest.importorskip("psycopg")
    try:
        rs_cursor.execute("SELECT aclexplode(ARRAY[]::aclitem[])")
        rs_cursor.fetchall()
        present = True
        detail = "aclexplode accepted an empty aclitem[]"
    except psycopg.Error as exc:
        rs_cursor.connection.rollback()
        present = False
        detail = str(exc).strip().splitlines()[0]

    conformance.record("aclexplode present?", "yes" if present else "no", detail)
    assert not present, (
        "aclexplode EXISTS on this cluster — the Redshift schema-privileges path "
        "could use the same ACL query as PostgreSQL and report grantor and grant "
        "option properly."
    )


# --- catalogs the other services depend on ----------------------------------


@pytest.mark.parametrize(
    ("relation", "used_by"),
    [
        ("svv_table_info", "dp db top-tables"),
        ("sys_query_history", "dp db long-queries"),
        ("stv_recents", "dp db long-queries (fallback shape)"),
        ("pg_namespace", "dp db describe"),
        ("information_schema.tables", "dp db dbt-orphans scan"),
    ],
)
def test_service_catalogs_are_readable(
    rs_cursor: ReadOnlyCursor, conformance: ConformanceLog, relation: str, used_by: str
) -> None:
    """Each service's entry point into the catalog, checked for existence.

    Recorded rather than asserted for the optional ones: a cluster may withhold
    a system view from a non-superuser, and that is a fact about the deployment,
    not a defect in dataplat.
    """
    readable, error = _relation_readable(rs_cursor, relation)
    conformance.record(
        f"is {relation} readable?", "yes" if readable else "no", f"{used_by}; {error}"
    )
    if not readable:
        pytest.skip(f"{relation} unreadable ({used_by}): {error}")


# --- the server itself ------------------------------------------------------


def test_server_identifies_itself(
    rs_cursor: ReadOnlyCursor, conformance: ConformanceLog
) -> None:
    """Recorded for the report: which engine and version answered."""
    version = _scalar(rs_cursor, "SELECT version()")
    conformance.record("server version()", str(version))
    assert version, "the server returned no version string"
    lowered = str(version).lower()
    conformance.record(
        "looks like Redshift?",
        "yes" if "redshift" in lowered else "NO - not a Redshift server",
        "version() should name Redshift",
    )


def test_read_only_transaction_is_honoured(
    rs_conn: Any, conformance: ConformanceLog
) -> None:
    """Whether the second safety layer actually engages on this engine.

    ``rs_conn`` has already been put into read-only mode during session setup;
    the harness recorded whether the server accepted and confirmed it. This test
    surfaces that as an assertion so a cluster that silently ignores READ ONLY is
    visible rather than merely logged — the client-side guard is then the only
    thing standing between a mistake and the data.
    """
    state = getattr(rs_conn, "_dp_read_only_state", None)
    if state is None:
        cursor = ReadOnlyCursor(rs_conn.cursor())
        readonly = _scalar(cursor, "SHOW transaction_read_only")
        conformance.record("transaction_read_only", str(readonly))
        assert str(readonly).lower() in {"on", "true"}, (
            "the server is not enforcing READ ONLY, so only the client-side "
            "guard protects this cluster"
        )
        return
    conformance.record("READ ONLY transaction", state.answer, state.detail)
    assert state.accepted, f"the server refused READ ONLY: {state.detail}"
