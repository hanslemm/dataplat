"""The one way a Superset command gets its connection settings.

Four command modules need the same three values and the same failure. Reading
the config in each of them is how the wording drifts; importing a private name
out of ``superset.py`` is how an import cycle starts once that module mounts
the sub-apps.
"""

from __future__ import annotations

from rich.console import Console

from dataplat.cli._exit import fail
from dataplat.core.errors import ConfigError
from dataplat.services.superset.client import get_auth_config_from_env

__all__ = ["load_auth_context"]


def load_auth_context(console: Console) -> tuple[str, str, str]:
    """Base URL, username and password, or exit with the config error."""
    try:
        cfg = get_auth_config_from_env()
    except ConfigError as exc:
        fail(exc, console=console)
    return cfg.base_url, cfg.username, cfg.password
