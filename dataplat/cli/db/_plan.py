"""Printing and running a list of :class:`SqlOp`.

Four ``dp db schema`` subcommands share the same three beats — show the SQL, get
a confirmation, run it in order — and the two details worth centralising are that
a statement is escaped before Rich sees it, and that a ``secret=True`` op prints
its description instead of its text.
"""

from __future__ import annotations

from typing import Any

from rich.console import Console

from dataplat.cli._render import esc
from dataplat.services.db.role_dialects import SqlOp

__all__ = ["execute_ops", "print_ops"]


def _adapt_context(conn: Any) -> Any:
    """The object psycopg can render a ``Composed`` against, or ``None``.

    ``as_string(context)`` reads ``context.connection`` to pick an encoding, so a
    psycopg ``Connection`` (which returns itself) works and a
    :class:`~dataplat.cli.db._common.DuckDbSession` raises ``AttributeError``.
    Passing ``None`` is the documented way to render without a connection, and it
    is what a DuckDB target needs — the statements here are identifiers and
    keywords, which do not depend on a server encoding.

    Duck-typed rather than an isinstance check so that this module needs no
    import of either connection class, and so a traced cursor or any other
    psycopg-shaped wrapper keeps its fidelity.
    """
    return conn if hasattr(conn, "connection") else None


def print_ops(
    console: Console, ops: list[SqlOp], conn_ctx: Any, *, indent: int = 2
) -> None:
    """Print each op's rendered SQL, one per line.

    SQL is dense in brackets — quoted identifiers, arrays, driver-rendered
    literals — so an unescaped statement is the likeliest source of a Rich
    ``MarkupError`` in this codebase. An op marked ``secret`` prints its
    description, which is written not to contain the secret.
    """
    pad = " " * indent
    context = _adapt_context(conn_ctx)
    for op in ops:
        if op.secret:
            console.print(f"{pad}[yellow]{esc(op.description)};[/yellow]")
        else:
            console.print(f"{pad}{esc(op.statement.as_string(context))};")


def execute_ops(cursor: Any, ops: list[SqlOp]) -> None:
    """Run every op in order, as given.

    Order is the plan's contract, not an accident: ``USAGE`` before the grants
    that depend on it, ``CREATE SCHEMA`` before anything inside it.
    """
    for op in ops:
        cursor.execute(op.statement)
