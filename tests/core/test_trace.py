"""The trace seam: silent by default, stderr when asked, never a credential.

The last group is why this module exists at all. Ad-hoc `print(sql)` calls
scattered through the areas would each have to remember that role creation
sends a literal password and that an Airbyte client sends a bearer token. One
redactor, tested here, is the alternative.
"""

from __future__ import annotations

import importlib
from collections.abc import Iterator

import pytest

from dataplat.core import trace as trace_mod
from dataplat.core.trace import (
    CATEGORY_HTTP,
    CATEGORY_SQL,
    VERBOSE_ENV_VAR,
    disable,
    enable,
    is_enabled,
    redact,
    trace,
    trace_http,
    trace_sql,
    verbose,
)


@pytest.fixture(autouse=True)
def _restore_switch() -> Iterator[None]:
    """Tracing is process-wide, so no test may leak its setting into the next."""
    previous = is_enabled()
    try:
        yield
    finally:
        trace_mod._set(previous)


# --- the switch -------------------------------------------------------------


def test_disabled_by_default_in_the_suite() -> None:
    """The suite runs without DP_VERBOSE, so nothing should be tracing."""
    assert is_enabled() is False


def test_silent_when_disabled(capsys: pytest.CaptureFixture[str]) -> None:
    disable()
    trace("sql", "SELECT 1")
    trace_sql("SELECT 1", params=(1,))
    trace_http("GET", "https://example.test/x", status=200, elapsed_ms=1.0)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_enable_is_idempotent(capsys: pytest.CaptureFixture[str]) -> None:
    enable()
    enable()
    assert is_enabled() is True
    trace(CATEGORY_SQL, "SELECT 1")
    assert capsys.readouterr().err.count("[dp:sql]") == 1


def test_disable_silences_again(capsys: pytest.CaptureFixture[str]) -> None:
    enable()
    disable()
    trace(CATEGORY_SQL, "SELECT 1")
    assert capsys.readouterr().err == ""


def test_verbose_restores_the_previous_setting(
    capsys: pytest.CaptureFixture[str],
) -> None:
    disable()
    with verbose():
        assert is_enabled() is True
        trace(CATEGORY_SQL, "SELECT 1")
    assert is_enabled() is False
    assert "[dp:sql]" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("1", True), ("true", True), ("ture", True), ("0", False), ("", False)],
)
def test_env_var_read_at_import(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: bool
) -> None:
    """DP_VERBOSE is read once, when the module is imported.

    Reload is the only honest way to test that, and the module is put back
    afterwards so the rest of the suite sees a tracer that is off.
    """
    monkeypatch.setenv(VERBOSE_ENV_VAR, raw)
    reloaded = importlib.reload(trace_mod)
    try:
        assert reloaded.is_enabled() is expected
    finally:
        monkeypatch.delenv(VERBOSE_ENV_VAR, raising=False)
        importlib.reload(trace_mod)


def test_env_var_absent_means_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(VERBOSE_ENV_VAR, raising=False)
    assert importlib.reload(trace_mod).is_enabled() is False


# --- where it writes --------------------------------------------------------


def test_writes_to_stderr_only(capsys: pytest.CaptureFixture[str]) -> None:
    """--json and --format csv stay machine-readable; that is the whole rule."""
    with verbose():
        trace_sql("SELECT 1")
        trace_http("GET", "https://example.test/x", status=200)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "[dp:sql]" in captured.err
    assert "[dp:http]" in captured.err


def test_categories_are_prefixed(capsys: pytest.CaptureFixture[str]) -> None:
    with verbose():
        trace("aws", "assume-role profile=x")
    assert capsys.readouterr().err.startswith("[dp:aws] ")


def test_one_trace_is_one_line(capsys: pytest.CaptureFixture[str]) -> None:
    """A second line without the prefix is invisible to the grep it exists for."""
    with verbose():
        trace_sql("SELECT a,\n       b\nFROM t\nWHERE a = 1")
    err = capsys.readouterr().err
    assert err.count("\n") == 1
    assert "SELECT a, b FROM t WHERE a = 1" in err


# --- SQL --------------------------------------------------------------------


def test_sql_reports_the_statement_and_that_params_were_bound(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with verbose():
        trace_sql("SELECT * FROM t WHERE id = %s", params=(42,))
    err = capsys.readouterr().err
    assert "SELECT * FROM t WHERE id = %s" in err
    assert "1 params bound" in err


def test_sql_never_prints_parameter_values(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Parameter values are warehouse data -- names, emails, secrets.

    psycopg keeps them out of the statement; the tracer must not put them back.
    """
    with verbose():
        trace_sql(
            "SELECT * FROM users WHERE email = %s",
            params=("someone@example.com", "s3cr3t"),
        )
    err = capsys.readouterr().err
    assert "someone@example.com" not in err
    assert "s3cr3t" not in err
    assert "2 params bound" in err


def test_sql_says_when_nothing_was_bound(capsys: pytest.CaptureFixture[str]) -> None:
    with verbose():
        trace_sql("SELECT 1")
    assert "no params" in capsys.readouterr().err


def test_sql_handles_a_mapping_of_params(capsys: pytest.CaptureFixture[str]) -> None:
    with verbose():
        trace_sql("SELECT %(a)s", params={"a": 1, "b": 2})
    err = capsys.readouterr().err
    assert "2 params bound" in err
    assert "'a'" not in err


def test_sql_does_not_consume_an_unsized_params_object(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A generator must survive being traced, so it is never iterated."""
    params = (n for n in (1, 2, 3))
    with verbose():
        trace_sql("SELECT %s", params=params)
    assert "params bound" in capsys.readouterr().err
    assert list(params) == [1, 2, 3]


def test_sql_includes_target_and_elapsed(capsys: pytest.CaptureFixture[str]) -> None:
    with verbose():
        trace_sql("SELECT 1", target="demo_pg", elapsed_ms=12.34)
    err = capsys.readouterr().err
    assert "demo_pg" in err
    assert "12.3ms" in err


# --- HTTP -------------------------------------------------------------------


def test_http_reports_method_url_status_and_elapsed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with verbose():
        trace_http(
            "get", "https://api.example.test/v1/jobs", status=404, elapsed_ms=7.5
        )
    err = capsys.readouterr().err
    assert f"[dp:{CATEGORY_HTTP}] GET https://api.example.test/v1/jobs" in err
    assert "-> 404" in err
    assert "7.5ms" in err


def test_http_before_the_response(capsys: pytest.CaptureFixture[str]) -> None:
    """The hanging-request case: no status yet, and no fake one either."""
    with verbose():
        trace_http("POST", "https://api.example.test/v1/jobs")
    err = capsys.readouterr().err.strip()
    assert err == "[dp:http] POST https://api.example.test/v1/jobs"


# --- redaction: the reason this is one module and not many prints -----------


DSN = "postgresql://admin:s3cr3t@warehouse.example.test:5439/dev?sslmode=require"


def test_traced_dsn_password_is_redacted(capsys: pytest.CaptureFixture[str]) -> None:
    with verbose():
        trace("db", f"connecting to {DSN}")
    err = capsys.readouterr().err
    assert "s3cr3t" not in err
    assert "admin:***@warehouse.example.test:5439" in err


def test_traced_conninfo_password_is_redacted(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with verbose():
        trace("db", "user=admin password=s3cr3t host=db.example.test port=5439")
    err = capsys.readouterr().err
    assert "s3cr3t" not in err
    assert "password=***" in err
    assert "user=admin" in err  # everything else still legible


def test_traced_sql_password_literal_is_redacted(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The case that made a shared redactor mandatory.

    services/db/role_dialects.py sends `CREATE ROLE x LOGIN PASSWORD '<literal>'`
    to the server, so tracing "what did we send" traces a live credential unless
    the SQL form -- no `=`, just a quoted literal -- is handled.
    """
    with verbose():
        trace_sql("CREATE ROLE demo LOGIN PASSWORD 'p4ssw0rd' NOSUPERUSER")
    err = capsys.readouterr().err
    assert "p4ssw0rd" not in err
    assert "CREATE ROLE demo LOGIN PASSWORD *** NOSUPERUSER" in err


def test_traced_secret_value_is_redacted(capsys: pytest.CaptureFixture[str]) -> None:
    with verbose():
        trace("aws", 'response: {"client_secret": "sh-abc123", "client_id": "public"}')
    err = capsys.readouterr().err
    assert "sh-abc123" not in err
    assert "public" in err  # the non-secret field is still there


def test_traced_bearer_token_is_redacted(capsys: pytest.CaptureFixture[str]) -> None:
    with verbose():
        trace_http("GET", "https://api.example.test/v1/jobs")
        trace("http", "headers: {'Authorization': 'Bearer eyJhbGciOi.s3cr3t'}")
    err = capsys.readouterr().err
    assert "eyJhbGciOi.s3cr3t" not in err
    assert "Bearer ***" in err


def test_traced_api_key_in_a_url_is_redacted(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with verbose():
        trace_http("GET", "https://api.example.test/v1/jobs?api_key=k3y&limit=10")
    err = capsys.readouterr().err
    assert "k3y" not in err
    assert "api_key=***&limit=10" in err


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        (DSN, "s3cr3t"),
        ("PGPASSWORD=hunter2", "hunter2"),
        ("sslpassword='hunter 2' sslmode=require", "hunter 2"),
        ("CREATE USER demo PASSWORD 'p4ssw0rd'", "p4ssw0rd"),
        ('{"password": "p4ssw0rd"}', "p4ssw0rd"),
        ("AIRBYTE_CLIENT_SECRET=sh-abc123", "sh-abc123"),
        ("access_token=at-abc123", "at-abc123"),
        ("x-api-key: ak-abc123", "ak-abc123"),
        ("Authorization: Bearer eyJ.abc", "eyJ.abc"),
        ("Authorization: token ghp_abc123", "ghp_abc123"),
        ("credentials=cr-abc123", "cr-abc123"),
    ],
)
def test_redact_removes_every_credential_spelling(text: str, secret: str) -> None:
    redacted = redact(text)
    assert secret not in redacted
    assert "***" in redacted


@pytest.mark.parametrize(
    "text",
    [
        "SELECT 1",
        "Password set: unknown",  # the report wording, not a value
        "Set AIRBYTE_BASE_URL, AIRBYTE_EMAIL, AIRBYTE_PASSWORD",  # a hint
        "postgresql://admin@warehouse.example.test:5439/dev",  # no password
        "http://localhost:8000/api/v1/security/login",  # port is not a password
        "role demo is passwordless",
    ],
)
def test_redact_leaves_credential_free_text_alone(text: str) -> None:
    """Over-masking is the safe direction, but not at the cost of every message."""
    assert redact(text) == text


def test_redaction_is_applied_by_trace_itself(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No caller can opt out: the write path is the redaction path.

    Asserted separately from redact() because a future refactor could keep the
    function and lose the call.
    """
    with verbose():
        trace(CATEGORY_SQL, "password=s3cr3t")
    assert "s3cr3t" not in capsys.readouterr().err


def test_http_status_survives_a_url_path_ending_in_token(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The redactor masks the word after a bare `token`; the separator must stop it.

    Airbyte's token endpoint is /api/public/v1/applications/token, so joining the
    pieces with a space made `token -> 200` look like `Authorization: token …`
    and the status was replaced by ***. Over-masking fails safe, but it hid
    information from a line that leaked nothing.
    """
    with verbose():
        trace_http(
            "POST",
            "https://example.test/api/public/v1/applications/token",
            status=200,
            elapsed_ms=0.1,
        )
    err = capsys.readouterr().err
    assert "-> 200" in err
    assert "***" not in err


def test_a_real_bearer_token_is_still_redacted() -> None:
    """Proof the separator change did not loosen the pattern."""
    assert "ghp_abcdefghij" not in redact("Authorization: token ghp_abcdefghij")
    assert "eyJhbGciOiJI" not in redact("Authorization: Bearer eyJhbGciOiJI")
