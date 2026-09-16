"""Which SQL constructs do not survive the move from Postgres to Redshift.

VENDORED. The tables below are a copy of ``betterdoc/mage``'s
``dbt_global_migration/scripts/scan_constructs.py``, which is where the
evidence lives: fifty-odd migration waves, each row measured on both engines
before it was written down. Copying it here buys `dp` a check it can run with
no second repository on disk, and costs a list that goes stale between syncs.

RE-SYNCING. Keep upstream's three-table shape and algorithm exactly, so a sync
is a diff of three literals rather than a re-reading of the evidence. Update
``SOURCE_SHA256`` and ``SOURCE_SYNCED`` in the same commit, and never edit a
message to "improve" it -- the wave citation is the thread back to the proof.

WHY IT IS NOT A DENYLIST. Upstream's own account: a hand-maintained list of
suspicious regexes "missed a construct in FOUR consecutive waves, each time
the same way -- the construct simply was not on the list". So this enumerates
every call in the SQL and subtracts the ones proven safe. What is left over is
UNKNOWN, which is a finding, not a pass. New constructs surface by default
instead of by luck.

WHAT THIS CANNOT SEE. The constructs that differ without erroring -- a bare
``::numeric`` that truncates, ``arr[1]`` reading a different element, a
``concat`` that propagates NULL. It flags the ones it knows; only comparing
output on both engines catches the rest, which is what ``--compare`` is for.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = [
    "Finding",
    "SOURCE_PATH",
    "SOURCE_REPO",
    "SOURCE_SHA256",
    "SOURCE_SYNCED",
    "scan",
]

SOURCE_REPO = "betterdoc/mage"
SOURCE_PATH = "dbt_global_migration/scripts/scan_constructs.py"
SOURCE_SHA256 = "457283b593fb50d5e8678a3b6d72ad6e0d71df58c2df59b31c43e49e561b4cbf"
SOURCE_SYNCED = "2026-09-16"


@dataclass(frozen=True)
class Finding:
    """One construct worth reading before this SQL is trusted on Redshift."""

    kind: str  # "RISKY" | "UNKNOWN" | "SYNTAX"
    construct: str
    message: str


# Proven identical on both engines, or otherwise not a porting concern.
# ONLY add after measuring on both engines, and cite the wave in the comment.
# Upstream's dbt/Jinja names are dropped: this scans Superset SQL, where `ref`
# and `source` are ordinary column names rather than macros.
KNOWN_SAFE = {
    "select",
    "from",
    "where",
    "and",
    "or",
    "not",
    "in",
    "as",
    "on",
    "join",
    "left",
    "right",
    "inner",
    "full",
    "outer",
    "group",
    "order",
    "by",
    "having",
    "union",
    "all",
    "distinct",
    "case",
    "when",
    "then",
    "else",
    "end",
    "with",
    "over",
    "partition",
    "between",
    "is",
    "null",
    "asc",
    "desc",
    "limit",
    "cast",
    "exists",
    "using",
    "cross",
    "lateral",
    "count",
    "sum",
    "min",
    "max",
    "round",
    "abs",
    "coalesce",
    "nullif",
    "greatest",
    "least",
    "lower",
    "upper",
    "trim",
    "ltrim",
    "rtrim",
    "length",
    "substring",
    "substr",
    "replace",
    "position",
    "strpos",
    "md5",
    "floor",
    "ceil",
    "ceiling",
    "mod",
    "power",
    "row_number",
    "rank",
    "dense_rank",
    "lag",
    "lead",
    "first_value",
    "last_value",
    "ntile",
    "date_trunc",
    "extract",
    "to_char",
    "to_date",
    "current_date",
    "now",
    "dateadd",
    "datediff",
    "octet_length",
    "split_part",
    "regexp_substr",
    "regexp_replace",
    "initcap",
    # verified identical on both engines 2026-08-26 (wave 44 follow-up)
    "chr",
    "date_part",
    "date",
}

# Constructs measured to DIFFER or to need care. Message explains the fix.
KNOWN_RISKY = {
    "concat": "TWO problems. (1) ARITY: Redshift's concat() takes exactly TWO "
    "arguments -- a 5-arg call raises 'function concat(...) does not "
    "exist'. Rewrite as ||. (2) NULLS: Postgres concat() IGNORES NULL "
    "args but || PROPAGATES them, so the rewrite silently collapses a "
    "whole expression to NULL unless every non-literal arg is wrapped: "
    "coalesce(col, ''). (waves 44, 48)",
    "avg": "avg(integer) returns numeric on Postgres but TRUNCATES to integer on "
    "Redshift. Cast inside: avg(col::numeric(38,10)). A bare ::numeric is "
    "numeric(18,0) on Redshift and truncates again. (wave 30)",
    "concat_ws": "DOES NOT EXIST ON REDSHIFT AT ALL. Do NOT rewrite as || with "
    "coalesce -- concat_ws SKIPS NULL args, so coalescing to '' "
    "leaves the separator behind: concat_ws('; ', NULL, 'b') is 'b', "
    "not '; b'. (wave 51)",
    "string_agg": "Not on Redshift. Redshift's LISTAGG has DISTINCT/ORDER BY "
    "restrictions and cannot share a SELECT with another DISTINCT "
    "aggregate.",
    "array_agg": "Postgres array_agg over all-NULL yields {}; Redshift listagg "
    "yields NULL, and NULL is not [] downstream. (wave 35)",
    "array_to_string": "Redshift refuses to subscript anything but a plain "
    "column: 'applying array subscript on complex expression "
    "of SUPER type is currently not supported'. (wave 31)",
    "unnest": "Not on Redshift. PartiQL unnesting is an INNER join -- it DROPS "
    "empty arrays, changing row counts rather than values.",
    "generate_series": "Leader-node-only on Redshift. Usually a calendar join.",
    "array_remove": "Postgres-only.",
    "jsonb_agg": "Postgres-only.",
    "percentile_cont": "Redshift needs WITHIN GROUP and has restrictions.",
    "listagg": "Redshift-only; DISTINCT/ORDER BY restrictions, emits VARCHAR(MAX).",
    "make_date": "POSTGRES-ONLY (verified 2026-08-26). Build the date as a string "
    "and cast, or use to_date with an explicit format.",
    "array_length": "POSTGRES-ONLY (verified 2026-08-26). Redshift's SUPER "
    "equivalent is get_array_length(). Often the array can be "
    "avoided entirely. (wave 36)",
    "string_to_table": "POSTGRES-ONLY, PG14+. No direct equivalent. (2026-08-26)",
}

# Operator / syntax level, which a function-name scan cannot see.
SYNTAX_CHECKS: list[tuple[str, str, str]] = [
    (
        r"\bwindow\s+[a-z_][a-z_0-9]*\s+as\s*\(",
        "named WINDOW clause",
        "POSTGRES-ONLY. Redshift has no named window definitions at all; inline "
        "the whole window at every OVER <name> site. A frame hidden in a WINDOW "
        "clause also defeats a frameless-window scan. (wave 53)",
    ),
    (
        r"::\s*numeric(?!\s*\()",
        "bare ::numeric cast",
        "On Redshift a bare ::numeric is DECIMAL(18,0) and TRUNCATES. It builds "
        "fine and every test passes while silently rounding: 0.714 became 0.0 "
        "across ~100%% of rows. There is NO error to grep for -- only comparing "
        "output catches it. Pin the scale on BOTH engines, e.g. ::numeric(38,8). "
        "Do NOT apply blanket: widening an INTEGER-valued cast broke "
        "to_date(year || '0104', 'YYYYMMDD'). (wave 55)",
    ),
    (
        r"\bdistinct\s+on\b",
        "DISTINCT ON",
        "Postgres-only. Rewrite as row_number() with a TOTAL order.",
    ),
    (
        r"\bfilter\s*\(\s*where",
        "FILTER (WHERE)",
        "Postgres-only. Rewrite as agg(CASE WHEN cond THEN x END).",
    ),
    (
        r"(@>|<@)",
        "array containment",
        "No Redshift equivalent: 'operator does not exist: super @> text[]'. (wave 36)",
    ),
    (
        r"&&",
        "array overlap",
        "No Redshift equivalent. OR the containment calls. (wave 36)",
    ),
    (r"(->>|#>>|->)", "json operator", "Postgres-only json navigation."),
    (r"\bjsonb\b", "jsonb", "Postgres-only type; 'type \"jsonb\" does not exist'."),
    (
        r"\brange\s+between\b.*\b(preceding|following)\b",
        "RANGE frame with an offset",
        "Redshift allows NO value offset in a RANGE frame. Rewrite as a self-join "
        "on an integer index; ROWS is NOT equivalent when the series has gaps. "
        "(wave 42)",
    ),
    (
        r"\binterval\s*'[^']*\b(month|year)",
        "interval with month/year",
        "A LITERAL is fine; against a COLUMN it fails on Redshift. Read the site. "
        "(wave 40)",
    ),
    (
        r"::\s*(text|varchar)\b",
        "::text / ::varchar cast",
        "Fails silently on SUPER columns (returns NULL). Also renders dates "
        "differently per engine -- use to_char with an explicit format inside "
        "keys or hashes. (waves 33, 34)",
    ),
    (
        r"[^A-Za-z0-9_]E'",
        "E'' escape-string literal",
        'Redshift: type "e" does not exist.',
    ),
    (
        r"\(\?",
        "regex lookahead/lookbehind or non-capturing group",
        "POSIX ERE has none of them; Redshift rejects '(?:' outright. (wave 27)",
    ),
    (
        r"'[^']*\\[sdw]",
        "Perl regex class",
        "Redshift's POSIX ERE has no \\s, \\d or \\w and FAILS SILENTLY -- it "
        "matches nothing rather than erroring.",
    ),
]

ROWNUM_MESSAGE = (
    "Runs on Redshift but is NOT a total order -- the pick is arbitrary and "
    "plan-dependent. Add a tie-break if the choice is visible downstream. "
    "(waves 32, 43)"
)


def _jinja_spans(sql: str) -> list[tuple[int, int]]:
    """Character ranges covered by ``{{ ... }}`` and ``{% ... %}``."""
    return [
        (m.start(), m.end()) for m in re.finditer(r"\{\{.*?\}\}|\{%.*?%\}", sql, re.S)
    ]


def _only_called_through_jinja(name: str, sql: str) -> bool:
    """Whether every call of ``name(`` sits inside a Jinja expression.

    Superset templates virtual datasets when ENABLE_TEMPLATE_PROCESSING is on,
    so ``{{ current_username() }}`` and ``{{ filter_values(...) }}`` appear in
    dataset SQL and never reach either engine. Conservative on purpose: one
    bare-SQL call site anywhere and the name is still flagged.
    """
    spans = _jinja_spans(sql)
    sites = list(re.finditer(r"\b" + re.escape(name) + r"\s*\(", sql, re.I))
    if not sites:
        return False
    return all(any(a <= m.start() < b for a, b in spans) for m in sites)


def _rownumber_without_order(sql: str) -> bool:
    r"""Whether any ``row_number() OVER (...)`` lacks an ORDER BY.

    Balanced parentheses rather than a regex: ``[^)]*`` stops at the FIRST
    closing paren, so a window whose ORDER BY contains a function call --
    ``OVER (PARTITION BY r ORDER BY c DESC, md5(n))`` -- had the pattern
    consume up to md5's own paren and report a violation on a window that has
    one.
    """
    low = sql.lower()
    for match in re.finditer(r"row_number\s*\(\s*\)\s*over\s*\(", low):
        open_at = match.end() - 1
        depth = 0
        close_at = None
        for index in range(open_at, len(low)):
            if low[index] == "(":
                depth += 1
            elif low[index] == ")":
                depth -= 1
                if depth == 0:
                    close_at = index
                    break
        if close_at is None:
            continue  # unbalanced; let the engine complain, not us
        if "order by" not in low[open_at + 1 : close_at]:
            return True
    return False


def _strip(sql: str) -> str:
    """Remove what must not be scanned, in the one order that works.

    Jinja comments FIRST: a ``{# ... #}`` block may legitimately contain ``--``,
    and stripping line comments first eats the block's closing tag, leaving the
    whole comment in the scanned text. Then string literals, before looking for
    calls: German label text like ``'Verdauungssystem (Magen)'`` is otherwise
    read as a call to ``verdauungssystem()``.
    """
    sql = re.sub(r"\{#.*?#\}", "", sql, flags=re.S)
    sql = re.sub(r"/\*.*?\*/", "", sql, flags=re.S)
    sql = re.sub(r"--[^\n]*", "", sql)
    return re.sub(r"'(?:[^']|'')*'", "''", sql)


def scan(sql: str) -> tuple[Finding, ...]:
    """Every construct in ``sql`` that is risky, unknown, or bad syntax on Redshift."""
    if not sql or not sql.strip():
        return ()

    stripped = _strip(sql)

    # A token preceded by '::' is a CAST, not a call -- ``::numeric(38, 6)`` is
    # the standard fix for the bare-::numeric trap, and reading it as a call to
    # numeric() reported a phantom UNKNOWN on every model that uses one.
    calls = {
        m.group(1).lower()
        for m in re.finditer(r"\b([a-zA-Z_][a-zA-Z0-9_]*)\s*\(", stripped)
        if not stripped[: m.start()].rstrip().endswith("::")
    }
    # FILTER (WHERE ...) and RANGE BETWEEN are syntax, not calls -- the '(' that
    # follows makes them look like functions. Both are in SYNTAX_CHECKS, so drop
    # them here rather than reporting them twice.
    calls -= {"filter", "range"}

    findings: list[Finding] = []
    for call in sorted(calls):
        if _only_called_through_jinja(call, stripped):
            continue
        if call in KNOWN_RISKY:
            findings.append(Finding("RISKY", call, KNOWN_RISKY[call]))
        elif call not in KNOWN_SAFE:
            findings.append(
                Finding(
                    "UNKNOWN",
                    call,
                    "Not on the proven-safe list. VERIFY ON BOTH ENGINES before "
                    "porting, then add it upstream with the wave.",
                )
            )

    for pattern, label, message in SYNTAX_CHECKS:
        if re.search(pattern, stripped, re.I):
            findings.append(Finding("SYNTAX", label, message))

    if _rownumber_without_order(stripped):
        findings.append(
            Finding("SYNTAX", "row_number() with no ORDER BY", ROWNUM_MESSAGE)
        )

    return tuple(findings)
