"""Shared AWS authentication/session helpers."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from typing import Any

from dataplat.core.errors import AuthError, ServiceError
from dataplat.core.trace import trace

Notifier = Callable[[str], None]

# The trace category for everything the aws group does, declared here because
# this is the lowest AWS module: the CLI helpers import it, and a service module
# must not import back out of dataplat.cli to reach a constant.
CATEGORY_AWS = "aws"

# What a region reads as when we pass none: boto3 then resolves it from the
# profile or the environment, and saying "unset" is honest about which of the
# two the trace can attest to. A blank there would look like a bug in the tracer.
UNSET = "unset"


def _import_boto() -> tuple[Any, Any]:
    try:
        import boto3
        import botocore.exceptions
    except (
        Exception
    ) as exc:  # pragma: no cover - import failure is environment-specific
        raise ServiceError("boto3 and botocore are required for AWS commands") from exc
    return boto3, botocore.exceptions


def ensure_sso_login(profile: str, notify: Notifier | None = None) -> None:
    """Run aws sso login when token is missing/expired."""
    boto3, botocore_exceptions = _import_boto()

    session = boto3.Session(profile_name=profile)
    sts = session.client("sts")

    # The probe is a real API call and the first thing that fails when a token
    # has expired, so it is the one line that explains a command that "did
    # nothing but ask for a login".
    trace(CATEGORY_AWS, f"sts.get_caller_identity | profile={profile} | sso probe")
    try:
        sts.get_caller_identity()
    except (
        botocore_exceptions.UnauthorizedSSOTokenError,
        botocore_exceptions.TokenRetrievalError,
        botocore_exceptions.SSOTokenLoadError,
        botocore_exceptions.NoCredentialsError,
    ):
        # Worded "session expired", not "token expired": redact() masks the value
        # after a bare `token`, because `Authorization: token ghp_…` is exactly
        # that shape — so the honest wording would have come out as `token ***`.
        trace(
            CATEGORY_AWS,
            f"aws sso login | profile={profile} | session expired or missing",
        )
        if notify:
            notify(
                "SSO session expired or missing; running aws sso login "
                f"for profile {profile}"
            )
        try:
            subprocess.run(["aws", "sso", "login", "--profile", profile], check=True)
        except subprocess.CalledProcessError as exc:
            raise AuthError(f"SSO login failed for profile {profile}") from exc


def get_session(
    *,
    profile: str,
    region: str | None = None,
    notify: Notifier | None = None,
):
    """Get a boto3 session with SSO readiness checks."""
    boto3, _ = _import_boto()
    ensure_sso_login(profile, notify=notify)
    trace(
        CATEGORY_AWS,
        f"boto3.Session | profile={profile} | region={region or UNSET}",
    )
    return boto3.Session(profile_name=profile, region_name=region)


def get_client(
    *,
    service_name: str,
    profile: str | None = None,
    region: str | None = None,
    notify: Notifier | None = None,
):
    """Get a boto3 client, optionally through a profile-based session."""
    boto3, _ = _import_boto()

    kwargs: dict[str, str] = {}
    if region:
        kwargs["region_name"] = region

    # Traced before the branch, so the intent is on the record even when the SSO
    # login below never returns one. `profile=unset` is the ambient credential
    # chain, which is a different thing to debug than a named profile.
    trace(
        CATEGORY_AWS,
        f"boto3.client | service={service_name} | profile={profile or UNSET} | "
        f"region={region or UNSET}",
    )

    if profile:
        ensure_sso_login(profile, notify=notify)
        session = boto3.Session(profile_name=profile)
        return session.client(service_name, **kwargs)

    return boto3.client(service_name, **kwargs)
