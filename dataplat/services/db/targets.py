"""Named database targets, declared through the environment.

A target bundles everything the rest of the code needs to reach one
warehouse: the env-var prefix that holds its connection settings, the SQL
engine dialect, and the owner that orphaned objects should be reassigned to
when roles are dropped.

Targets are fully user-defined:

- ``DP_TARGETS`` — comma-separated target names, e.g. ``warehouse,lake``.
- Per target ``<NAME>_ENGINE`` — ``postgresql`` (default) or ``redshift``.
- Per target ``<NAME>_HOST/_PORT/_USER/_PASSWORD/_DATABASE/...`` — connection
  settings, read by :mod:`dataplat.services.db.connection`.
- Per target ``<NAME>_REASSIGN_OWNER`` — default owner for ``role drop``.
- ``DP_DEFAULT_TARGET`` — the target used when ``--target`` is omitted
  (defaults to the first name in ``DP_TARGETS``).

``<NAME>`` is the target name uppercased with ``-`` mapped to ``_``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from dataplat.core.errors import ConfigError, ValidationError
from dataplat.services.db.connection import SqlEngine


@dataclass(frozen=True)
class DbTarget:
    """A named warehouse the CLI can connect to."""

    name: str
    env_prefix: str
    engine: SqlEngine
    reassign_owner: str | None = None


ALL_TARGETS = "all"

_TARGETS_VAR = "DP_TARGETS"
_DEFAULT_TARGET_VAR = "DP_DEFAULT_TARGET"


def _prefix_for(name: str) -> str:
    return name.strip().upper().replace("-", "_")


def _build_target(name: str) -> DbTarget:
    prefix = _prefix_for(name)
    raw_engine = os.getenv(f"{prefix}_ENGINE", "").strip().lower()
    if raw_engine:
        try:
            engine = SqlEngine(raw_engine)
        except ValueError:
            valid = ", ".join(e.value for e in SqlEngine)
            raise ConfigError(f"{prefix}_ENGINE must be one of: {valid}.")
    else:
        engine = SqlEngine.postgresql
    return DbTarget(
        name=name,
        env_prefix=prefix,
        engine=engine,
        reassign_owner=os.getenv(f"{prefix}_REASSIGN_OWNER") or None,
    )


def load_targets() -> dict[str, DbTarget]:
    """Build the target registry from ``DP_TARGETS``.

    Returns an empty dict when no targets are configured; commands that
    need a target then fall back to raw ``--engine``/``--env-prefix``
    flags or PG*/DB_* env vars.
    """
    raw = os.getenv(_TARGETS_VAR, "")
    targets: dict[str, DbTarget] = {}
    for chunk in raw.split(","):
        name = chunk.strip().lower()
        if not name:
            continue
        if name == ALL_TARGETS:
            raise ConfigError(f"'{ALL_TARGETS}' is a reserved target name.")
        targets[name] = _build_target(name)
    return targets


def default_target_name() -> str | None:
    """The target used when ``--target`` is omitted, if any."""
    explicit = os.getenv(_DEFAULT_TARGET_VAR, "").strip().lower()
    targets = load_targets()
    if explicit:
        if explicit not in targets:
            known = ", ".join(targets) or "none configured"
            raise ConfigError(
                f"{_DEFAULT_TARGET_VAR}='{explicit}' is not in {_TARGETS_VAR} "
                f"(known targets: {known})."
            )
        return explicit
    return next(iter(targets), None)


def resolve_target(name: str) -> DbTarget:
    """Return the target for ``name``; raise ValidationError if unknown."""
    targets = load_targets()
    target = targets.get(name.strip().lower())
    if target is None:
        if not targets:
            raise ValidationError(
                f"No targets configured. Set {_TARGETS_VAR} (e.g. "
                f"{_TARGETS_VAR}=warehouse) plus <NAME>_HOST/_USER/... env vars, "
                "or use --engine/--env-prefix directly."
            )
        known = ", ".join(targets)
        raise ValidationError(f"Unknown target '{name}'. Known targets: {known}.")
    return target


def resolve_targets(name: str) -> list[DbTarget]:
    """Like resolve_target, but ``all`` expands to every configured target."""
    if name.strip().lower() == ALL_TARGETS:
        targets = load_targets()
        if not targets:
            raise ValidationError(
                f"No targets configured. Set {_TARGETS_VAR} to use '{ALL_TARGETS}'."
            )
        return list(targets.values())
    return [resolve_target(name)]
