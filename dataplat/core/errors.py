"""Typed domain exceptions, each declaring the exit code it should produce.

Exit codes are a public contract — a script branches on them long after it has
stopped reading our output — so they are anchored to what the tool already did
rather than invented. Click exits 2 for its own usage errors, and every
pre-existing ``typer.Exit(code=2)`` in this repo is input validation, so 2
already meant "invalid input" here; the rest follow from it:

======  =============================  =====================================
``0``   :attr:`ExitCode.SUCCESS`       the command did what was asked
``1``   :attr:`ExitCode.FAILURE`       unexpected or unclassified failure —
                                       the base :class:`DataplatError`, and
                                       every bare exit that already meant
                                       "no", a declined confirmation included
``2``   :attr:`ExitCode.INVALID_INPUT` :class:`ValidationError`
``3``   :attr:`ExitCode.CONFIG`        :class:`ConfigError`
``4``   :attr:`ExitCode.AUTH`          :class:`AuthError`
``5``   :attr:`ExitCode.SERVICE`       :class:`ServiceError`
======  =============================  =====================================

The code is a class attribute on the exception, not a mapping inside the CLI,
for two reasons: a caller can ask the exception it already caught, and a new
error type inherits a defensible code instead of falling off the end of a
dispatch table nobody remembered to extend. :func:`dataplat.cli._exit.fail` is
the one place that turns it into an exit.

Exit *with* one of these numbers, never with a bare integer: ``ExitCode`` is
what makes them greppable from docs and tests, which is the only reason a
contract like this survives contact with seven contributors.
"""

from __future__ import annotations

from enum import IntEnum
from typing import ClassVar

__all__ = [
    "AuthError",
    "ConfigError",
    "DataplatError",
    "ExitCode",
    "ServiceError",
    "ValidationError",
]


class ExitCode(IntEnum):
    """Every exit status a dataplat command is allowed to produce."""

    SUCCESS = 0
    # Unclassified. Also what a refusal exits with: declining a confirmation is
    # not a service failure and must never be reported as one.
    FAILURE = 1
    # 2 is Click's own usage-error code. Sharing it is deliberate: to a caller,
    # "you passed a bad value" is one condition, whether Click or we caught it.
    INVALID_INPUT = 2
    CONFIG = 3
    AUTH = 4
    SERVICE = 5


class DataplatError(Exception):
    """Base class for predictable operational errors.

    Its own code is :attr:`ExitCode.FAILURE`, because a bare ``DataplatError``
    says only "this failed for a reason we anticipated" — which is exactly the
    unclassified case. Subclasses narrow it.
    """

    exit_code: ClassVar[ExitCode] = ExitCode.FAILURE


class ConfigError(DataplatError):
    """Raised for missing/invalid local configuration."""

    exit_code: ClassVar[ExitCode] = ExitCode.CONFIG


class AuthError(DataplatError):
    """Raised for authentication and authorization failures."""

    exit_code: ClassVar[ExitCode] = ExitCode.AUTH


class ServiceError(DataplatError):
    """Raised for provider/service API failures."""

    exit_code: ClassVar[ExitCode] = ExitCode.SERVICE


class ValidationError(DataplatError):
    """Raised for user input validation failures."""

    exit_code: ClassVar[ExitCode] = ExitCode.INVALID_INPUT
