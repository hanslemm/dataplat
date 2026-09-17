"""Stripping comments and string literals so SQL can be scanned by content.

Two callers each need to look at what a piece of SQL actually says without
being fooled by what merely sits inside a comment or a string literal:
:mod:`dataplat.core.redshift_ports`, scanning for portability-breaking call
names and syntax, and :mod:`dataplat.services.superset.databases`, checking
whether a dataset's SQL carries a statement separator before that SQL is
wrapped and run. Neither owns this logic -- it is shared so a false-positive
fix (a construct that only ever appears inside a literal, say) lands once and
applies to both, instead of being fixed in one copy while the other silently
keeps the bug.
"""

from __future__ import annotations

import re

__all__ = ["strip_comments", "strip_literals"]


def strip_comments(sql: str) -> str:
    """Remove comments only, in the one order that works.

    Jinja comments FIRST: a ``{# ... #}`` block may legitimately contain
    ``--``, and stripping line comments first eats the block's closing tag,
    leaving the whole comment in the scanned text.

    String literals are deliberately left alone here -- call
    :func:`strip_literals` on the result when a literal's contents must not
    confuse the scan too. Some callers need comments stripped but literals
    intact (a scan that looks for content that only ever appears inside a
    literal, such as a regex pattern argument or an ``interval '1 month'``);
    blanking literals unconditionally here would make those permanently
    unable to match anything.
    """
    sql = re.sub(r"\{#.*?#\}", "", sql, flags=re.S)
    sql = re.sub(r"/\*.*?\*/", "", sql, flags=re.S)
    return re.sub(r"--[^\n]*", "", sql)


def strip_literals(sql: str) -> str:
    """Blank string literals, on top of comment stripping.

    Meant to run on :func:`strip_comments`'s output, not raw SQL: a line
    comment containing a stray quote (``-- don't``) would otherwise desync
    the literal scan for everything after it. A label like
    ``'Verdauungssystem (Magen)'`` would otherwise be read as a call to a
    function, or a literal's own ``;`` mistaken for a statement separator.
    """
    return re.sub(r"'(?:[^']|'')*'", "''", sql)
