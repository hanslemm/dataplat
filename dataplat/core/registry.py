"""The area registry: the single seam between the CLI shell and its areas.

An area is a Typer app plus (optionally) a dependency contract. ``main``
mounts whatever this module returns and knows nothing about individual
areas; areas know nothing about mounting. A future plugin mechanism only
has to make :func:`all_areas` yield additional mounts — ``target`` is
already the ``module:attr`` shape a package entry point declares.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Any

from dataplat.core.deps import AREAS, AreaDeps


@dataclass(frozen=True)
class AreaMount:
    """Everything needed to mount one area on the root command."""

    name: str
    help_text: str
    # Where the area's Typer app lives, as "module:attr".
    target: str
    # Optional-dependency contract; None means always available.
    deps: AreaDeps | None


BUILTIN_AREAS: tuple[AreaMount, ...] = (
    AreaMount(
        name="db",
        help_text="Database query commands",
        target="dataplat.cli.db:app",
        deps=AREAS["db"],
    ),
    AreaMount(
        name="ingest",
        help_text="Data ingestion tools (Airbyte)",
        target="dataplat.cli.ingest.app:app",
        deps=AREAS["ingest"],
    ),
    AreaMount(
        name="bi",
        help_text="Business-intelligence tools (Superset)",
        target="dataplat.cli.bi.app:app",
        deps=AREAS["bi"],
    ),
    AreaMount(
        name="cloud",
        help_text="Cloud-provider tools (AWS)",
        target="dataplat.cli.cloud.app:app",
        deps=AREAS["cloud"],
    ),
    AreaMount(
        name="ci",
        help_text="CI tools (GitHub Actions runners)",
        target="dataplat.cli.ci.app:app",
        deps=None,
    ),
)


def all_areas() -> tuple[AreaMount, ...]:
    """Every mountable area, in display order."""
    return BUILTIN_AREAS


def load_app(mount: AreaMount) -> Any:
    """Import and return the Typer app behind ``mount.target``.

    Only called once the area's dependencies are known to be installed;
    the import may pull in the area's optional packages.
    """
    module_name, _, attr = mount.target.partition(":")
    module = importlib.import_module(module_name)
    return getattr(module, attr)
