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

from dataplat.core.deps import AREAS, AreaDeps, ready


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


def area_by_name(name: str) -> AreaMount | None:
    """The mount registered as ``name``, or ``None`` if nothing claims it.

    Scanned instead of indexed in a module-level dict: the CLI resolves a name
    once per invocation, and a plugin mechanism that makes :func:`all_areas`
    grow would leave a prebuilt index stale.
    """
    return next((mount for mount in all_areas() if mount.name == name), None)


def missing_extra_help(help_text: str, spec: AreaDeps) -> str:
    """``help_text`` plus the extra an area is waiting on.

    One template, two renderers: the root command lists the area this way while
    it is still a placeholder, and the stub that eventually explains the missing
    extra shows the same line as its own help.
    """
    return f"{help_text} (needs extra: {spec.extra})"


def mount_help(mount: AreaMount) -> str:
    """``mount``'s help line, flagging an area whose extra is not installed.

    The root command lists areas without importing them, so the hint has to be
    answerable from the mount alone — hence ``mount.deps`` rather than a lookup
    in the ``AREAS`` global, which a third-party mount is not in.
    """
    if mount.deps is None or ready(mount.deps):
        return mount.help_text
    return missing_extra_help(mount.help_text, mount.deps)


def load_app(mount: AreaMount) -> Any:
    """Import and return the Typer app behind ``mount.target``.

    Only called once the area's dependencies are known to be installed;
    the import may pull in the area's optional packages.
    """
    module_name, _, attr = mount.target.partition(":")
    module = importlib.import_module(module_name)
    return getattr(module, attr)
