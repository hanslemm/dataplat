"""Pure helpers for classifying and rewriting Airbyte cursor state values.

No I/O — safe to unit-test in isolation. A cursor's saved state value is
connector-defined JSON. Two kinds of cursor are rewritten:

* "date" cursors — values that are ISO-8601 date/datetime *strings*. Rewritten
  to a target date (format-preserving).
* "xmin" cursors — a Postgres stream state object whose ``state_type == "xmin"``,
  carrying a transaction-id (``xmin_xid_value`` / ``xmin_raw_value``). These are
  numbers, not dates, so a calendar date can't drive them; instead they are set
  to an absolute value or scaled by a factor.

Everything else — numeric/epoch cursors, LSN/CDC objects, non-ISO strings — is an
"opaque" cursor and is left alone.
"""
from __future__ import annotations

import copy
from datetime import date, datetime
from typing import Any, Literal

CursorKind = Literal["date", "opaque"]


def _parse_iso(value: str) -> datetime | date | None:
    """Return a parsed datetime/date if `value` is ISO 8601, else None."""
    text = value.strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(text)
        # datetime.fromisoformat("2024-06-01") succeeds (returns a datetime),
        # so we try date first to preserve date-only format classification.
    except ValueError:
        return None


def parse_target_date(text: str) -> date | None:
    """Parse `--to` input into a calendar date, or None if not ISO-parseable."""
    parsed = _parse_iso(text)
    if parsed is None:
        return None
    return parsed.date() if isinstance(parsed, datetime) else parsed


def _cursor_date(value: str) -> date:
    """Calendar date of a date-classified cursor value.

    Callers must only pass values already classified as "date", so the ISO
    parse cannot fail here.
    """
    parsed = _parse_iso(value)
    assert parsed is not None
    return parsed.date() if isinstance(parsed, datetime) else parsed


def classify_cursor_value(value: Any) -> CursorKind:
    """Classify a cursor state value by shape.

    "date" iff `value` is a string that parses as an ISO 8601 date/datetime;
    everything else is "opaque". (bool/None/numbers are not str, so they fall
    through to "opaque".)
    """
    if isinstance(value, str) and _parse_iso(value) is not None:
        return "date"
    return "opaque"


def rewrite_date(old_value: str, target: date) -> str:
    """Re-serialize `target` in the same textual format as `old_value`.

    String-level date-prefix replacement: only the leading calendar-date token
    is replaced, and the entire remainder of the string (time-of-day,
    fractional seconds, timezone suffix, separator) is kept byte-for-byte. We
    do not re-parse and re-serialize via `datetime.isoformat()`, since that
    normalizes formats (e.g. ".123Z" -> ".123000Z", "+0200" -> "+02:00") in
    ways that can break connectors that compare cursor strings lexicographically.
    """
    text = old_value.strip()
    sep_index = None
    for i, ch in enumerate(text):
        if ch in ("T", "t", " "):
            sep_index = i
            break
    if sep_index is not None:
        date_part, remainder = text[:sep_index], text[sep_index:]
    else:
        date_part, remainder = text, ""
    if "-" in date_part:
        new_date = f"{target.year:04d}-{target.month:02d}-{target.day:02d}"
    else:
        new_date = f"{target.year:04d}{target.month:02d}{target.day:02d}"
    return new_date + remainder


def rewrite_xmin(
    old_value: Any, *, xmin_value: int | None = None, xmin_factor: float | None = None
) -> int:
    """Compute a new xmin transaction id.

    Exactly one of `xmin_value` (absolute) or `xmin_factor` (multiplicative —
    e.g. 0.1 rewinds to 10% of the current xid, 0 forces a full re-read) is used;
    `xmin_value` wins if both are somehow set. When scaling, `old_value` must be
    coercible to int, else TypeError/ValueError propagates to the caller.
    """
    if xmin_value is not None:
        return int(xmin_value)
    if xmin_factor is None:
        raise ValueError("either xmin_value or xmin_factor must be given")
    return int(round(int(old_value) * xmin_factor))


def _plan_xmin_stream(
    inner: dict,
    name: str,
    namespace: str | None,
    *,
    xmin_value: int | None,
    xmin_factor: float | None,
    only_rewind: bool = False,
) -> dict:
    """Rewrite an xmin stream's xid fields in place; return its action row.

    `xmin_xid_value` and `xmin_raw_value` are updated together; `version` and
    `num_wraparound` are left untouched (so a rewound xid keeps its wraparound
    accounting). With no xmin op requested — or a non-numeric payload — the
    stream is left alone and reported as "skip:xmin". With `only_rewind`, a
    rewrite that would *raise* the xid is skipped as "skip:advance".
    """
    old_xid = inner.get("xmin_xid_value")
    if xmin_value is None and xmin_factor is None:
        action = "skip:xmin"
    else:
        try:
            if only_rewind and old_xid is not None and (
                rewrite_xmin(
                    old_xid, xmin_value=xmin_value, xmin_factor=xmin_factor
                ) > int(old_xid)
            ):
                action = "skip:advance"
            else:
                for xkey in ("xmin_xid_value", "xmin_raw_value"):
                    if inner.get(xkey) is not None:
                        inner[xkey] = rewrite_xmin(
                            inner[xkey], xmin_value=xmin_value, xmin_factor=xmin_factor
                        )
                action = "rewrite:xmin"
        except (TypeError, ValueError):
            action = "skip:xmin"
    return {
        "stream": name, "namespace": namespace, "key": "xmin",
        "old": old_xid,
        "new": inner.get("xmin_xid_value") if action == "rewrite:xmin" else old_xid,
        "action": action,
    }


def plan_cursor_rewrites(
    connection_state: dict,
    target: date | None = None,
    *,
    xmin_value: int | None = None,
    xmin_factor: float | None = None,
    only_rewind: bool = False,
) -> tuple[dict, list[dict]]:
    """Return (new_state, actions).

    Deep-copies `connection_state` and, for each stream under
    `streamState[].streamState`:

    * xmin streams (inner ``state_type == "xmin"``) are rewritten when an xmin op
      is given (`xmin_value` absolute, or `xmin_factor` scaling the current xid);
      with no xmin op they are skipped.
    * every other stream has its date-shaped cursor values rewritten to `target`
      (format-preserving) when `target` is given; numeric/CDC ("opaque") values
      are always left alone.

    With `only_rewind`, cursors are never moved forward: a date cursor whose
    current value is *earlier* than `target` — or an xmin whose xid would
    *increase* — is left alone and reported as "skip:advance". Moving a cursor
    forward makes the next sync silently skip every record between the old and
    new positions, so bulk rewinds usually want this guard.

    When a stream's date cursor is rewritten and it carries a boundary-dedup
    `cursor_record_count`, that count is reset to 0 (the rewound cursor has
    emitted no rows at the new boundary).

    Records one action row per cursor: {stream, namespace, key, old, new, action},
    where action is "rewrite", "rewrite:xmin", "reset:count", "skip:date",
    "skip:xmin", "skip:advance" or "skip:opaque". Passing `target` together with
    an xmin op fixes date streams and xmin streams in a single pass. Non-stream
    state (global/legacy/not_set) yields zero actions and an unchanged copy.
    """
    new_state = copy.deepcopy(connection_state)
    actions: list[dict] = []
    for entry in new_state.get("streamState") or []:
        if not isinstance(entry, dict):
            continue
        descriptor = entry.get("streamDescriptor") or {}
        name = descriptor.get("name", "")
        namespace = descriptor.get("namespace")
        inner = entry.get("streamState")
        if not isinstance(inner, dict):
            continue

        if inner.get("state_type") == "xmin":
            actions.append(
                _plan_xmin_stream(
                    inner, name, namespace,
                    xmin_value=xmin_value, xmin_factor=xmin_factor,
                    only_rewind=only_rewind,
                )
            )
            continue

        rewrote_date = False
        for key, value in list(inner.items()):
            if key == "cursor_record_count":
                continue  # decided after the loop, based on whether the cursor moved
            if classify_cursor_value(value) == "date":
                if target is None:
                    actions.append({
                        "stream": name, "namespace": namespace, "key": key,
                        "old": value, "new": value, "action": "skip:date",
                    })
                    continue
                if only_rewind and _cursor_date(value) < target:
                    actions.append({
                        "stream": name, "namespace": namespace, "key": key,
                        "old": value, "new": value, "action": "skip:advance",
                    })
                    continue
                new_value = rewrite_date(value, target)
                inner[key] = new_value
                rewrote_date = True
                actions.append({
                    "stream": name, "namespace": namespace, "key": key,
                    "old": value, "new": new_value, "action": "rewrite",
                })
            else:
                actions.append({
                    "stream": name, "namespace": namespace, "key": key,
                    "old": value, "new": value, "action": "skip:opaque",
                })

        # A rewound cursor has emitted no rows at the new boundary, so the
        # boundary-dedup counter must reset to 0 — otherwise the next sync would
        # skip that many real rows whose cursor value equals the new cursor.
        if "cursor_record_count" in inner:
            old_count = inner["cursor_record_count"]
            if rewrote_date and old_count != 0:
                inner["cursor_record_count"] = 0
                actions.append({
                    "stream": name, "namespace": namespace,
                    "key": "cursor_record_count",
                    "old": old_count, "new": 0, "action": "reset:count",
                })
            else:
                actions.append({
                    "stream": name, "namespace": namespace,
                    "key": "cursor_record_count",
                    "old": old_count, "new": old_count, "action": "skip:opaque",
                })
    return new_state, actions
