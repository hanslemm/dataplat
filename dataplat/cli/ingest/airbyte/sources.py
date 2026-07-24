"""Airbyte sources CLI commands (built by the shared resource factory)."""

from __future__ import annotations

from dataplat.cli.ingest.airbyte._resource import make_resource_app
from dataplat.services.airbyte.sources import (
    create_source,
    delete_source,
    get_source,
    list_sources,
    update_source,
)

app = make_resource_app(
    kind="source",
    plural="sources",
    id_flag="--source-id",
    id_short="-s",
    id_key="sourceId",
    connector_keys=("sourceName", "sourceDefinitionName"),
    list_fn=list_sources,
    get_fn=get_source,
    create_fn=create_source,
    update_fn=update_source,
    delete_fn=delete_source,
)
