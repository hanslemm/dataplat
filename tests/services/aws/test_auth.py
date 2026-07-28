from __future__ import annotations

import pytest

from dataplat.core.errors import AuthError
from dataplat.core.trace import verbose
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
        self.clients: list[tuple[str, dict]] = []

    def client(self, service_name: str, **kwargs: object):
        self.clients.append((service_name, dict(kwargs)))
        return _FakeSts(self.should_raise)


class _FakeBoto3:
    def __init__(self, should_raise: bool) -> None:
        self.should_raise = should_raise
        self.session_args: list[dict] = []
        self.sessions: list[_FakeSession] = []
        self.clients: list[tuple[str, dict]] = []

    def Session(self, profile_name: str, region_name: str | None = None):
        assert profile_name
        self.session_args.append({"profile": profile_name, "region": region_name})
        session = _FakeSession(self.should_raise)
        self.sessions.append(session)
        return session

    def client(self, service_name: str, **kwargs: object):
        self.clients.append((service_name, dict(kwargs)))
        return object()


def test_import_boto_returns_boto3_then_botocore_exceptions() -> None:
    """Every other test patches this seam, so nothing else checks its shape.

    The order is load-bearing: callers unpack ``boto3, exceptions``, and swapping
    them would only fail at the point an SSO error was already being handled.
    """
    boto3_module, exceptions = auth._import_boto()

    assert boto3_module.__name__ == "boto3"
    assert issubclass(exceptions.NoCredentialsError, Exception)


def test_ensure_sso_login_skips_login_when_identity_works(monkeypatch) -> None:
    monkeypatch.setattr(
        auth,
        "_import_boto",
        lambda: (_FakeBoto3(should_raise=False), _FakeExceptions),
    )

    called = {"run": 0}
    monkeypatch.setattr(
        auth.subprocess, "run", lambda *args, **kwargs: called.__setitem__("run", 1)
    )

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
        raise auth.subprocess.CalledProcessError(
            returncode=1, cmd=["cloud", "aws", "sso", "login"]
        )

    monkeypatch.setattr(auth.subprocess, "run", _boom)

    with pytest.raises(AuthError):
        auth.ensure_sso_login("my-profile")


def test_sso_login_runs_the_profile_scoped_command(monkeypatch) -> None:
    """The argv is the contract with the user's machine.

    ``check=True`` is half of it: without it a failed login returns quietly and
    the AuthError above is never raised, so the caller retries against a session
    that is still expired.
    """
    monkeypatch.setattr(
        auth, "_import_boto", lambda: (_FakeBoto3(should_raise=True), _FakeExceptions)
    )
    seen: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        auth.subprocess, "run", lambda *args, **kwargs: seen.append((args, kwargs))
    )

    auth.ensure_sso_login("my-profile")

    assert seen[0][0][0] == ["aws", "sso", "login", "--profile", "my-profile"]
    assert seen[0][1] == {"check": True}


def test_notifier_is_told_which_profile_is_being_logged_in(monkeypatch) -> None:
    """The one line a user sees before the browser opens, and it names the profile.

    It is a callback rather than a print because the caller decides where it goes:
    ``dp status --json`` sends it to stderr to keep the payload parseable.
    """
    monkeypatch.setattr(
        auth, "_import_boto", lambda: (_FakeBoto3(should_raise=True), _FakeExceptions)
    )
    monkeypatch.setattr(auth.subprocess, "run", lambda *a, **k: None)
    seen: list[str] = []

    auth.ensure_sso_login("my-profile", notify=seen.append)

    assert seen == [
        "SSO session expired or missing; running aws sso login for profile my-profile"
    ]


@pytest.mark.parametrize(
    ("profile", "region"),
    [
        ("my-profile", "eu-central-1"),
        (None, "eu-central-1"),
        ("my-profile", None),
        (None, None),
    ],
    ids=["profile-and-region", "region-only", "profile-only", "neither"],
)
def test_get_client_forwards_only_the_region_it_was_given(
    profile: str | None, region: str | None, monkeypatch
) -> None:
    """A region reaches boto3 as ``region_name``, and is omitted when absent.

    With no region the keyword is left off entirely, so botocore resolves the
    profile's own region instead of being handed something to override it with.
    """
    boto3 = _FakeBoto3(should_raise=False)
    monkeypatch.setattr(auth, "_import_boto", lambda: (boto3, _FakeExceptions))

    auth.get_client(service_name="secretsmanager", profile=profile, region=region)

    expected = {"region_name": region} if region else {}
    if profile:
        # Two sessions: the SSO probe's, then the one the client is built on.
        assert boto3.sessions[0].clients == [("sts", {})]
        assert boto3.sessions[-1].clients == [("secretsmanager", expected)]
        assert boto3.clients == [], "a profile must not use the ambient chain"
    else:
        assert boto3.clients == [("secretsmanager", expected)]
        assert boto3.sessions == [], "no profile means no session at all"


# ── --verbose ────────────────────────────────────────────────────────────────
# The STS probe is the first thing that fails when a token has expired, so it is
# the line that explains a command which appeared to do nothing but ask for a
# login. Everything here goes to stderr: stdout belongs to --json.


def test_verbose_traces_the_probe_and_the_session(monkeypatch, capsys) -> None:
    boto3 = _FakeBoto3(should_raise=False)
    monkeypatch.setattr(auth, "_import_boto", lambda: (boto3, _FakeExceptions))

    with verbose():
        auth.get_session(profile="my-profile", region="eu-central-1")

    captured = capsys.readouterr()
    assert captured.out == "", "stdout belongs to the command's own output"
    assert (
        "[dp:aws] sts.get_caller_identity | profile=my-profile | sso probe"
        in captured.err
    )
    assert (
        "[dp:aws] boto3.Session | profile=my-profile | region=eu-central-1"
        in captured.err
    )


def test_verbose_traces_the_sso_login_it_had_to_run(monkeypatch, capsys) -> None:
    """And the wording survives the redactor, which is not a given.

    "token expired" came out as "token ***": redact() masks the word after a bare
    ``token`` because ``Authorization: token ghp_…`` is that exact shape. Failing
    towards over-masking is the right default, so the message moved instead.
    """
    monkeypatch.setattr(
        auth, "_import_boto", lambda: (_FakeBoto3(should_raise=True), _FakeExceptions)
    )
    monkeypatch.setattr(auth.subprocess, "run", lambda *a, **k: None)

    with verbose():
        auth.ensure_sso_login("my-profile")

    assert (
        "[dp:aws] aws sso login | profile=my-profile | session expired or missing"
        in capsys.readouterr().err
    )


@pytest.mark.parametrize(
    ("profile", "expected"),
    [
        ("my-profile", "profile=my-profile"),
        # No profile means the ambient credential chain, which is a different
        # thing to debug than a named profile -- so it is not left blank.
        (None, "profile=unset"),
    ],
    ids=["named-profile", "ambient-credentials"],
)
def test_verbose_traces_the_client_it_built(
    profile: str | None, expected: str, monkeypatch, capsys
) -> None:
    boto3 = _FakeBoto3(should_raise=False)
    monkeypatch.setattr(auth, "_import_boto", lambda: (boto3, _FakeExceptions))

    with verbose():
        auth.get_client(service_name="secretsmanager", profile=profile)

    err = capsys.readouterr().err
    assert (
        f"[dp:aws] boto3.client | service=secretsmanager | {expected} | region=unset"
        in err
    )


def test_nothing_is_traced_without_verbose(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        auth, "_import_boto", lambda: (_FakeBoto3(should_raise=False), _FakeExceptions)
    )

    auth.get_session(profile="my-profile", region="eu-central-1")

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
