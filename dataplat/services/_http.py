"""The one HTTP seam every service client goes through.

Two services grew the same plumbing independently: identical tracing hooks,
two ``_raise_for_status`` helpers, eighteen inline ``response.text[:500]``
snippets, and three different sentences for the same failure. Both client
modules said so in a comment — "they belong in a shared HTTP seam, which does
not exist yet" — for two releases. This is that seam.

What it owns is deliberately narrow: how a request is traced, and how a
failure is worded. Timeouts, retries and redirect policy stay with the caller,
because those are per-service facts (Airbyte retries connects and refuses
redirects; Superset does neither) and burying them here would hide them.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import httpx

from dataplat.core.errors import ServiceError
from dataplat.core.trace import is_enabled, trace_http

__all__ = [
    "build_client",
    "error_detail",
    "raise_for_status",
    "service_error",
    "trace_hooks",
]

# Two lines per request, not one: a request that never returns is the case
# worth seeing, and a single line logged on completion cannot show it.
_TRACE_STARTED = "_dp_trace_started"

_DETAIL_LIMIT = 400
_UNPARSED = object()


def trace_hooks() -> dict[str, list[Callable[..., None]]]:
    """Event hooks tracing method, URL, status and duration to stderr.

    Headers are never read, so the ``Authorization: Bearer …`` a client sends
    cannot reach the trace.
    """

    def on_request(request: httpx.Request) -> None:
        if not is_enabled():
            return
        setattr(request, _TRACE_STARTED, time.perf_counter())
        trace_http(request.method, str(request.url))

    def on_response(response: httpx.Response) -> None:
        if not is_enabled():
            return
        started = getattr(response.request, _TRACE_STARTED, None)
        trace_http(
            response.request.method,
            str(response.request.url),
            status=response.status_code,
            elapsed_ms=None
            if started is None
            else (time.perf_counter() - started) * 1000,
        )

    return {"request": [on_request], "response": [on_response]}


def build_client(**kwargs: object) -> httpx.Client:
    """The one way a service gets an HTTP client.

    It exists so tracing holds by construction: a command that calls
    ``httpx.Client()`` directly still works and silently traces nothing, which
    is the failure mode of an opt-in diagnostic — quietly absent exactly where
    it was needed. Everything else is passed through untouched.
    """
    return httpx.Client(event_hooks=trace_hooks(), **kwargs)  # type: ignore[arg-type]


def _join(value: object) -> str:
    """Flatten one field's errors; an API sends a list even for a single one."""
    if isinstance(value, list):
        return "; ".join(str(item) for item in value)
    return str(value)


def error_detail(response: httpx.Response) -> str:
    """The server's own account of a failure, as one capped line.

    Only the body is read, so the ``Authorization`` header stays as unreachable
    from an error message as it is from the trace.
    """
    try:
        payload: object = response.json()
    except ValueError:
        payload = _UNPARSED

    message = payload.get("message") if isinstance(payload, dict) else None

    if isinstance(message, dict):
        # A schema rejection arrives per field: {"password": ["too short"]}.
        text = "; ".join(f"{k}: {_join(v)}" for k, v in sorted(message.items()))
    elif message is not None:
        text = _join(message)
    elif payload is _UNPARSED or payload:
        # No recognised envelope: a gateway's HTML or a bare string still beats
        # a status line. An empty body ({} or []) says nothing and is left out.
        text = response.text
    else:
        text = ""

    text = " ".join(text.split())
    if len(text) > _DETAIL_LIMIT:
        text = text[:_DETAIL_LIMIT].rstrip() + "…"
    return text


def service_error(response: httpx.Response, action: str) -> ServiceError:
    """The one shape a failed call reports, the server's reason included."""
    detail = error_detail(response)
    return ServiceError(
        f"Failed to {action} ({response.status_code} {response.reason_phrase})"
        + (f": {detail}" if detail else "")
    )


def raise_for_status(response: httpx.Response, action: str) -> None:
    """Turn a failed response into a :class:`ServiceError` naming ``action``."""
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise service_error(exc.response, action) from exc
