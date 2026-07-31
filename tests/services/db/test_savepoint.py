"""``guarded_fetch``: the difference between "nothing" and "could not tell".

That distinction is the whole reason this helper exists rather than a bare
try/except returning ``[]``. A quota view that is unavailable must render as
*unknown*, and a privilege nobody could check must default to *not held* — both
of which need a value that is not an empty result set.
"""

from __future__ import annotations

from dataplat.services.db._savepoint import guarded_fetch


class _Cursor:
    """Records statements; optionally fails on the query or on the savepoint."""

    def __init__(
        self,
        rows: list[tuple] | None = None,
        *,
        fail_query: bool = False,
        fail_savepoint: bool = False,
    ) -> None:
        self._rows = rows if rows is not None else []
        self._fail_query = fail_query
        self._fail_savepoint = fail_savepoint
        self.executed: list[str] = []

    def execute(self, sql_text, params=None) -> None:
        text = str(sql_text)
        self.executed.append(text)
        if text.startswith("SAVEPOINT") and self._fail_savepoint:
            raise RuntimeError("connection is closed")
        if not text.startswith(("SAVEPOINT", "ROLLBACK", "RELEASE")) and (
            self._fail_query
        ):
            raise RuntimeError('relation "svv_thing" does not exist')

    def fetchall(self) -> list[tuple]:
        return self._rows


def test_rows_come_back_and_the_savepoint_is_released() -> None:
    cursor = _Cursor([("a", 1)])

    assert guarded_fetch(cursor, "SELECT 1", savepoint="sp") == [("a", 1)]
    assert cursor.executed[0] == "SAVEPOINT sp"
    assert cursor.executed[-1] == "RELEASE SAVEPOINT sp"


def test_an_empty_result_is_a_list_not_none() -> None:
    """Asked, and the answer is nothing — a different fact from "could not ask"."""
    assert guarded_fetch(_Cursor([]), "SELECT 1", savepoint="sp") == []


def test_a_missing_view_returns_none_and_rolls_back() -> None:
    """The rollback is the point: the caller's transaction must stay usable."""
    cursor = _Cursor(fail_query=True)

    assert guarded_fetch(cursor, "SELECT 1 FROM svv_thing", savepoint="sp") is None
    assert "ROLLBACK TO SAVEPOINT sp" in cursor.executed
    assert "RELEASE SAVEPOINT sp" not in cursor.executed


def test_a_connection_that_cannot_savepoint_returns_none_without_querying() -> None:
    """Nothing to guard with, so do not run the query that needs guarding."""
    cursor = _Cursor(fail_savepoint=True)

    assert guarded_fetch(cursor, "SELECT 1 FROM svv_thing", savepoint="sp") is None
    assert cursor.executed == ["SAVEPOINT sp"]


def test_a_failing_rollback_is_swallowed() -> None:
    """A dead connection must not turn a feature probe into a traceback."""

    class _Hostile(_Cursor):
        def execute(self, sql_text, params=None) -> None:
            text = str(sql_text)
            self.executed.append(text)
            if text.startswith("SAVEPOINT"):
                return
            raise RuntimeError("everything is broken")

    assert guarded_fetch(_Hostile(), "SELECT 1", savepoint="sp") is None


def test_params_are_passed_through() -> None:
    seen: list[tuple] = []

    class _Recording(_Cursor):
        def execute(self, sql_text, params=None) -> None:
            super().execute(sql_text, params)
            if params:
                seen.append(params)

    guarded_fetch(_Recording([]), "SELECT %s", (["a", "b"],), savepoint="sp")

    assert seen == [(["a", "b"],)]
