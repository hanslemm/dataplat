"""Escaping for SQL ``LIKE`` patterns.

``_`` and ``%`` are wildcards inside a ``LIKE`` pattern, so any value
interpolated into one — a schema prefix, a suffix such as ``_deprecated``, a
project name — matches more than it reads as. That is easy to miss because the
over-matching pattern still returns the rows you expected, plus others.

This lived in ``top_tables`` while ``orphans`` went without, which is precisely
how ``LIKE '%_deprecated'`` came to treat its underscore as "any character" and
report unrelated relations as purge candidates. One home, so the next module to
build a pattern has somewhere obvious to reach for.

The escape character is ``#``, not the SQL-standard backslash, and that is
load-bearing rather than taste. Redshift runs with
``standard_conforming_strings`` **off**, so a backslash inside a string literal
escapes whatever follows it — including the closing quote of ``ESCAPE '\\'``,
which leaves the literal unterminated and the whole statement a syntax error.
PostgreSQL has the setting on and parses the same text happily, which is exactly
why this hid: every test passed, and ``dp db top-tables`` and
``dp db dbt-orphans`` failed on Redshift only. Reproduced on PostgreSQL 16 by
starting a session with the Redshift setting::

    PGOPTIONS='-c standard_conforming_strings=off' psql ...
    SELECT 'ok' WHERE 'dev_x' LIKE 'dev\\_%' ESCAPE '\\';
    ERROR:  unterminated quoted string at or near "'\\'"

``#`` has no special meaning to either engine's string-literal parser, so it
behaves identically on both. Do not "simplify" this back to a backslash.
"""

from __future__ import annotations

__all__ = ["LIKE_ESCAPE", "LIKE_ESCAPE_CLAUSE", "glob_to_like", "like_escape"]

# The escape character itself, exposed so tests can assert on one constant
# rather than a literal repeated across modules.
LIKE_ESCAPE = "#"

# Pair every pattern built with :func:`like_escape` with this clause: the
# escape character is only special when the statement declares it.
LIKE_ESCAPE_CLAUSE = f"ESCAPE '{LIKE_ESCAPE}'"


def like_escape(value: str) -> str:
    """Escape ``LIKE`` metacharacters so ``value`` matches literally.

    The escape character itself first, or the escapes added below would be
    escaped in turn. A backslash is *not* escaped: it carries no meaning to
    ``LIKE`` once ``#`` is the declared escape, so doubling it would corrupt a
    schema name that legitimately contains one.
    """
    return (
        value.replace(LIKE_ESCAPE, LIKE_ESCAPE * 2)
        .replace("%", f"{LIKE_ESCAPE}%")
        .replace("_", f"{LIKE_ESCAPE}_")
    )


def glob_to_like(pattern: str) -> str:
    """Translate an operator's glob into a ``LIKE`` pattern.

    ``*`` becomes ``%``, because a filter is the one place shell habits should
    apply. A literal ``%`` is left alone, so ``dev_*`` and ``dev_%`` stay
    equivalent for anyone who already thinks in SQL.

    ``_`` is escaped, and that is the whole point of this function existing
    rather than a bare ``pattern.replace("*", "%")``. In ``LIKE``, ``_`` matches
    any single character, so ``dev_*`` also matches ``devops_prod`` — every
    schema in these warehouses uses ``_`` as a word separator, so the pattern an
    operator reads as a prefix is not the one the server applies. On a listing
    that is a surprise; on ``dp db schema drop --like`` it selected a schema
    nobody named and destroyed what was in it. Same failure as the
    ``'%_deprecated'`` purge candidates that motivated this module, one command
    further along.

    Pair the result with :data:`LIKE_ESCAPE_CLAUSE`. Escaping without declaring
    the escape character leaves ``#`` as a literal ``#``, which matches nothing —
    a silent empty result rather than an error.
    """
    return (
        pattern.replace(LIKE_ESCAPE, LIKE_ESCAPE * 2)
        .replace("_", f"{LIKE_ESCAPE}_")
        .replace("*", "%")
    )
