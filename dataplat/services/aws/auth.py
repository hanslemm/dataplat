"""Shared AWS authentication/session helpers."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from typing import Any

from dataplat.core.errors import AuthError, ServiceError

Notifier = Callable[[str], None]


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

    try:
        sts.get_caller_identity()
    except (
        botocore_exceptions.UnauthorizedSSOTokenError,
        botocore_exceptions.TokenRetrievalError,
        botocore_exceptions.SSOTokenLoadError,
        botocore_exceptions.NoCredentialsError,
    ):
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

    if profile:
        ensure_sso_login(profile, notify=notify)
        session = boto3.Session(profile_name=profile)
        return session.client(service_name, **kwargs)

    return boto3.client(service_name, **kwargs)
