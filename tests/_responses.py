"""Real HTTP responses for tests, instead of stand-ins for them.

Nine test modules each hand-rolled a class that duck-typed ``httpx.Response``:
its own ``json()``, ``text``, ``headers``, ``raise_for_status``, and later its
own ``reason_phrase`` when production started reading one. They drifted, and
the drift hid a bug rather than causing one — a fake whose ``json()`` returned
``None`` for a body that was not JSON, where a real response raises, meaning
the error body never reached the code under test *or* the assertions about it.

A real response cannot drift from itself. The only thing worth a helper is the
request object, which ``raise_for_status`` needs to build its error and which
httpx will not invent for you.
"""

from __future__ import annotations

from typing import Any

import httpx

__all__ = ["response"]


def response(
    data: Any = None,
    status_code: int = 200,
    *,
    text: str | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    """An ``httpx.Response`` carrying ``data`` as JSON, or ``text`` verbatim.

    Passing ``text`` that is not JSON is how a test says "the server answered
    with something unparseable": the resulting ``json()`` raises exactly as the
    real one does, which is what the callers are written to handle.
    """
    request = httpx.Request("GET", "http://test")
    if text is not None:
        return httpx.Response(status_code, text=text, headers=headers, request=request)
    return httpx.Response(
        status_code,
        json=data if data is not None else {},
        headers=headers,
        request=request,
    )
