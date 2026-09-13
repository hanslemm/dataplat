"""The package's one piece of runtime behaviour: where the version comes from.

A literal ``__version__`` would be a second place to edit at release time, and
the kind that goes stale silently — nothing in the package reads it, so a
mismatch with ``[project].version`` would never surface anywhere.
"""

from __future__ import annotations

import pytest

import dataplat


def test_the_version_comes_from_the_installed_distribution() -> None:
    assert dataplat.__version__ == __import__(
        "importlib.metadata", fromlist=["x"]
    ).version("dataplat")


def test_a_bare_source_tree_reports_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    """Running from a checkout that was never installed is not an error."""
    import importlib.metadata as metadata

    def missing(name: str) -> str:
        raise metadata.PackageNotFoundError(name)

    monkeypatch.setattr(metadata, "version", missing)

    assert dataplat.__version__ == "unknown"


def test_any_other_attribute_is_still_an_error() -> None:
    """__getattr__ must not turn typos into None."""
    with pytest.raises(AttributeError, match="no attribute 'nope'"):
        _ = dataplat.nope
