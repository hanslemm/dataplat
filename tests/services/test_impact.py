"""What else points at a schema, before you drop it.

The interesting half is Superset's virtual datasets: on the real instance, 190
of 842 datasets carry no `schema` field at all, and their SQL references
`md_analytics` 184 times, `borg` 171 times, `analytics` 136 times. Matching the
field alone would call dropping `borg` safe while 171 dataset queries break.
"""

from __future__ import annotations

import pytest

from dataplat.services.impact import (
    connections_into,
    datasets_referencing,
    destinations_writing_to,
    sql_references_schema,
)

DATASETS = [
    {
        "id": 1,
        "table_name": "orders",
        "schema": "analytics",
        "sql": None,
        "database": {"database_name": "Dataocean Production"},
    },
    {
        "id": 2,
        "table_name": "virtual_summary",
        "schema": None,
        "sql": "SELECT * FROM analytics.orders JOIN raw.customers USING (id)",
        "database": {"database_name": "Dataocean Production"},
    },
    {
        "id": 3,
        "table_name": "elsewhere",
        "schema": "md_analytics",
        "sql": None,
        "database": {"database_name": "Dataocean Production"},
    },
]


@pytest.mark.parametrize(
    ("statement", "schema", "expected"),
    [
        ("SELECT * FROM analytics.orders", "analytics", True),
        ('SELECT * FROM "analytics".orders', "analytics", True),
        ("SELECT * FROM ANALYTICS.ORDERS", "analytics", True),
        ("select 1 from x join analytics . orders on true", "analytics", True),
        # The trap a word-boundary regex falls into: a longer name that ends
        # with the one being searched for.
        ("SELECT * FROM md_analytics.orders", "analytics", False),
        ('SELECT * FROM "md_analytics".orders', "analytics", False),
        ("SELECT * FROM analytics_staging.orders", "analytics", False),
        # Named but not as a schema: a column called analytics is not a use.
        ("SELECT analytics FROM other.table", "analytics", False),
        ("", "analytics", False),
        (None, "analytics", False),
    ],
)
def test_a_schema_reference_is_read_as_a_qualified_name(
    statement: str | None, schema: str, expected: bool
) -> None:
    assert sql_references_schema(statement, schema) is expected


def test_a_dataset_is_matched_by_its_schema_field() -> None:
    found = datasets_referencing(DATASETS, "analytics")

    by_name = {d.name: d for d in found}
    assert by_name["orders"].via == "schema"


def test_a_virtual_dataset_is_matched_by_its_sql() -> None:
    """The case a field-only match misses, and the majority one in practice."""
    found = datasets_referencing(DATASETS, "raw")

    assert [d.name for d in found] == ["virtual_summary"]
    assert found[0].via == "sql"


def test_a_dataset_naming_a_longer_schema_is_not_matched() -> None:
    found = datasets_referencing(DATASETS, "analytics")

    assert "elsewhere" not in [d.name for d in found]


def test_a_dataset_matched_both_ways_is_reported_once() -> None:
    datasets = [
        {
            "id": 4,
            "table_name": "both",
            "schema": "analytics",
            "sql": "SELECT * FROM analytics.orders",
            "database": {"database_name": "db"},
        }
    ]

    found = datasets_referencing(datasets, "analytics")

    assert len(found) == 1
    assert found[0].via == "schema"


def test_the_database_travels_with_the_finding() -> None:
    """Two databases can hold a schema of the same name; the reader decides."""
    found = datasets_referencing(DATASETS, "analytics")

    assert found[0].database == "Dataocean Production"


DESTINATIONS = [
    {
        "destinationId": "d1",
        "name": "Dataocean raw",
        "configuration": {"schema": "raw", "database": "dataocean"},
    },
    {
        "destinationId": "d2",
        "name": "Other",
        "configuration": {"schema": "staging", "database": "dataocean"},
    },
]


def test_a_destination_writing_into_the_schema_is_found() -> None:
    found = destinations_writing_to(DESTINATIONS, "raw")

    assert [d.name for d in found] == ["Dataocean raw"]
    assert found[0].destination_id == "d1"


def test_a_destination_writing_elsewhere_is_not() -> None:
    assert destinations_writing_to(DESTINATIONS, "analytics") == ()


CONNECTIONS = [
    {
        "connectionId": "c1",
        "name": "sheets -> raw",
        "destinationId": "d1",
        "namespaceDefinition": "destination",
        "status": "active",
    },
    {
        "connectionId": "c2",
        "name": "api -> staging",
        "destinationId": "d2",
        "namespaceDefinition": "destination",
        "status": "active",
    },
    {
        "connectionId": "c3",
        "name": "custom namespace",
        "destinationId": "d1",
        "namespaceDefinition": "custom_format",
        "namespaceFormat": "other_place",
        "status": "active",
    },
]


def test_connections_into_a_destination_are_found() -> None:
    found = connections_into(CONNECTIONS, ("d1",))

    assert [c.name for c in found] == ["sheets -> raw"]


def test_a_connection_overriding_the_namespace_is_not_counted() -> None:
    """It points at the destination but writes somewhere else entirely."""
    found = connections_into(CONNECTIONS, ("d1",))

    assert "custom namespace" not in [c.name for c in found]
