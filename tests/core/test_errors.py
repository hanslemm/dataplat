"""The exit-code contract. These numbers are public; pin every one of them.

A caller's script says ``if [ $? -eq 3 ]; then`` and keeps saying it long after
it stopped reading our messages. Renumbering is therefore a breaking change, and
these assertions are what makes that visible in a diff instead of in someone's
pipeline.
"""

from __future__ import annotations

import pytest
from click.exceptions import UsageError

from dataplat.core.errors import (
    AuthError,
    ConfigError,
    DataplatError,
    ExitCode,
    ServiceError,
    ValidationError,
)

# The whole contract, as a table. Read it as documentation.
EXPECTED: list[tuple[type[DataplatError], int]] = [
    (DataplatError, 1),
    (ValidationError, 2),
    (ConfigError, 3),
    (AuthError, 4),
    (ServiceError, 5),
]


@pytest.mark.parametrize(("error_class", "code"), EXPECTED)
def test_class_declares_its_exit_code(
    error_class: type[DataplatError], code: int
) -> None:
    assert error_class.exit_code == code


@pytest.mark.parametrize(("error_class", "code"), EXPECTED)
def test_instance_carries_the_code(error_class: type[DataplatError], code: int) -> None:
    """A catch site holds an instance, not the class, so ask the instance."""
    assert error_class("boom").exit_code == code


@pytest.mark.parametrize(
    ("member", "value"),
    [
        (ExitCode.SUCCESS, 0),
        (ExitCode.FAILURE, 1),
        (ExitCode.INVALID_INPUT, 2),
        (ExitCode.CONFIG, 3),
        (ExitCode.AUTH, 4),
        (ExitCode.SERVICE, 5),
    ],
)
def test_exit_code_values(member: ExitCode, value: int) -> None:
    assert member == value


def test_exit_code_has_no_other_members() -> None:
    """An added member is a contract change and has to be asserted, not slipped."""
    assert {member.value for member in ExitCode} == {0, 1, 2, 3, 4, 5}


@pytest.mark.parametrize(
    ("error_class", "member"),
    [
        (DataplatError, ExitCode.FAILURE),
        (ValidationError, ExitCode.INVALID_INPUT),
        (ConfigError, ExitCode.CONFIG),
        (AuthError, ExitCode.AUTH),
        (ServiceError, ExitCode.SERVICE),
    ],
)
def test_enum_and_class_attribute_agree(
    error_class: type[DataplatError], member: ExitCode
) -> None:
    """The number and its name must not be able to drift apart."""
    assert error_class.exit_code is member


def _all_subclasses(root: type[DataplatError]) -> set[type[DataplatError]]:
    found: set[type[DataplatError]] = set()
    pending = [root]
    while pending:
        current = pending.pop()
        for child in current.__subclasses__():
            if child not in found:
                found.add(child)
                pending.append(child)
    return found


def test_every_error_type_has_a_usable_code() -> None:
    """The reason the code is an attribute: a new type cannot forget it.

    Importing the package pulls in whatever error types exist; each one either
    declares a code or inherits a defensible one, and every value has to be a
    real ExitCode rather than a stray integer.
    """
    for error_class in {DataplatError, *_all_subclasses(DataplatError)}:
        assert isinstance(error_class.exit_code, ExitCode), error_class


def test_typed_errors_are_distinguishable() -> None:
    """Four distinct codes for four distinct causes -- that is the point."""
    codes = {
        error_class.exit_code
        for error_class in (ValidationError, ConfigError, AuthError, ServiceError)
    }
    assert len(codes) == 4
    assert DataplatError.exit_code not in codes


def test_codes_are_plain_ints_for_sys_exit() -> None:
    """typer.Exit(code=...) and the OS both want an int; IntEnum is one."""
    assert isinstance(ValidationError.exit_code, int)
    assert f"{ConfigError.exit_code:d}" == "3"


def test_invalid_input_matches_clicks_own_usage_error() -> None:
    """Why 2 is 'invalid input': Click already exits 2 for a bad invocation.

    To a caller there is one condition -- "you passed something wrong" -- and it
    must not depend on whether Click's parser or our validation noticed.
    """
    assert UsageError.exit_code == ExitCode.INVALID_INPUT
