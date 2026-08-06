"""Pure helpers inside the Airbyte client: auth headers, JWT expiry, cron.

These need no HTTP at all, which makes them the part of ``client.py`` a test can
speak about with real authority — unlike the request paths around them, where a
fake proves only that our code calls what we told it to.

``build_auth_headers`` earns the attention: it decides what credential goes on
every request, and it is configured by four environment variables that had no
test between them.
"""

from __future__ import annotations

import base64
import json
import time

import pytest

from dataplat.services.airbyte.client import (
    build_auth_headers,
    parse_jwt_exp,
    split_cron_timezone,
    validate_cron_expression,
)

_AUTH_VARS = (
    "AIRBYTE_AUTH_HEADER",
    "AIRBYTE_AUTH_VALUE",
    "AIRBYTE_AUTH_SCHEME",
    "AIRBYTE_AUTH_COOKIE",
)


@pytest.fixture(autouse=True)
def _clear_auth_env(monkeypatch):
    """These read os.getenv directly, so a developer's own shell would leak in."""
    for name in _AUTH_VARS:
        monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------------------
# build_auth_headers
# ---------------------------------------------------------------------------


def test_the_default_is_a_bearer_authorization_header() -> None:
    headers = build_auth_headers("tok")

    assert headers["Authorization"] == "Bearer tok"
    assert headers["Accept"] == "application/json"
    assert headers["Content-Type"] == "application/json"


def test_a_custom_scheme_replaces_bearer(monkeypatch) -> None:
    """Some deployments sit behind a proxy expecting `Token <x>`."""
    monkeypatch.setenv("AIRBYTE_AUTH_SCHEME", "Token")

    assert build_auth_headers("tok")["Authorization"] == "Token tok"


def test_an_empty_scheme_sends_the_bare_token(monkeypatch) -> None:
    """`AIRBYTE_AUTH_SCHEME=` is how you ask for no prefix at all — a header
    reading `Bearer ` with an empty scheme would be a different credential."""
    monkeypatch.setenv("AIRBYTE_AUTH_SCHEME", "")

    assert build_auth_headers("tok")["Authorization"] == "tok"


def test_an_explicit_value_wins_over_the_token_entirely(monkeypatch) -> None:
    """AIRBYTE_AUTH_VALUE is the escape hatch for an auth scheme this code does
    not model. The token it was handed is then deliberately unused."""
    monkeypatch.setenv("AIRBYTE_AUTH_VALUE", "Basic abc123")

    headers = build_auth_headers("tok")

    assert headers["Authorization"] == "Basic abc123"
    assert "tok" not in headers["Authorization"]


def test_an_empty_explicit_value_is_honoured_not_ignored(monkeypatch) -> None:
    """`is not None`, not truthiness: setting it empty means "send nothing",
    and falling back to `Bearer <token>` would send a credential the operator
    explicitly cleared."""
    monkeypatch.setenv("AIRBYTE_AUTH_VALUE", "")

    assert build_auth_headers("tok")["Authorization"] == ""


def test_the_header_name_is_configurable(monkeypatch) -> None:
    monkeypatch.setenv("AIRBYTE_AUTH_HEADER", "X-Api-Key")

    headers = build_auth_headers("tok")

    assert headers["X-Api-Key"] == "Bearer tok"
    assert "Authorization" not in headers


def test_a_cookie_is_added_only_when_set(monkeypatch) -> None:
    assert "Cookie" not in build_auth_headers("tok")

    monkeypatch.setenv("AIRBYTE_AUTH_COOKIE", "session=abc")

    assert build_auth_headers("tok")["Cookie"] == "session=abc"


# ---------------------------------------------------------------------------
# parse_jwt_exp
# ---------------------------------------------------------------------------


def _jwt(payload: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"header.{body}.signature"


def test_the_expiry_is_read_from_the_payload() -> None:
    expires = int(time.time()) + 3600

    assert parse_jwt_exp(_jwt({"exp": expires})) == expires


def test_padding_is_restored_before_decoding() -> None:
    """base64url in a JWT is unpadded; decoding without re-padding raises.

    Exercised across several payload lengths so at least one needs each of the
    three possible padding counts.
    """
    for size in range(1, 12):
        token = _jwt({"exp": 1700000000, "sub": "x" * size})
        assert parse_jwt_exp(token) == 1700000000


def test_a_string_expiry_is_coerced() -> None:
    assert parse_jwt_exp(_jwt({"exp": "1700000000"})) == 1700000000


@pytest.mark.parametrize(
    "token",
    [
        "",
        "not-a-jwt",
        "onlyonepart",
        "header.!!!not-base64!!!.sig",
        "header.eyJub3RfanNvbg.sig",
    ],
)
def test_an_unreadable_token_is_none_rather_than_an_exception(token: str) -> None:
    """Best effort: the caller uses this to decide whether to refresh early, and
    an unparseable token simply means "cannot tell", not a crash on every call.
    """
    assert parse_jwt_exp(token) is None


def test_a_payload_without_exp_is_none() -> None:
    assert parse_jwt_exp(_jwt({"sub": "user"})) is None


def test_a_non_numeric_exp_is_none() -> None:
    assert parse_jwt_exp(_jwt({"exp": "soon"})) is None


# ---------------------------------------------------------------------------
# cron
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "expr",
    [
        "0 0 * * *",  # 5 fields, standard
        "0 0 0 * * *",  # 6, Quartz with seconds
        "0 0 0 * * ? *",  # 7, Quartz with year
        "0 0 12 ? * MON",  # `?` for "no specific value"
    ],
)
def test_valid_expressions_are_accepted(expr: str) -> None:
    assert validate_cron_expression(expr) is True


@pytest.mark.parametrize(
    "expr",
    ["", "   ", "not a cron", "0 0", "99 99 99 99 99", "0 0 * * * * * * *"],
)
def test_invalid_expressions_are_rejected(expr: str) -> None:
    assert validate_cron_expression(expr) is False


def test_a_quartz_timezone_suffix_is_accepted() -> None:
    """Airbyte accepts a trailing IANA zone that croniter would choke on."""
    assert validate_cron_expression("0 0 0 * * ? * Europe/Berlin") is True


def test_a_timezone_with_an_underscore_is_accepted() -> None:
    assert validate_cron_expression("0 0 0 * * ? * America/New_York") is True


def test_a_trailing_field_that_is_not_a_timezone_is_not_stripped() -> None:
    """Stripping it blindly would let an 8-field expression validate by losing a
    field, so the suffix has to actually resolve as a zone."""
    assert validate_cron_expression("0 0 0 * * ? * Not/AZone") is False


def test_the_timezone_is_split_off_for_the_api() -> None:
    expr, tz = split_cron_timezone("0 0 0 * * ? * Europe/Berlin")

    assert expr == "0 0 0 * * ? *"
    assert tz == "Europe/Berlin"


def test_an_expression_without_a_zone_is_returned_unchanged() -> None:
    assert split_cron_timezone("0 0 * * *") == ("0 0 * * *", None)


def test_an_unresolvable_suffix_is_left_on_the_expression() -> None:
    """Better to hand the API something it will reject with a clear message than
    to silently drop a field the operator typed."""
    assert split_cron_timezone("0 0 0 * * ? * Not/AZone") == (
        "0 0 0 * * ? * Not/AZone",
        None,
    )


def test_splitting_is_the_inverse_of_what_validation_accepts() -> None:
    """The two read the trailing field with the same rule; if they disagreed, an
    expression could validate and then be sent with its zone still attached."""
    full = "0 0 0 * * ? * Europe/Berlin"
    expr, tz = split_cron_timezone(full)

    assert validate_cron_expression(full) is True
    assert tz is not None
    assert validate_cron_expression(expr) is True
