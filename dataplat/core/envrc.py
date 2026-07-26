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
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

CONFIG_DIR = Path.home() / ".config" / "dataplat"
CONFIG_ENVRC = CONFIG_DIR / ".envrc"

# '#' starts a comment only when preceded by whitespace (shell semantics).
_INLINE_COMMENT = re.compile(r"\s+#.*$")

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


def parse_envrc(content: str) -> dict[str, str]:
    """Parse shell-style ``export KEY=value`` lines from .envrc content.

    Supports single-/double-quoted values (including multiline blocks such
    as PEM keys) and strips inline ``#`` comments outside quotes. Variable
    expansion and escapes are not interpreted.
    """
    lines = content.splitlines()
    env: dict[str, str] = {}
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
                env[key] = chunk
                continue
            collected = [chunk]
            while i < len(lines):
                chunk, closed = _close_quote(lines[i], quote)
                collected.append(chunk)
                i += 1
                if closed:
                    break
            env[key] = "\n".join(collected)
            continue

        env[key] = _INLINE_COMMENT.sub("", value).strip()

    return env


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
