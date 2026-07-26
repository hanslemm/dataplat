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


def missing_for(spec: AreaDeps) -> list[str]:
    """Import names of ``spec``'s dependencies that are not installed."""
    return [m for m in spec.modules if find_spec(m) is None]


def ready(spec: AreaDeps) -> bool:
    """True when every dependency in ``spec`` is importable."""
    return not missing_for(spec)


# The by-name wrappers below only reach the built-in AREAS. A third-party area
# carries its own AreaDeps and would KeyError here, so anything holding a spec
# (the registry, its mounts) must call missing_for/ready directly.
def missing_modules(area: str) -> list[str]:
    """Import names of built-in ``area``'s dependencies that are not installed."""
    return missing_for(AREAS[area])


def area_ready(area: str) -> bool:
    """True when every dependency of built-in ``area`` is importable."""
    return ready(AREAS[area])


def satisfied_extras() -> list[str]:
    """Extras whose area already imports cleanly in this environment.

    Areas share dependencies, so this is approximate in one direction: ``bi``
    looks satisfied whenever ``ingest``'s httpx is present. That is the
    harmless direction — see :func:`install_command`, where over-including an
    extra only reinstalls packages that are already there, while dropping one
    uninstalls a working area.
    """
    return [spec.extra for spec in AREAS.values() if ready(spec)]


def enabled_areas() -> dict[str, str]:
    """Areas the user's config enables, mapped to the env var that did it."""
    enabled: dict[str, str] = {}
    for name, spec in AREAS.items():
        for var in spec.enabled_by:
            if os.getenv(var):
                enabled[name] = var
                break
    return enabled


def install_spec(extras: Iterable[str], *, version: str | None = None) -> str:
    """Requirement string for ``extras``, e.g. ``dataplat[db,ingest]==0.1.0``.

    ``version`` pins the spec; ``None`` leaves it unpinned.
    """
    pin = f"=={version}" if version else ""
    return f"{PACKAGE}[{','.join(sorted(set(extras)))}]{pin}"


def _installed_version() -> str | None:
    """Version of the ``dataplat`` distribution this interpreter imports.

    ``None`` when there is no installed distribution to read a version from
    (a bare source tree), where any pin would be a guess.
    """
    try:
        from importlib.metadata import PackageNotFoundError, version

        return version(PACKAGE)
    except PackageNotFoundError:
        return None


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


def _has_pip(executable: Path) -> bool:
    """Whether ``executable``'s environment can run ``-m pip``.

    uv creates virtualenvs without pip unless asked, and that is now a common
    way to end up with ``dp`` installed. Prescribing ``python -m pip install``
    there fails with a bare non-zero exit and no explanation, so the caller
    needs to know before recommending it.
    """
    if executable == Path(sys.executable):
        return find_spec("pip") is not None
    return any((executable.parent / name).exists() for name in ("pip", "pip3"))


def install_command(
    extras: Iterable[str], *, executable: str | None = None
) -> list[str] | None:
    """Command that adds ``extras`` to the environment ``dp`` runs from.

    Detects uv tool, pipx, and plain-venv installs from the interpreter
    path. Returns ``None`` when the environment isn't one we should modify
    (editable dev checkout, system Python, unknown layout); callers then
    print :func:`manual_hint` instead of installing.
    """
    if _is_editable_install():
        return None
    # Pin to the version already running, on every path. Callers install a
    # missing extra and then re-exec the user's original invocation (see
    # cli/_missing), so an unpinned spec would resolve to whatever is newest on
    # PyPI and silently turn "add a dependency" into a major upgrade
    # mid-command.
    pinned = _installed_version()
    # Never resolve() here: a venv's bin/python is a symlink to the base
    # interpreter, and following it would make pip target the wrong env.
    exe = Path(executable or sys.executable)
    parts = exe.parts

    # `--force` recreates the environment from this spec *alone*, so it has to
    # carry the extras already installed or installing dataplat[db] uninstalls
    # a working dataplat[ingest] — and callers pass only the *missing* extras,
    # so they cannot compensate. Over-including is safe here (already-present
    # packages are simply reinstalled), dropping is not; satisfied_extras()
    # errs in that safe direction.
    if "uv" in parts and "tools" in parts and shutil.which("uv"):
        forced = install_spec({*extras, *satisfied_extras()}, version=pinned)
        return ["uv", "tool", "install", forced, "--force"]
    if "pipx" in parts and "venvs" in parts and shutil.which("pipx"):
        forced = install_spec({*extras, *satisfied_extras()}, version=pinned)
        return ["pipx", "install", forced, "--force"]

    if _in_venv():
        # Nothing is replaced on this path, so the missing extras are the whole
        # job: unioning would drag unrelated satisfied extras into an install
        # that never threatened them.
        additive = install_spec(extras, version=pinned)
        if _has_pip(exe):
            return [str(exe), "-m", "pip", "install", additive]
        # No pip to call (a uv-made venv). uv can still install into it, and
        # --python aims it at this environment rather than uv's default.
        if shutil.which("uv"):
            return ["uv", "pip", "install", "--python", str(exe), additive]
        # Neither installer is reachable; manual_hint explains what to do.
        return None
    return None


def manual_hint(extras: Iterable[str]) -> str:
    """Human instruction for environments we don't auto-modify."""
    if _is_editable_install():
        return "Development checkout — run: uv sync --group dev --all-extras"
    # Neither pinned nor unioned, unlike install_command: this is a plain
    # additive install in an environment we don't manage, so it removes
    # nothing, and the reader decides which version they want.
    spec = install_spec(extras)
    if _in_venv() and not _has_pip(Path(sys.executable)):
        # Recommending pip to an environment that has none just fails again.
        return f"This environment has no pip — run: uv pip install '{spec}'"
    return f"Install the extra yourself, e.g.: pip install '{spec}'"
