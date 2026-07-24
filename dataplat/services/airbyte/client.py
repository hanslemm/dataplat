"""Airbyte API client helpers used by CLI adapters."""

from __future__ import annotations

import base64
import os
import time
from zoneinfo import ZoneInfo

import httpx
from croniter import croniter

from dataplat.core.errors import AuthError, ConfigError

_TOKEN_CACHE: dict[str, str | float | None] = {"token": None, "expires_at": 0.0}


def parse_jwt_exp(token: str) -> int | None:
    """Parse exp claim from a JWT without signature verification."""
    import json

    try:
        parts = token.split(".")
        if len(parts) < 2:
            return None
        payload_b64 = parts[1]
        padding = "=" * (-len(payload_b64) % 4)
        payload_json = base64.urlsafe_b64decode(payload_b64 + padding).decode("utf-8")
        payload = json.loads(payload_json)
        exp = payload.get("exp")
        return int(exp) if exp is not None else None
    except Exception:
        return None


def build_auth_headers(token: str) -> dict[str, str]:
    """Build request headers with configurable auth settings."""
    header_name = os.getenv("AIRBYTE_AUTH_HEADER") or "Authorization"
    raw_value = os.getenv("AIRBYTE_AUTH_VALUE")
    scheme = os.getenv("AIRBYTE_AUTH_SCHEME", "Bearer")

    if raw_value is not None:
        auth_value = raw_value
    elif scheme:
        auth_value = f"{scheme} {token}"
    else:
        auth_value = token

    headers = {
        header_name: auth_value,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    cookie = os.getenv("AIRBYTE_AUTH_COOKIE")
    if cookie:
        headers["Cookie"] = cookie

    return headers


def get_access_token(
    client: httpx.Client,
    base_url: str,
    client_id: str,
    client_secret: str,
) -> str:
    """Exchange app credentials for a short-lived bearer token."""
    now = time.time()
    cached_token = _TOKEN_CACHE.get("token")
    expires_raw = _TOKEN_CACHE.get("expires_at")
    expires_at = float(expires_raw) if isinstance(expires_raw, (int, float)) else 0.0
    if isinstance(cached_token, str) and cached_token and expires_at - 30 > now:
        return cached_token

    token_url = f"{base_url}/api/public/v1/applications/token"
    try:
        response = client.post(
            token_url,
            json={
                "client_id": client_id,
                "client_secret": client_secret,
                "grant_type": "client_credentials",
            },
            timeout=60,
        )

        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("location", "")
            raise AuthError(
                "Authentication redirected by gateway "
                f"(status={response.status_code}, location={location or 'unknown'})"
            )

        response.raise_for_status()

        content_type = response.headers.get("content-type", "").lower()
        if "text/html" in content_type or response.text.strip().startswith("<!"):
            raise AuthError(
                "Received HTML response from token endpoint; likely OAuth redirect"
            )

        payload = response.json() or {}
        token = payload.get("access_token")
        if not token:
            raise AuthError("No access_token in Airbyte token response")

        expires_in = payload.get("expires_in")
        exp = parse_jwt_exp(token)
        if expires_in:
            _TOKEN_CACHE["expires_at"] = now + int(expires_in)
        elif exp:
            _TOKEN_CACHE["expires_at"] = float(exp)
        else:
            _TOKEN_CACHE["expires_at"] = now + 900
        _TOKEN_CACHE["token"] = token

        return token
    except httpx.HTTPStatusError as exc:
        raise AuthError(
            f"Failed to get access token: {exc.response.status_code} {exc.response.reason_phrase}"
        ) from exc
    except (httpx.ConnectError, httpx.RemoteProtocolError) as exc:
        raise AuthError(f"Failed to connect to Airbyte token endpoint: {exc}") from exc
    except ValueError as exc:
        raise AuthError("Failed to parse Airbyte token response") from exc


def login_airbyte_oss(
    client: httpx.Client,
    base_url: str,
    email: str,
    password: str,
) -> str:
    """Login to Airbyte OSS and return JWT token."""
    login_url = f"{base_url}/api/public/v1/users/login"
    try:
        response = client.post(
            login_url,
            json={"email": email, "password": password},
            timeout=60,
        )
        response.raise_for_status()
        token = (response.json() or {}).get("token")
        if not token:
            raise AuthError("No token in Airbyte OSS login response")
        return token
    except httpx.HTTPStatusError as exc:
        raise AuthError(
            f"Failed to login to Airbyte OSS: {exc.response.status_code} {exc.response.reason_phrase}"
        ) from exc
    except (httpx.ConnectError, httpx.RemoteProtocolError) as exc:
        raise AuthError(f"Failed to connect to Airbyte OSS login endpoint: {exc}") from exc
    except ValueError as exc:
        raise AuthError("Failed to parse Airbyte OSS login response") from exc


def validate_cloud_env_vars() -> tuple[str, str, str]:
    """Validate required env vars for Airbyte Cloud auth."""
    base_url = (os.getenv("AIRBYTE_BASE_URL") or "").rstrip("/")
    client_id = os.getenv("AIRBYTE_CLIENT_ID") or ""
    client_secret = os.getenv("AIRBYTE_CLIENT_SECRET") or ""

    if not base_url or not client_id or not client_secret:
        raise ConfigError(
            "Set AIRBYTE_BASE_URL, AIRBYTE_CLIENT_ID, AIRBYTE_CLIENT_SECRET"
        )

    return base_url, client_id, client_secret


def validate_oss_env_vars() -> tuple[str, str, str]:
    """Validate required env vars for Airbyte OSS auth."""
    base_url = (os.getenv("AIRBYTE_BASE_URL") or "").rstrip("/")
    email = os.getenv("AIRBYTE_EMAIL") or ""
    password = os.getenv("AIRBYTE_PASSWORD") or ""

    if not base_url or not email or not password:
        raise ConfigError("Set AIRBYTE_BASE_URL, AIRBYTE_EMAIL, AIRBYTE_PASSWORD")

    return base_url, email, password


def build_authenticated_client() -> tuple[httpx.Client, str]:
    """Build an authenticated Airbyte client from env settings."""
    base_url = (os.getenv("AIRBYTE_BASE_URL") or "").rstrip("/")
    if not base_url:
        raise ConfigError("Set AIRBYTE_BASE_URL")

    email = os.getenv("AIRBYTE_EMAIL")
    password = os.getenv("AIRBYTE_PASSWORD")
    client_id = os.getenv("AIRBYTE_CLIENT_ID")
    client_secret = os.getenv("AIRBYTE_CLIENT_SECRET")

    # Default timeout guards every request (several endpoints used to pass no
    # timeout and could hang forever); transport retries cover connect blips.
    client = httpx.Client(
        follow_redirects=False,
        timeout=httpx.Timeout(60.0),
        transport=httpx.HTTPTransport(retries=2),
    )
    use_cloud = bool(client_id and client_secret)

    if not use_cloud and not (email and password):
        raise ConfigError(
            "For Airbyte Cloud, set AIRBYTE_CLIENT_ID and AIRBYTE_CLIENT_SECRET. "
            "For OSS, set AIRBYTE_EMAIL and AIRBYTE_PASSWORD"
        )

    try:
        if use_cloud:
            token = get_access_token(
                client,
                base_url,
                client_id or "",
                client_secret or "",
            )
        else:
            token = login_airbyte_oss(client, base_url, email or "", password or "")
    except AuthError as primary_error:
        if use_cloud and email and password:
            token = login_airbyte_oss(client, base_url, email or "", password or "")
        else:
            client.close()
            raise primary_error

    client.headers.update(build_auth_headers(token))
    return client, base_url


def validate_cron_expression(cron_expr: str) -> bool:
    """Validate a cron expression, including optional Quartz timezone suffix."""
    expr = cron_expr.strip()
    if not expr:
        return False

    parts = expr.split()
    if len(parts) >= 7:
        tz_candidate = parts[-1]
        if (
            any(ch.isalpha() for ch in tz_candidate)
            or "/" in tz_candidate
            or "_" in tz_candidate
        ):
            try:
                ZoneInfo(tz_candidate)
                parts = parts[:-1]
            except Exception:
                pass

    expr_no_tz = " ".join(parts).replace("?", "*")
    parts_no_tz = expr_no_tz.split()
    if len(parts_no_tz) not in (5, 6, 7):
        return False

    try:
        croniter(expr_no_tz, second_at_beginning=len(parts_no_tz) in (6, 7))
        return True
    except (ValueError, KeyError):
        return False


def split_cron_timezone(cron_expr: str) -> tuple[str, str | None]:
    """Split a Quartz cron expression into expression and timezone."""
    parts = cron_expr.strip().split()
    if len(parts) >= 7:
        tz_candidate = parts[-1]
        if (
            any(ch.isalpha() for ch in tz_candidate)
            or "/" in tz_candidate
            or "_" in tz_candidate
        ):
            try:
                ZoneInfo(tz_candidate)
                return " ".join(parts[:-1]), tz_candidate
            except Exception:
                pass
    return cron_expr, None


