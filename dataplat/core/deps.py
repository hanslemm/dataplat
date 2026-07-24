"""Optional per-area dependencies: detection and install planning.

Each CLI area maps to a pip extra. This module answers three questions
without importing any optional package:

- which areas the user's configuration *enables*,
- which of their dependencies are *missing*,
- what install command fixes that in the environment ``dp`` runs from.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from importlib.util import find_spec
from pathlib import Path

PACKAGE = "dataplat"


@dataclass(frozen=True)
class AreaDeps:
    """Dependency contract for one CLI area."""

    area: str
    extra: str
    # Import names of the area's dependencies (dist names match on PyPI).
    modules: tuple[str, ...]
    # Env vars whose presence marks the area as enabled by the user.
    enabled_by: tuple[str, ...]


AREAS: dict[str, AreaDeps] = {
    "db": AreaDeps(
        area="db",
        extra="db",
        modules=("psycopg",),
        enabled_by=("DP_TARGETS",),
    ),
    "ingest": AreaDeps(
        area="ingest",
        extra="ingest",
        modules=("httpx", "textual", "croniter"),
        enabled_by=("AIRBYTE_BASE_URL",),
    ),
    "bi": AreaDeps(
        area="bi",
        extra="bi",
        modules=("httpx",),
        enabled_by=("SUPERSET_BASE_URL",),
    ),
    "cloud": AreaDeps(
        area="cloud",
        extra="cloud",
        modules=("boto3", "plotext"),
        enabled_by=("DP_AWS_PROFILE", "DP_AWS_PROFILE_ALIASES", "DP_RDS_INSTANCE"),
    ),
}


def missing_modules(area: str) -> list[str]:
    """Import names of ``area``'s dependencies that are not installed."""
    return [m for m in AREAS[area].modules if find_spec(m) is None]


def area_ready(area: str) -> bool:
    """True when every dependency of ``area`` is importable."""
    return not missing_modules(area)


def enabled_areas() -> dict[str, str]:
    """Areas the user's config enables, mapped to the env var that did it."""
    enabled: dict[str, str] = {}
    for name, spec in AREAS.items():
        for var in spec.enabled_by:
            if os.getenv(var):
                enabled[name] = var
                break
    return enabled


def install_spec(extras: Iterable[str]) -> str:
    """Requirement string for ``extras``, e.g. ``dataplat[db,ingest]``."""
    return f"{PACKAGE}[{','.join(sorted(set(extras)))}]"


def _is_editable_install() -> bool:
    """True for a development checkout (``uv sync`` / ``pip install -e``)."""
    try:
        from importlib.metadata import distribution

        raw = distribution(PACKAGE).read_text("direct_url.json")
        if not raw:
            return False
        return bool(json.loads(raw).get("dir_info", {}).get("editable"))
    except Exception:
        return False


def _in_venv() -> bool:
    return sys.prefix != sys.base_prefix


def install_command(
    extras: Iterable[str], *, executable: str | None = None
) -> list[str] | None:
    """Command that adds ``extras`` to the environment ``dp`` runs from.

    Detects uv tool, pipx, and plain-venv installs from the interpreter
    path. Returns ``None`` when the environment isn't one we should modify
    (editable dev checkout, system Python, unknown layout); callers then
    print :func:`manual_hint` instead of installing.
    """
    spec = install_spec(extras)
    if _is_editable_install():
        return None
    # Never resolve() here: a venv's bin/python is a symlink to the base
    # interpreter, and following it would make pip target the wrong env.
    exe = Path(executable or sys.executable)
    parts = exe.parts
    if "uv" in parts and "tools" in parts and shutil.which("uv"):
        return ["uv", "tool", "install", spec, "--force"]
    if "pipx" in parts and "venvs" in parts and shutil.which("pipx"):
        return ["pipx", "install", spec, "--force"]
    if _in_venv():
        return [str(exe), "-m", "pip", "install", spec]
    return None


def manual_hint(extras: Iterable[str]) -> str:
    """Human instruction for environments we don't auto-modify."""
    if _is_editable_install():
        return "Development checkout — run: uv sync --group dev --all-extras"
    return f"Install the extra yourself, e.g.: pip install '{install_spec(extras)}'"
