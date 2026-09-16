"""Relation names a dbt project produces, read from its manifest.

The warehouse only knows the relation that exists. dbt knows the node that
produced it, and the two differ whenever a model sets ``alias`` or
``identifier``. Matching on the node name alone reports a live table as an
orphan, which is the one mistake the orphan scan must not make.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from dataplat.services.dbt.projects import DbtProject

# Only these resource types are things dbt actually produces into the
# warehouse. Sources live under the manifest's own ``sources`` key, not
# ``nodes`` -- dbt reads them, it does not create them -- so they are
# deliberately never consulted here. What keeps a source table safe from the
# orphan scan is schema exclusion (raw/_raw), not this function; folding
# sources into the produced set would assert dbt makes things it does not.
_PRODUCING_TYPES = frozenset({"model", "seed", "snapshot"})


def _manifest_path(project: DbtProject) -> Path:
    return project.path / "target" / "manifest.json"


def relation_names(project: DbtProject) -> set[str]:
    """Final relation names ``project``'s manifest says it produces.

    Precedence per node is ``alias``, then ``identifier``, then ``name`` --
    the order dbt itself resolves a node's final relation name in. Names are
    lowercased, since that is how the warehouse catalog reports them.

    Raises ``FileNotFoundError`` if the project has no ``target/manifest.json``
    yet. That case must fail loudly rather than return an empty set: an empty
    produced-set would make every table in the scanned schemas look orphaned,
    which is worse than refusing to run.
    """
    path = _manifest_path(project)
    if not path.is_file():
        raise FileNotFoundError(
            f"No manifest.json under {path.parent}. Run `dbt compile` or "
            "`dbt docs generate` in the project first."
        )
    with path.open(encoding="utf-8") as handle:
        manifest: dict[str, Any] = json.load(handle)

    names: set[str] = set()
    nodes: dict[str, Any] = manifest.get("nodes") or {}
    for node in nodes.values():
        if not isinstance(node, dict):
            continue
        if node.get("resource_type") not in _PRODUCING_TYPES:
            continue
        final = node.get("alias") or node.get("identifier") or node.get("name")
        if final:
            names.add(str(final).lower())
    return names
