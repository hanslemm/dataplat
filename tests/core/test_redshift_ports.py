"""The vendored Redshift portability scan.

Every test here that looks like a non-event -- a comment, a German label, a
scaled cast -- is a false positive the upstream scanner hit in production and
fixed. They are pinned so a re-sync cannot quietly undo the fix.
"""

from __future__ import annotations

from dataplat.core.redshift_ports import SOURCE_SHA256, scan


def _kinds(sql: str) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {"RISKY": set(), "UNKNOWN": set(), "SYNTAX": set()}
    for finding in scan(sql):
        out[finding.kind].add(finding.construct)
    return out


def test_plain_sql_is_clean() -> None:
    assert scan("select id, count(*) from orders group by id") == ()


def test_a_risky_call_is_flagged_with_its_wave() -> None:
    found = scan("select concat('a: ', b) from t")
    risky = [f for f in found if f.kind == "RISKY" and f.construct == "concat"]
    assert risky, found
    # The wave citation is the thread back to the evidence; a message without
    # it is an assertion instead of a finding.
    assert "wave" in risky[0].message.lower()


def test_an_unknown_call_is_reported_rather_than_assumed_safe() -> None:
    assert "pg_sleep" in _kinds("select pg_sleep(1)")["UNKNOWN"]


def test_distinct_on_is_a_syntax_finding() -> None:
    assert "DISTINCT ON" in _kinds("select distinct on (id) id, x from t")["SYNTAX"]


def test_bare_numeric_cast_is_flagged_but_a_scaled_one_is_not() -> None:
    assert "bare ::numeric cast" in _kinds("select x::numeric from t")["SYNTAX"]
    assert (
        "bare ::numeric cast" not in _kinds("select x::numeric(38, 8) from t")["SYNTAX"]
    )


def test_a_scaled_cast_is_not_read_as_a_call() -> None:
    # `::numeric(38, 6)` is a cast. Reading it as numeric() reported a phantom
    # UNKNOWN on every model using the standard fix (upstream wave 58).
    assert "numeric" not in _kinds("select x::numeric(38, 6) from t")["UNKNOWN"]


def test_a_jinja_comment_containing_a_dash_dash_does_not_swallow_the_query() -> None:
    # Stripping line comments BEFORE Jinja comments eats the block's closing
    # tag, leaving the comment body in the scanned text -- where `pg_sleep(1)`
    # then reads as a real call. The token must sit inside the comment and on
    # the same line as the `--`, or the ordering cannot be discriminated at all.
    sql = "{# note: pg_sleep(1) -- see ticket #}\nselect 1 from t"
    assert _kinds(sql)["UNKNOWN"] == set()


def test_a_string_literal_is_not_read_as_a_call() -> None:
    # 'Verdauungssystem (Magen)' was read as a call to verdauungssystem().
    assert _kinds("select 'Verdauungssystem (Magen)' as label")["UNKNOWN"] == set()


def test_row_number_with_a_function_in_its_order_by_is_not_a_false_positive() -> None:
    # The regex form stops at md5's own paren and reports a total-order
    # violation on a window that has one.
    sql = "select row_number() over (partition by r order by c desc, md5(n)) from t"
    assert "row_number() with no ORDER BY" not in _kinds(sql)["SYNTAX"]


def test_a_function_inside_partition_by_is_not_a_false_positive() -> None:
    # The truncating-regex bug needs the nested call BEFORE the ORDER BY:
    # `[^)]*` stops at md5's own paren, the span has no "order by" left in it,
    # and a window that HAS an ORDER BY gets flagged as having none.
    sql = "select row_number() over (partition by md5(r) order by c) from t"
    assert "row_number() with no ORDER BY" not in _kinds(sql)["SYNTAX"]


def test_row_number_without_an_order_by_is_flagged() -> None:
    sql = "select row_number() over (partition by r) from t"
    assert "row_number() with no ORDER BY" in _kinds(sql)["SYNTAX"]


def test_supersets_own_jinja_is_not_a_finding() -> None:
    # With ENABLE_TEMPLATE_PROCESSING on, a virtual dataset may template.
    # These never reach either engine.
    #
    # Deliberately UNQUOTED: inside a string literal the stripping pass would
    # erase the call before the Jinja check ever ran, and the test would pass
    # without exercising _only_called_through_jinja at all.
    sql = "select * from t where id in ({{ filter_values('id')|join(',') }})"
    assert _kinds(sql)["UNKNOWN"] == set()


def test_provenance_is_recorded() -> None:
    # "Where did this come from, and what have I not picked up" must be
    # answerable without asking anyone.
    assert len(SOURCE_SHA256) == 64
