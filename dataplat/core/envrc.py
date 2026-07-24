"""Environment loading and config-link helpers."""

from __future__ import annotations

import os
import re
from pathlib import Path

CONFIG_DIR = Path.home() / ".config" / "dataplat"
CONFIG_ENVRC = CONFIG_DIR / ".envrc"

# '#' starts a comment only when preceded by whitespace (shell semantics).
_INLINE_COMMENT = re.compile(r"\s+#.*$")


def find_envrc() -> Path | None:
    """Find an envrc file.

    Priority: ``DP_ENVRC_PATH`` override, the global config link, the
    current directory, then the repo root (editable/dev installs).
    """
    envrc_override = os.environ.get("DP_ENVRC_PATH")
    if envrc_override:
        override_path = Path(envrc_override).expanduser()
        if override_path.is_file():
            return override_path

    candidates = [
        CONFIG_ENVRC,
        Path.cwd() / ".envrc",
        Path(__file__).resolve().parents[2] / ".envrc",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


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
