"""Running a query that may reference a catalog the cluster does not have.

Redshift's ``svv_*`` views are version-dependent: RBAC views are absent on a
pre-RBAC cluster, quota views vary by version, and any of them can be
permission-denied for a non-superuser. Probing for them is unavoidable, and a
failed probe inside a transaction aborts *the whole transaction*, so a feature
detection would take the caller's real work down with it.

Hence the savepoint. Set one, run the query, and roll back to it on failure so
the connection is left exactly as it was found.

``None`` means "could not tell", which is deliberately distinct from ``[]``,
meaning "asked, and the answer is nothing". Callers must not collapse the two: a
quota view that is unavailable has to render as *unknown*, never as zero, and an
undetectable privilege has to default to "not held" — an idempotent re-GRANT
costs nothing, whereas claiming a privilege is held when nobody checked is how a
listing reports access that does not exist.
"""

from __future__ import annotations

import contextlib
from typing import Any

__all__ = ["guarded_fetch"]


def guarded_fetch(
    cursor: Any,
    sql_text: str,
    params: tuple[Any, ...] = (),
    *,
    savepoint: str,
) -> list[tuple[Any, ...]] | None:
    """Run ``sql_text``, returning its rows, or ``None`` if it could not run.

    ``savepoint`` is an identifier, not user input — every call site passes a
    literal, because it is interpolated into the statement text (SAVEPOINT takes
    no bound parameters).
    """
    try:
        cursor.execute(f"SAVEPOINT {savepoint}")
    except Exception:  # noqa: BLE001  connection-level failure: nothing to guard
        return None
    try:
        cursor.execute(sql_text, params)
        rows = list(cursor.fetchall())
    except Exception:  # noqa: BLE001  view missing / permission denied
        with contextlib.suppress(Exception):
            cursor.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        return None
    with contextlib.suppress(Exception):
        cursor.execute(f"RELEASE SAVEPOINT {savepoint}")
    return rows
