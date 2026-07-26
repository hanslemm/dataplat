"""dataplat — one command to manage any shape of data platform."""

from __future__ import annotations

from typing import Any

__all__ = ["__version__"]


def __getattr__(name: str) -> Any:
    """Serve ``__version__`` from the installed distribution, on demand.

    A literal here would be a second place to edit at release time, and the
    kind that goes stale silently: nothing in the package reads it, so a
    mismatch with ``[project].version`` would never surface. Reading the
    metadata keeps pyproject the only source of truth, and doing it in
    ``__getattr__`` (PEP 562) keeps ``importlib.metadata`` off the import path
    of every ``dp`` invocation that never asks.
    """
    if name == "__version__":
        from importlib.metadata import PackageNotFoundError, version

        try:
            return version("dataplat")
        except PackageNotFoundError:  # running from a bare source tree
            return "unknown"
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
