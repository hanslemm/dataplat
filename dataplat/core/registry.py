"""The area registry: the single seam between the CLI shell and its areas.

An area is a Typer app plus (optionally) a dependency contract. ``main``
mounts whatever this module returns and knows nothing about individual
areas; areas know nothing about mounting.

Third-party areas arrive the same way, through a packaging entry point:

.. code-block:: toml

    [project.entry-points."dataplat.areas"]
    widget = "widget_dp.cli:app"

The name on the left becomes ``dp widget``; the value on the right is the
``module:attr`` target :func:`load_app` imports. That is the entire contract,
and it is a *string* rather than an :class:`AreaMount` on purpose:

- **Discovery imports nothing.** ``dp --help`` lists every area without
  importing one (see :mod:`dataplat.cli._lazy`), and an entry point resolving to
  an ``AreaMount`` would end that — reading the mount means importing the
  plugin, so one plugin that pulls in boto3 puts a quarter of a second back onto
  every invocation, and one that raises on import breaks ``dp --help`` itself.
- **A plugin does not import dataplat in order to be a plugin.** No dataclass to
  construct, and no version skew the day a field is added here.

What a plugin consequently cannot declare, and why that is the right trade:

- *Help text.* Taken from the distribution's ``Summary`` — the author's own
  words, already written, and one metadata read to fetch. Once the area is
  imported its own Typer help takes over, so this line only has to hold up in
  the root listing.
- *A dependency contract* (:class:`~dataplat.core.deps.AreaDeps`). Plugin mounts
  get ``deps=None``: that machinery models *our* optional extras, and its
  installer prescribes ``dataplat[<extra>]``, which for a third party's extra
  would be a fabricated command. A plugin's dependencies are its own
  distribution's requirements, installed with it. ``AreaMount`` keeps the field
  for a mount built in code — an embedder shipping extras of *this*
  distribution, which is who it was always for.

Refusal, not shadowing, is the rule for a name clash: see :func:`plugin_areas`.
"""

from __future__ import annotations

import importlib
import re
import sys
from dataclasses import dataclass
from functools import cache
from importlib import metadata
from typing import Any

from dataplat.core.deps import AREAS, AreaDeps, ready


@dataclass(frozen=True)
class AreaMount:
    """Everything needed to mount one area on the root command."""

    name: str
    help_text: str
    # Where the area's Typer app lives, as "module:attr".
    target: str
    # Optional-dependency contract; None means always available.
    deps: AreaDeps | None


BUILTIN_AREAS: tuple[AreaMount, ...] = (
    AreaMount(
        name="db",
        help_text="Database query commands",
        target="dataplat.cli.db:app",
        deps=AREAS["db"],
    ),
    AreaMount(
        name="ingest",
        help_text="Data ingestion tools (Airbyte)",
        target="dataplat.cli.ingest.app:app",
        deps=AREAS["ingest"],
    ),
    AreaMount(
        name="bi",
        help_text="Business-intelligence tools (Superset)",
        target="dataplat.cli.bi.app:app",
        deps=AREAS["bi"],
    ),
    AreaMount(
        name="cloud",
        help_text="Cloud-provider tools (AWS)",
        target="dataplat.cli.cloud.app:app",
        deps=AREAS["cloud"],
    ),
    AreaMount(
        name="ci",
        help_text="CI tools (GitHub Actions runners)",
        target="dataplat.cli.ci.app:app",
        deps=None,
    ),
)


# The entry-point group a distribution declares its areas in. Prefixed with the
# distribution name, not the command name (`dataplat.areas`, not `dp.areas`):
# groups are one flat global namespace shared by every installed package, and the
# distribution name is the only part of it nobody else can claim. `.areas`
# because "area" is the word this module, `AreaMount`, `AreaDeps` and `mount_help`
# already use for the thing being declared — `dataplat.plugins` would be a second
# name for one concept.
PLUGIN_GROUP = "dataplat.areas"

# An area name has to work as both a shell word and a click command name, so the
# accepted set is narrower than "whatever the entry point said": lowercase,
# digits, dash and underscore, starting alphanumeric. `dbt-orphans` shows the
# dash is house style; a name with a space would be unreachable from a shell and
# one starting with a dash would parse as an option.
_AREA_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

# "module:attr" — what load_app can actually import. Matched against the raw
# entry-point value and never through ``EntryPoint.module``/``.attr``: those
# assert on a malformed value, so a third party's typo would reach the user as a
# bare AssertionError from inside the standard library.
#
# This also refuses the legacy ``module:attr [extra]`` spelling, rather than
# accepting it and dropping the marker the way modern tooling does. The marker
# says "this entry point needs these extras of my distribution", and dp installs
# nothing for a plugin — silently ignoring a requirement its author wrote down is
# worse than saying the value is not a shape we take.
_TARGET_RE = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*$")

# A plugin's help line shares a table with the built-ins' single phrases; a
# Summary written as a paragraph would push the other areas off the screen.
_HELP_MAX = 60


def warn_plugin(message: str) -> None:
    """Write one ``dp: <message>`` line about a third-party area to stderr.

    Stderr, never stdout, for the same reason :mod:`dataplat.core.trace` uses it:
    discovery happens during ordinary commands, so a warning on stdout would
    land in the middle of the JSON or CSV a caller is parsing.

    Written raw rather than through a Rich console because every value in these
    messages — a distribution name, an entry-point value, an exception's text —
    comes from a third party, and Rich reads a ``[/x]`` in any of them as markup
    and raises mid-render (see :mod:`dataplat.cli._render`). Plain text has no
    such failure mode, and a warning that crashes the tool is worse than no
    warning at all. ``sys.stderr`` is resolved per call so a host that redirects
    the stream is honoured.
    """
    sys.stderr.write(f"dp: {message}\n")


def _one_line(text: str) -> str:
    """Collapse whitespace, so one diagnostic is one greppable line."""
    return " ".join(text.split())


def _reason(exc: BaseException) -> str:
    """``TypeName: message`` on one line — enough to act on, and no traceback."""
    return _one_line(f"{type(exc).__name__}: {exc}")


def warn_plugin_failed(mount: AreaMount, exc: BaseException) -> None:
    """Report a third-party area that would not import.

    Kept here beside the discovery warnings rather than at the call site in
    :mod:`dataplat.cli._lazy`, so every sentence dp says about a plugin has one
    shape and one stream.
    """
    warn_plugin(f"area {mount.name!r} ({mount.target}) failed to load: {_reason(exc)}")


def _origin(ep: metadata.EntryPoint) -> str:
    """Which distribution declared ``ep``, so a warning can name a culprit.

    Nothing in here may raise, and nothing may fill a gap with ``None``:
    ``dist.name`` and ``dist.version`` both parse METADATA and both answer
    ``None`` when it is unreadable, so "None None" is a real outcome to guard
    against — the whole point of the message is that discovery survived a
    distribution being wrong.
    """
    dist = ep.dist
    if dist is None:
        return "an unknown distribution"
    # Annotated as optional against the stubs' promise of `str`: both are header
    # lookups, and an absent header answers None today and raises tomorrow.
    name: str | None
    version: str | None
    try:
        name, version = dist.name, dist.version
    except Exception:
        name = version = None
    if not name:
        return "a distribution with unreadable metadata"
    return _one_line(f"{name} {version}" if version else name)


def _plugin_help(ep: metadata.EntryPoint) -> str:
    """``ep``'s help line, read from metadata instead of from the area itself.

    The root listing has to be answerable without importing the plugin, which
    leaves the distribution ``Summary`` as the only description available — and
    it is the right one: the author wrote it, and it costs a single metadata
    read. Failing that, name the distribution, which at least answers "why does
    my dp have a `widget` command". Old setuptools wrote ``Summary: UNKNOWN``
    when there was none, and printing that as help would be a defect no reader
    could explain.
    """
    dist = ep.dist
    label: str | None = None
    summary = ""
    if dist is not None:
        try:
            label = dist.name
            # .get, not ["Summary"]: a missing header returns None from
            # __getitem__ only under a DeprecationWarning, and is documented to
            # start raising KeyError.
            summary = _one_line(dist.metadata.get("Summary", ""))
        except Exception:
            # Broken metadata is the fallback's job, not an error of its own:
            # this area may still import and run perfectly.
            pass
    if summary and summary.upper() != "UNKNOWN":
        return summary if len(summary) <= _HELP_MAX else summary[: _HELP_MAX - 1] + "…"
    return f"Provided by {label}" if label else "Third-party area"


@cache
def plugin_areas() -> tuple[AreaMount, ...]:
    """Areas declared by other distributions, refused ones dropped, name-ordered.

    Cached for the life of the process for three reasons: the scan costs
    milliseconds and several call sites ask (2 ms in a 41-distribution
    virtualenv, more with a longer ``sys.path``); installed metadata cannot
    change under a running command; and a refused plugin must warn once, not once
    per lookup. A test that changes what is installed calls
    ``plugin_areas.cache_clear()``.

    Sorted by name because the scan order is the order the filesystem happens to
    list ``sys.path`` in — stable on one machine, arbitrary between two, and
    ``dp --help`` should not reorder itself when a colleague runs it.

    Three kinds of refusal, each deliberate:

    *A built-in's name is never given away.* ``dp db`` is the command people have
    muscle memory for; letting any installed distribution redefine it would make
    an accident and a supply-chain attack look identical from the outside.

    *Two distributions claiming one name get neither.* The scan order that would
    pick a winner is the filesystem's, so "first wins" means "whoever happened to
    be listed first", differing between machines. And the loser fails as a
    handful of missing subcommands inside an area that otherwise works — far
    harder to diagnose than an area that is plainly absent with a line saying
    why. This is the standing "prefer unknown to a confident falsehood" rule
    (CONTRIBUTING.md) applied to mounting.

    *A name or target that cannot work is dropped at discovery*, not left to fail
    later: ``dp --help`` should not advertise an area that provably cannot run,
    and the warning still has the entry point in hand to name the distribution.
    """
    try:
        entries = list(metadata.entry_points(group=PLUGIN_GROUP))
    except Exception as exc:
        # One unreadable distribution anywhere on sys.path must not take the tool
        # with it. The built-ins are what dp *is*; plugins are what it grew.
        warn_plugin(
            f"could not read plugin areas, using built-ins only: {_reason(exc)}"
        )
        return ()

    builtin = {mount.name for mount in BUILTIN_AREAS}
    accepted: dict[str, AreaMount] = {}
    claimed_by: dict[str, str] = {}
    for ep in entries:
        origin = _origin(ep)
        if not _AREA_NAME_RE.match(ep.name):
            warn_plugin(
                f"ignoring plugin area {ep.name!r} from {origin}: "
                f"not a usable area name"
            )
            continue
        if ep.name in builtin:
            warn_plugin(
                f"ignoring plugin area {ep.name!r} from {origin}: "
                f"dp {ep.name} is a built-in area"
            )
            continue
        if not _TARGET_RE.match(ep.value):
            warn_plugin(
                f"ignoring plugin area {ep.name!r} from {origin}: "
                f"{ep.value!r} is not module:attr"
            )
            continue
        if ep.name in claimed_by:
            # Drop the one already accepted too, and keep claimed_by as it was so
            # a third claimant still names the first.
            accepted.pop(ep.name, None)
            warn_plugin(
                f"ignoring plugin area {ep.name!r}: claimed by both "
                f"{claimed_by[ep.name]} and {origin} — uninstall one"
            )
            continue
        claimed_by[ep.name] = origin
        accepted[ep.name] = AreaMount(
            name=ep.name,
            help_text=_plugin_help(ep),
            target=ep.value,
            deps=None,
        )
    return tuple(sorted(accepted.values(), key=lambda mount: mount.name))


def all_areas() -> tuple[AreaMount, ...]:
    """Every mountable area, in display order: the built-ins, then plugins.

    Calling this triggers plugin discovery. The paths that must not pay for it —
    ``dp --version``, and dispatching a built-in — go through
    :data:`BUILTIN_AREAS` and :func:`area_by_name` instead.
    """
    return BUILTIN_AREAS + plugin_areas()


def is_builtin(mount: AreaMount) -> bool:
    """Whether ``mount`` is one of dp's own areas.

    The caller that needs this is error handling: an ``ImportError`` from a
    built-in is a bug in dp and should reach the user as the traceback it is,
    while the same failure from a third-party area is news about their
    environment and gets a sentence (see :mod:`dataplat.cli._lazy`).
    """
    return mount in BUILTIN_AREAS


def area_by_name(name: str) -> AreaMount | None:
    """The mount registered as ``name``, or ``None`` if nothing claims it.

    Scanned instead of indexed in a module-level dict: the CLI resolves a name
    once per invocation, and :func:`all_areas` grows with whatever is installed,
    so an index built at import time would be stale.

    Built-ins answer before plugin discovery runs, and that is not just an
    optimization: a plugin cannot claim a built-in name, so the scan could not
    change the answer, and ``dp db query`` must not pay ~5 ms to learn that.
    """
    for mount in BUILTIN_AREAS:
        if mount.name == name:
            return mount
    return next((mount for mount in plugin_areas() if mount.name == name), None)


def missing_extra_help(help_text: str, spec: AreaDeps) -> str:
    """``help_text`` plus the extra an area is waiting on.

    One template, two renderers: the root command lists the area this way while
    it is still a placeholder, and the stub that eventually explains the missing
    extra shows the same line as its own help.
    """
    return f"{help_text} (needs extra: {spec.extra})"


def mount_help(mount: AreaMount) -> str:
    """``mount``'s help line, flagging an area whose extra is not installed.

    The root command lists areas without importing them, so the hint has to be
    answerable from the mount alone — hence ``mount.deps`` rather than a lookup
    in the ``AREAS`` global, which a third-party mount is not in.
    """
    if mount.deps is None or ready(mount.deps):
        return mount.help_text
    return missing_extra_help(mount.help_text, mount.deps)


def load_app(mount: AreaMount) -> Any:
    """Import and return the Typer app behind ``mount.target``.

    Only called once the area's dependencies are known to be installed;
    the import may pull in the area's optional packages.
    """
    module_name, _, attr = mount.target.partition(":")
    module = importlib.import_module(module_name)
    return getattr(module, attr)
