from __future__ import annotations

import pytest

from dataplat.core.errors import AuthError
from dataplat.services.aws import auth


class _Unauthorized(Exception):
    pass


class _NoCreds(Exception):
    pass


class _Token(Exception):
    pass


class _Load(Exception):
    pass


class _FakeExceptions:
    UnauthorizedSSOTokenError = _Unauthorized
    TokenRetrievalError = _Token
    SSOTokenLoadError = _Load
    NoCredentialsError = _NoCreds


class _FakeSts:
    def __init__(self, should_raise: bool) -> None:
        self.should_raise = should_raise

    def get_caller_identity(self) -> None:
        if self.should_raise:
            raise _NoCreds()


class _FakeSession:
    def __init__(self, should_raise: bool) -> None:
        self.should_raise = should_raise

    def client(self, _: str):
        return _FakeSts(self.should_raise)


class _FakeBoto3:
    def __init__(self, should_raise: bool) -> None:
        self.should_raise = should_raise

    def Session(self, profile_name: str):
        assert profile_name
        return _FakeSession(self.should_raise)


def test_ensure_sso_login_skips_login_when_identity_works(monkeypatch) -> None:
    monkeypatch.setattr(
        auth,
        "_import_boto",
        lambda: (_FakeBoto3(should_raise=False), _FakeExceptions),
    )

    called = {"run": 0}
    monkeypatch.setattr(auth.subprocess, "run", lambda *args, **kwargs: called.__setitem__("run", 1))

    auth.ensure_sso_login("my-profile")
    assert called["run"] == 0


def test_ensure_sso_login_runs_aws_sso_when_expired(monkeypatch) -> None:
    monkeypatch.setattr(
        auth,
        "_import_boto",
        lambda: (_FakeBoto3(should_raise=True), _FakeExceptions),
    )

    called = {"run": 0}

    def _fake_run(*args, **kwargs):
        called["run"] += 1

    monkeypatch.setattr(auth.subprocess, "run", _fake_run)

    auth.ensure_sso_login("my-profile")
    assert called["run"] == 1


def test_ensure_sso_login_raises_auth_error_on_failed_login(monkeypatch) -> None:
    monkeypatch.setattr(
        auth,
        "_import_boto",
        lambda: (_FakeBoto3(should_raise=True), _FakeExceptions),
    )

    def _boom(*args, **kwargs):
        raise auth.subprocess.CalledProcessError(returncode=1, cmd=["cloud", "aws", "sso", "login"])

    monkeypatch.setattr(auth.subprocess, "run", _boom)

    with pytest.raises(AuthError):
        auth.ensure_sso_login("my-profile")
