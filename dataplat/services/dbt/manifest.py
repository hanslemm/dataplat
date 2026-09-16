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


# The macro this whole feature replaces (macros/global/deprecated_models.sql
# in betterdoc-org/mage) calls this "excluded" partitioning: a table like
# ``fct_events_p_trello`` has no manifest node of its own -- dbt only ever
# produces ``fct_events`` -- so without this, spotting it as an orphan is a
# false positive real enough that someone acts on it.
_PARTITION_MARKER = "_p_"


def is_partition_of(relation: str, produced: set[str]) -> bool:
    """Whether ``relation`` is a partition child of something ``produced`` builds.

    A naive version splits ``relation`` on the *first* (or last) occurrence of
    ``_p_`` and checks whether the piece before it is in ``produced``. That
    breaks the moment a produced model's own name legitimately contains the
    marker: given ``produced = {"shipments_p_class"}``, a genuine partition
    child ``shipments_p_class_p_west`` splits, on either end, into a head that
    is not ``"shipments_p_class"`` -- so a single split can never be right for
    every produced name at once. The macro this replaces does not have this
    problem because its SQL anchors the *produced* name as the prefix
    (``'^' || model_name || '_p_.*$'``), checked against every model, not the
    candidate's own text split at one position. This does the same: for each
    name the project produces, ask whether ``relation`` extends it with the
    marker and something after. ``produced`` is typically small enough
    (hundreds of models) that the linear scan costs nothing that matters.

    Case-insensitive on both sides -- ``relation`` is lowercased here the same
    way ``relation_names`` lowercases everything it returns, so a caller
    comparing a warehouse-cased candidate against ``produced`` does not have
    to get that normalization right twice. Requires a non-empty partition-key
    suffix: ``relation`` merely ending in the bare marker with nothing after
    it is not a real partition name under this or the macro's convention.

    Only spares a child whose parent is still live. A partition of a model
    that has genuinely gone is an orphan like any other, and sparing it would
    make this exception a way to accumulate dead tables forever.
    """
    relation = relation.lower()
    for parent in produced:
        prefix = f"{parent.lower()}{_PARTITION_MARKER}"
        if relation.startswith(prefix) and len(relation) > len(prefix):
            return True
    return False
