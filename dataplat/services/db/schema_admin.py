"""Schema inspection and administration, engine-independent.

The dialects in :mod:`dataplat.services.db.schema_dialects` own the SQL; this
module owns the shapes that come back out of it and the argument parsing that
goes in. It opens no connections.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["SchemaSummary", "translate_like_pattern"]


@dataclass(frozen=True)
class SchemaSummary:
    """One row of ``dp db schema list``.

    ``quota_mb`` / ``used_mb`` are Redshift-only and stay ``None`` everywhere
    else — including on a Redshift cluster whose quota view is unavailable. That
    is why they are ``None`` rather than ``0``: callers must render them as
    *unknown*, because a schema with an unknown quota is not a schema with no
    quota.

    ``other`` counts relations that are neither table-like nor view-like
    (sequences, composite types) — anything that still blocks a ``RESTRICT``
    drop and is still destroyed by ``CASCADE``, but has no more specific bucket.
    It exists so that nothing a drop would destroy goes uncounted; ``list`` does
    not render it.
    """

    name: str
    owner: str
    tables: int
    views: int
    quota_mb: int | None = None
    used_mb: int | None = None
    other: int = 0


def translate_like_pattern(pattern: str) -> str:
    """Accept glob ``*`` as a friendlier spelling of SQL ``LIKE``'s ``%``.

    ``dev_*`` and ``dev_%`` both work, because a filter is the one place an
    operator expects shell habits to apply.

    ``_`` is left alone. It *is* a single-character wildcard in ``LIKE``, so
    ``dev_x`` also matches ``devax`` — but every schema in these warehouses uses
    ``_`` as a literal word separator, and over-matching by one character in a
    read-only filter is harmless. Where over-matching is *not* harmless — a
    pattern that selects schemas to drop — the caller escapes it instead, with
    :func:`~dataplat.services.db._like.like_escape`.
    """
    return pattern.replace("*", "%")
