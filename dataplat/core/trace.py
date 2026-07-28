"""An opt-in tracer for the one question logs cannot answer: what did we send?

Every defect in this tool that took more than an hour to find came down to the
same gap — the SQL or the HTTP request that actually reached the server was
invisible, so debugging meant reading code and guessing. ``--verbose`` (or
``DP_VERBOSE=1``) makes it visible, and this module is the only place that
decides how.

Three rules shape it:

*Stderr, never stdout.* ``--json`` and ``--format csv`` exist so another
program can read the output; a tracer that wrote one line to stdout would break
every one of those callers. Nothing here touches stdout, so ``dp ... --json
--verbose | jq`` stays valid and the human still sees the trace.

*Silent when off, at zero cost.* Every entry point returns before it formats
anything. Callers whose message is expensive to build should still guard with
:func:`is_enabled` — the check is the cheap part.

*Redacted, always.* Every message passes through :func:`redact` on the way out,
because the strings we most want to see are exactly the ones that carry
credentials: a libpq conninfo, an ``Authorization`` header, and — the reason
this is not a print statement — ``CREATE ROLE x LOGIN PASSWORD '...'``, which
:mod:`dataplat.services.db.role_dialects` builds and sends verbatim.

What is deliberately NOT traced
===============================

*Parameter values.* A statement plus "2 params bound" is what you debug; the
values are warehouse data — names, emails, whatever the row held — and they are
also the one thing psycopg keeps out of the SQL for a reason. We say how many
were bound and nothing about what they were.

*Result rows and response bodies.* Same data problem, plus volume: a trace that
scrolls the answer off the screen has hidden the request it exists to show.

*Credentials, in any spelling we can recognize.* See :func:`redact`. That is a
net, not a proof — the discipline is to trace metadata and let redaction be the
second line of defence, not the first.

*Nothing at all when disabled.* No buffering, no "collect in case someone asks
later". If it was not enabled when the statement ran, it is gone.
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Iterator, Sized
from contextlib import contextmanager

__all__ = [
    "CATEGORY_HTTP",
    "CATEGORY_SQL",
    "VERBOSE_ENV_VAR",
    "disable",
    "enable",
    "is_enabled",
    "redact",
    "trace",
    "trace_http",
    "trace_sql",
    "verbose",
]

VERBOSE_ENV_VAR = "DP_VERBOSE"

# Categories are a prefix, not a level: `[dp:sql]` so `dp ... --verbose 2>&1 |
# grep '\[dp:sql\]'` is the whole filtering story. Areas that need a third one
# pass their own string; these two are named because they are the ones with
# dedicated helpers below.
CATEGORY_SQL = "sql"
CATEGORY_HTTP = "http"


# --- the switch -------------------------------------------------------------


def _truthy(raw: str | None) -> bool:
    """Lenient reader: anything but an explicit negative enables tracing.

    Failing towards *on* is the safe direction here. A typo'd ``DP_VERBOSE=ture``
    that silently stayed off would cost a debugging session and look like the
    tracer is broken; the same typo turning diagnostics on costs a few lines of
    stderr. (Same semantics as the test harnesses' ``truthy``, on purpose — one
    rule for boolean environment variables in this project.)
    """
    if raw is None:
        return False
    return raw.strip().lower() not in {"", "0", "false", "no", "off"}


# Read once, at import: the flag is process state, so a later os.environ change
# does not retroactively decide whether earlier statements were traced. The root
# `--verbose` option calls enable() instead, which is the same switch reached a
# few milliseconds later.
_enabled: bool = _truthy(os.environ.get(VERBOSE_ENV_VAR))


def _set(value: bool) -> None:
    global _enabled
    _enabled = value


def enable() -> None:
    """Turn tracing on for the rest of the process. Idempotent."""
    _set(True)


def disable() -> None:
    """Turn tracing off again.

    The CLI never calls this — verbosity is a one-way switch for a real run.
    It exists so a test (or a long-lived host such as the Airbyte TUI) can put
    the process back the way it found it instead of leaking global state into
    whatever runs next. Prefer :func:`verbose` when the scope is a block.
    """
    _set(False)


def is_enabled() -> bool:
    """Whether tracing is on, for callers whose message is costly to build."""
    return _enabled


@contextmanager
def verbose() -> Iterator[None]:
    """Enable tracing for the duration of the block, then restore the flag.

    The leak-free way to exercise tracing from a test: a bare ``enable()`` in
    one test makes an unrelated one start writing to stderr.
    """
    previous = _enabled
    enable()
    try:
        yield
    finally:
        _set(previous)


# --- redaction --------------------------------------------------------------
# Lifted from tests/integration/redshift/conftest.py's redact() -- which covers
# the libpq URL and keyword password forms -- and extended, because a trace sees
# more than a conninfo: bearer tokens, API keys, client secrets, and the SQL
# `PASSWORD 'literal'` form (no `=`) that role creation emits. Kept here rather
# than imported from the tests so the production path does not depend on them.
#
# False positives are the intended failure direction. Masking the word after a
# stray "token" in prose costs a reader nothing; the reverse costs a credential
# in a CI log that is retained for a year.

_MASK = "***"

# Credentials in a URL: postgresql://user:pw@host/db, https://user:pw@host/.
# The character classes stop at `/?#` so a path or query can never be mistaken
# for a password, and an `@`-less URL (postgresql://host:5432/db) cannot match.
_URL_PASSWORD_RE = re.compile(r"(?P<scheme>://[^:/?#@\s]+):[^@/?#\s]*@")

# A value after `key=` or `"key":`. Quoted forms win so a password containing
# spaces is fully consumed; the bare form stops at the separators that end a
# value in a conninfo (space), a URL query (&) or a serialized mapping (,;).
_VALUE = r"'(?:[^']|'')*'|\"[^\"]*\"|[^\s&;,]*"

# `password=x`, `PGPASSWORD=x`, `sslpassword='x y'` -- and the SQL form
# `PASSWORD 'x'`, which has no delimiter at all. A quoted literal is therefore
# accepted with or without one, while an unquoted value requires `=`/`:` so
# ordinary prose ("Password set: yes", "set AIRBYTE_PASSWORD") survives intact.
_PASSWORD_RE = re.compile(
    rf"(?P<key>\b[a-z_]*password\b\"?\s*)"
    rf"(?:(?P<delim>[=:]\s*)(?P<value>{_VALUE})"
    rf"|(?P<literal>'(?:[^']|'')*'|\"[^\"]*\"))",
    re.IGNORECASE,
)

# Anything whose *name* says it is a credential. Substring matching on the key
# (`AIRBYTE_CLIENT_SECRET`, `x-api-key`, `access_token`, `"client_secret":`) is
# what makes this hold for services nobody has written an adapter for yet.
_SECRET_KEY = (
    r"[a-z0-9_.\-]*"
    r"(?:secret|token|api[_\-]?key|access[_\-]?key|credential|passwd|pwd)"
    r"[a-z0-9_.\-]*"
)
_SECRET_RE = re.compile(
    rf"(?P<key>\b{_SECRET_KEY}\b\"?\s*)(?P<delim>[=:]\s*)(?P<value>{_VALUE})",
    re.IGNORECASE,
)

# An Authorization header's value is opaque, so it is masked whole -- scheme
# included. Unquoted, it runs to the end of the field rather than the next
# space, or `Authorization: Bearer abc` would keep `abc`. `|` terminates it
# because that is how the helpers below join a message.
_AUTH_VALUE = r"'[^']*'|\"[^\"]*\"|[^,;|]+"
_AUTH_HEADER_RE = re.compile(
    rf"(?P<key>\bauthorizations?\b\"?\s*)(?P<delim>[=:]\s*)(?P<value>{_AUTH_VALUE})",
    re.IGNORECASE,
)

# The scheme-and-space form, wherever it appears -- a curl line someone pasted,
# an httpx repr, an error message quoting the request. `token` covers the
# GitHub-style `Authorization: token ghp_...`.
_BEARER_RE = re.compile(
    r"\b(?P<scheme>bearer|basic|token)\s+(?P<value>[^\s,;|'\"]+)",
    re.IGNORECASE,
)


def _mask_value(match: re.Match[str]) -> str:
    """Keep the key and its delimiter; replace whatever followed."""
    delim = match.groupdict().get("delim") or ""
    return f"{match.group('key')}{delim}{_MASK}"


def redact(text: str) -> str:
    """Strip anything credential-shaped out of a message about to be written.

    Recognizes, in order: credentials embedded in a URL; an ``Authorization``
    header (masked whole, scheme included, since none of it is diagnostic); the
    ``Bearer``/``Basic``/``token`` prefix form; any ``key=value`` whose key
    names a secret, token, API key or credential; and every password spelling,
    including SQL's delimiter-free ``PASSWORD 'literal'``.

    It cannot recognize a bare secret with nothing around it — a raw token
    passed as a positional argument looks like any other word. That is why the
    rule for callers is to trace metadata, not values: this function is the
    second line of defence, not the first.
    """
    text = _URL_PASSWORD_RE.sub(rf"\g<scheme>:{_MASK}@", text)
    text = _AUTH_HEADER_RE.sub(_mask_value, text)
    text = _BEARER_RE.sub(rf"\g<scheme> {_MASK}", text)
    text = _SECRET_RE.sub(_mask_value, text)
    return _PASSWORD_RE.sub(_mask_value, text)


# --- writing ----------------------------------------------------------------

_WHITESPACE_RE = re.compile(r"\s+")


def _one_line(text: str) -> str:
    """Collapse whitespace so one trace is one greppable line.

    Multi-line SQL constants are the norm in this codebase, and a trace whose
    second line has no ``[dp:sql]`` prefix is invisible to the grep the prefix
    exists for. Collapsing before redaction also matters: a keyword and its
    value split across two lines are put back into a shape the patterns above
    can still see.
    """
    return _WHITESPACE_RE.sub(" ", text).strip()


def trace(category: str, message: str) -> None:
    """Write one redacted, single-line ``[dp:<category>]`` record to stderr."""
    if not _enabled:
        return
    # sys.stderr is resolved per call, not captured at import: pytest's capsys
    # and any host that redirects streams replace the attribute, not the file.
    sys.stderr.write(f"[dp:{category}] {redact(_one_line(message))}\n")


def _params_note(params: object) -> str:
    """Say whether parameters were bound — never what they were.

    ``Sized`` covers both shapes psycopg accepts (a sequence and a mapping), so
    the count is free; anything else gets the honest "bound" without being
    consumed, since a generator would be destroyed by looking.
    """
    if params is None:
        return "no params"
    if isinstance(params, Sized):
        return f"{len(params)} params bound"
    return "params bound"


def trace_sql(
    statement: str,
    *,
    params: object = None,
    target: str | None = None,
    elapsed_ms: float | None = None,
) -> None:
    """Trace a statement: the SQL text, whether params were bound, how long.

    ``statement`` must be SQL *text*. Pass ``Composed.as_string(cursor)`` for a
    composed statement, never the object itself: its repr is a Python data
    structure (``Literal('s3cr3t')``), which is neither what the server sees nor
    something :func:`redact` can read — a password inside one would survive.

    ``target`` is the dataplat target name, worth having when a command talks to
    two warehouses in one run. ``elapsed_ms`` is what turns this from "what did
    we send" into "and which statement was slow".
    """
    if not _enabled:
        return
    pieces = [] if target is None else [target]
    pieces += [_one_line(statement), _params_note(params)]
    if elapsed_ms is not None:
        pieces.append(f"{elapsed_ms:.1f}ms")
    trace(CATEGORY_SQL, " | ".join(pieces))


def trace_http(
    method: str,
    url: str,
    *,
    status: int | None = None,
    elapsed_ms: float | None = None,
) -> None:
    """Trace a request: method, URL, and — when known — status and duration.

    Call it once after the response so all four appear on one line; call it
    before instead (``status=None``) when the request may never return, which is
    the case a hanging command needs traced.

    No headers and no body: the header worth seeing is the one that carries the
    token, and the body is the data. A query string is redacted like everything
    else, so an ``?api_key=`` in a URL is safe to pass.
    """
    if not _enabled:
        return
    pieces = [f"{method.upper()} {url}"]
    if status is not None:
        pieces.append(f"-> {status}")
    if elapsed_ms is not None:
        pieces.append(f"{elapsed_ms:.1f}ms")
    # " | ", not " ": the redactor masks the word after a bare `token`, because
    # `Authorization: token ghp_...` is exactly that shape. Joined with a space,
    # a URL whose path ends in /token — Airbyte's own token endpoint does —
    # turned the following "-> 200" into "***", so the status vanished from a
    # line that leaked nothing. `|` is excluded from that pattern's value class,
    # so the separator ends the match instead of being eaten.
    trace(CATEGORY_HTTP, " | ".join(pieces))
