"""Environment loading and config-link helpers.

The active ``.envrc`` is looked up in a fixed order: the ``DP_ENVRC_PATH``
override, the global config link, ``./.envrc``, then the repo root of an
editable/dev install. That third candidate makes every command sensitive to
where it is run from — standing in a cloned repo points ``dp`` at whatever
host and credentials that repo's ``.envrc`` exports. direnv answers this with
an explicit allowlist; we keep the convenience but make it visible and
optional:

- :func:`locate_envrc` reports *which* candidate won, so the CLI can name the
  active file and warn when it came from the current directory;
- ``DP_ENVRC_ALLOW_CWD`` set to ``0``/``false``/``no`` skips the
  current-directory candidate entirely. It does not touch the dev repo root
  candidate, which is dataplat's own checkout rather than wherever the user
  happens to stand.

The loader is a parser, not a shell: it reads ``export KEY=value`` lines and
performs no expansion, so ``export PGHOST=$DB_HOST`` loads the seven characters
``$DB_HOST``. That was documented on :func:`parse_envrc` and invisible at
runtime — the connection then fails with an authentication or resolution error
that looks like a bad credential. :func:`unexpanded_env_refs` makes it
detectable so ``dp config doctor`` can name it. Expansion itself stays
unimplemented on purpose: doing it properly is a shell, and doing it
approximately is a second set of semantics for users to discover.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

CONFIG_DIR = Path.home() / ".config" / "dataplat"
CONFIG_ENVRC = CONFIG_DIR / ".envrc"

# '#' starts a comment only when preceded by whitespace (shell semantics).
_INLINE_COMMENT = re.compile(r"\s+#.*$")

# A shell variable reference this loader will *not* expand: `$NAME` or
# `${NAME}` (and `${NAME:-x}`, whose name is all we need). Three shapes are
# excluded because the shell would not have expanded them either, so flagging
# them would be a false alarm: `\$NAME` is escaped, `$$` is the PID, and `$1` is
# a positional — neither of the last two can start an identifier.
_SHELL_REF = re.compile(r"(?<!\\)\$\{?(?P<name>[A-Za-z_][A-Za-z0-9_]*)")

_ALLOW_CWD_VAR = "DP_ENVRC_ALLOW_CWD"
_DISABLED_VALUES = {"0", "false", "no"}


class EnvrcSource(str, Enum):
    """Which candidate produced the active ``.envrc``.

    The values are user-facing: ``dp config show`` and ``dp config doctor``
    print them verbatim, so they read as provenance, not as identifiers.
    """

    override = "DP_ENVRC_PATH"
    global_link = "global link"
    cwd = "current directory"
    repo_root = "dev repo root"


@dataclass(frozen=True)
class EnvrcLocation:
    """An active ``.envrc`` together with where the lookup found it."""

    path: Path
    source: EnvrcSource


def cwd_envrc_allowed() -> bool:
    """Whether ``./.envrc`` may be picked up (opt-out, direnv-style trust)."""
    return os.environ.get(_ALLOW_CWD_VAR, "").strip().lower() not in _DISABLED_VALUES


def _repo_root_envrc() -> Path:
    """The ``.envrc`` next to an editable/dev checkout of dataplat itself.

    Computed per call rather than at import so tests can point the candidate
    somewhere harmless instead of relying on the real checkout.
    """
    return Path(__file__).resolve().parents[2] / ".envrc"


def locate_envrc() -> EnvrcLocation | None:
    """Find an envrc file and report which candidate won.

    Callers that show configuration need the provenance, not just the path:
    a file found in the current directory is only as trustworthy as the repo
    the user is standing in, so it warrants a warning.
    """
    envrc_override = os.environ.get("DP_ENVRC_PATH")
    if envrc_override:
        override_path = Path(envrc_override).expanduser()
        if override_path.is_file():
            return EnvrcLocation(override_path, EnvrcSource.override)

    candidates = [(CONFIG_ENVRC, EnvrcSource.global_link)]
    if cwd_envrc_allowed():
        candidates.append((Path.cwd() / ".envrc", EnvrcSource.cwd))
    candidates.append((_repo_root_envrc(), EnvrcSource.repo_root))
    for candidate, source in candidates:
        if candidate.is_file():
            return EnvrcLocation(candidate, source)
    return None


def find_envrc() -> Path | None:
    """Path of the active envrc file, or ``None``.

    Thin wrapper over :func:`locate_envrc` for the callers that only need
    the path.
    """
    location = locate_envrc()
    return location.path if location else None


def _close_quote(text: str, quote: str) -> tuple[str, bool]:
    """Return ``(value, closed)`` for a quoted chunk.

    The value ends at the first occurrence of ``quote``; anything after the
    closing quote (e.g. an inline comment) is ignored.
    """
    idx = text.find(quote)
    if idx == -1:
        return text, False
    return text[:idx], True


@dataclass(frozen=True)
class _Export:
    """One ``export KEY=value`` line, with the quoting that produced it.

    The quote character is what :func:`unexpanded_env_refs` needs and
    :func:`parse_envrc` discards: in the shell, single quotes suppress expansion,
    so a ``$NAME`` inside them is a literal both there and here.
    """

    key: str
    value: str
    quote: str


def _iter_exports(content: str) -> Iterator[_Export]:
    """Yield every export in file order. The one parser both views share."""
    lines = content.splitlines()
    i = 0

    while i < len(lines):
        line = lines[i].strip()
        i += 1

        if not line or line.startswith("#") or not line.startswith("export "):
            continue

        rest = line[len("export ") :]
        if "=" not in rest:
            continue

        key, value = rest.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue

        if value[:1] in ('"', "'"):
            quote = value[0]
            chunk, closed = _close_quote(value[1:], quote)
            if closed:
                yield _Export(key, chunk, quote)
                continue
            collected = [chunk]
            while i < len(lines):
                chunk, closed = _close_quote(lines[i], quote)
                collected.append(chunk)
                i += 1
                if closed:
                    break
            yield _Export(key, "\n".join(collected), quote)
            continue

        yield _Export(key, _INLINE_COMMENT.sub("", value).strip(), "")


def parse_envrc(content: str) -> dict[str, str]:
    """Parse shell-style ``export KEY=value`` lines from .envrc content.

    Supports single-/double-quoted values (including multiline blocks such
    as PEM keys) and strips inline ``#`` comments outside quotes. Variable
    expansion and escapes are not interpreted — see
    :func:`unexpanded_env_refs`, which reports where that matters.
    """
    return {export.key: export.value for export in _iter_exports(content)}


def unexpanded_env_refs(
    content: str, environ: Mapping[str, str] | None = None
) -> dict[str, list[str]]:
    """Loaded keys whose value still holds a ``$VAR`` the shell would have expanded.

    Maps each affected key to the variable names its value references, in the
    order they appear. Empty when there is nothing to report, which is the normal
    case — the caller can treat a non-empty result as "warn".

    Three filters keep it from crying wolf, because a warning nobody trusts is
    worse than none:

    - a single-quoted value is skipped: the shell would not expand it either, so
      the literal we loaded is exactly what the shell would have exported (this
      is also what keeps a password like ``'p$ss'`` out of the report);
    - a key whose current environment value is *not* the literal from the file is
      skipped: :func:`load_envrc` is ``setdefault``-based, so the shell already
      had that variable and the file's line never took effect;
    - escaped and non-identifier forms never match at all — see ``_SHELL_REF``.

    ``environ`` exists to be substituted in tests; it defaults to the real one.
    """
    env = os.environ if environ is None else environ
    affected: dict[str, list[str]] = {}
    for export in _iter_exports(content):
        if export.quote == "'" or env.get(export.key) != export.value:
            continue
        names: list[str] = []
        for match in _SHELL_REF.finditer(export.value):
            name = match.group("name")
            if name not in names:
                names.append(name)
        if names:
            affected[export.key] = names
    return affected


def link_envrc(source: Path) -> None:
    """Create/update the global envrc symlink."""
    resolved = source.resolve()
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    if CONFIG_ENVRC.is_symlink() or CONFIG_ENVRC.exists():
        if CONFIG_ENVRC.resolve() == resolved:
            return
        CONFIG_ENVRC.unlink()

    CONFIG_ENVRC.symlink_to(resolved)


def load_envrc() -> None:
    """Load environment variables from .envrc without overriding existing values."""
    envrc_path = find_envrc()
    if not envrc_path:
        return

    try:
        content = envrc_path.read_text()
    except OSError:
        return

    for key, value in parse_envrc(content).items():
        os.environ.setdefault(key, value)
