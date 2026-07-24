"""Airbyte destinations CLI commands (built by the shared resource factory)."""

from __future__ import annotations

from dataplat.cli.ingest.airbyte._resource import make_resource_app
from dataplat.services.airbyte.destinations import (
    create_destination,
    delete_destination,
    get_destination,
    list_destinations,
    update_destination,
)

app = make_resource_app(
    kind="destination",
    plural="destinations",
    id_flag="--destination-id",
    id_short="-d",
    id_key="destinationId",
    connector_keys=("destinationName", "destinationDefinitionName"),
    list_fn=list_destinations,
    get_fn=get_destination,
    create_fn=create_destination,
    update_fn=update_destination,
    delete_fn=delete_destination,
)
