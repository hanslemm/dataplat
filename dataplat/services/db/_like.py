"""Escaping for SQL ``LIKE`` patterns.

``_`` and ``%`` are wildcards inside a ``LIKE`` pattern, so any value
interpolated into one — a schema prefix, a suffix such as ``_deprecated``, a
project name — matches more than it reads as. That is easy to miss because the
over-matching pattern still returns the rows you expected, plus others.

This lived in ``top_tables`` while ``orphans`` went without, which is precisely
how ``LIKE '%_deprecated'`` came to treat its underscore as "any character" and
report unrelated relations as purge candidates. One home, so the next module to
build a pattern has somewhere obvious to reach for.

``ESCAPE '\\'`` is standard SQL and accepted by both PostgreSQL and Redshift;
``dp db top-tables`` has always sent it to both.
"""

from __future__ import annotations

__all__ = ["LIKE_ESCAPE_CLAUSE", "like_escape"]

# Pair every pattern built with :func:`like_escape` with this clause: the
# escape character is only special when the statement declares it.
LIKE_ESCAPE_CLAUSE = "ESCAPE '\\'"


def like_escape(value: str) -> str:
    """Escape ``LIKE`` metacharacters so ``value`` matches literally.

    Backslash first, or the escapes added below would be escaped in turn.
    """
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
