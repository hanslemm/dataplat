"""What else points at a schema, before you drop it.

`dp db schema drop` can tell you what the schema contains. It cannot tell you
what *elsewhere* depends on it, and that is the half that breaks: a dashboard
nobody opened this week, an ingestion that lands at 03:00. This module answers
that question from the systems that already know -- Superset's datasets and
Airbyte's destinations -- and is pure, so the answer is testable without either.

The interesting case is Superset's virtual datasets. On the instance this was
written against, 190 of 842 datasets carry no ``schema`` field at all, and the
SQL of those references ``md_analytics`` 184 times, ``borg`` 171 times and
``analytics`` 136 times. Matching the field alone reports dropping ``borg`` as
perfectly safe while 171 dataset queries break.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

__all__ = [
    "ConnectionRef",
    "DatasetRef",
    "DestinationRef",
    "connections_into",
    "datasets_referencing",
    "destinations_writing_to",
    "sql_references_schema",
]

# Every `something.` in a statement, quoted or not. Matching the identifier and
# comparing it afterwards, rather than interpolating the schema into a pattern
# with word boundaries: `\banalytics\.` also matches `md_analytics.`, which is a
# different schema and, on this instance, a busy one.
_QUALIFIER = re.compile(r'"([^"]+)"\s*\.|\b([A-Za-z_][A-Za-z0-9_$]*)\s*\.')


@dataclass(frozen=True)
class DatasetRef:
    """A Superset dataset that would break, and how it was found."""

    name: str
    database: str
    via: str  # "schema" (the field) or "sql" (a qualified name inside it)


@dataclass(frozen=True)
class DestinationRef:
    name: str
    destination_id: str
    database: str


@dataclass(frozen=True)
class ConnectionRef:
    name: str
    connection_id: str
    status: str


def sql_references_schema(statement: str | None, schema: str) -> bool:
    """Whether ``statement`` uses ``schema`` as a qualifier."""
    if not statement:
        return False
    wanted = schema.lower()
    return any(
        (quoted or bare).lower() == wanted
        for quoted, bare in _QUALIFIER.findall(statement)
    )


def datasets_referencing(
    datasets: Iterable[dict], schema: str
) -> tuple[DatasetRef, ...]:
    """Datasets whose ``schema`` field names it, or whose SQL qualifies with it.

    A dataset found both ways is reported once, as ``schema``: that is the
    stronger statement about it, and two rows for one dataset would overstate
    the damage.
    """
    found: list[DatasetRef] = []
    for dataset in datasets:
        name = str(dataset.get("table_name") or dataset.get("id") or "?")
        database = str((dataset.get("database") or {}).get("database_name") or "")
        if str(dataset.get("schema") or "").lower() == schema.lower():
            found.append(DatasetRef(name=name, database=database, via="schema"))
        elif sql_references_schema(dataset.get("sql"), schema):
            found.append(DatasetRef(name=name, database=database, via="sql"))
    return tuple(found)


def destinations_writing_to(
    destinations: Iterable[dict], schema: str
) -> tuple[DestinationRef, ...]:
    """Airbyte destinations configured to land in ``schema``."""
    found: list[DestinationRef] = []
    for destination in destinations:
        config = destination.get("configuration") or {}
        if str(config.get("schema") or "").lower() != schema.lower():
            continue
        found.append(
            DestinationRef(
                name=str(destination.get("name") or "?"),
                destination_id=str(destination.get("destinationId") or ""),
                database=str(config.get("database") or ""),
            )
        )
    return tuple(found)


def connections_into(
    connections: Iterable[dict], destination_ids: Sequence[str]
) -> tuple[ConnectionRef, ...]:
    """Connections that write through one of ``destination_ids``.

    A connection with its own namespace format is left out: it points at the
    destination but writes somewhere else entirely, and naming it would send
    someone to check a connection that is not affected.
    """
    wanted = set(destination_ids)
    found: list[ConnectionRef] = []
    for connection in connections:
        if str(connection.get("destinationId") or "") not in wanted:
            continue
        if str(connection.get("namespaceDefinition") or "") != "destination":
            continue
        found.append(
            ConnectionRef(
                name=str(connection.get("name") or "?"),
                connection_id=str(connection.get("connectionId") or ""),
                status=str(connection.get("status") or ""),
            )
        )
    return tuple(found)
