"""Where generated passwords go, and with what permissions.

Two commands now generate credentials — ``dp db role create`` and
``dp db role grant --create-missing-users`` — and a second copy of "open 0600,
append, warn if the mode is wrong" is exactly the kind of duplication that ends
with one of the copies quietly losing the ``0600``. One home instead.

The default location is deliberate. It used to be ``Path.cwd()``, which for a
data engineer is almost always a checkout: nothing gitignores
``dp-credentials-*.csv``, the file holds a real password, and the failure mode is
committing it. These live with dataplat's other state instead, derived from
:data:`~dataplat.core.envrc.CONFIG_DIR` so the config location stays defined in
one place.
"""

from __future__ import annotations

import os
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dataplat.core.envrc import CONFIG_DIR

__all__ = [
    "CREDENTIALS_DIR",
    "credentials_default_path",
    "file_mode_secure",
    "open_credentials_file",
]

CREDENTIALS_DIR = CONFIG_DIR / "credentials"


def credentials_default_path() -> Path:
    """A timestamped credentials CSV under the shared config directory.

    The directory is created 0700 on first use: its listing alone leaks which
    roles were created and when, even before anyone reads a file. The file
    itself is opened 0600 by :func:`open_credentials_file`.
    """
    CREDENTIALS_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return CREDENTIALS_DIR / f"dp-credentials-{stamp}.csv"


def open_credentials_file(path: Path) -> tuple[Any, bool]:
    """Open the credentials CSV in append mode with secure permissions.

    Returns ``(file, is_new)``. New files are created with mode 0600. If the
    file already exists, we leave its mode alone but tell the caller so they
    can flag insecure permissions in the rendered output.
    """
    is_new = not path.exists()
    if is_new:
        # Create with 0600 atomically via os.open; otherwise there's a window
        # where the file is readable before we chmod it.
        fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
        file = os.fdopen(fd, "a", newline="")
    else:
        file = open(path, "a", newline="")  # noqa: SIM115
    return file, is_new


def file_mode_secure(path: Path) -> bool:
    """Whether ``path`` denies all group and other access."""
    try:
        mode = path.stat().st_mode
    except OSError:
        return True  # don't block on a missing file we just created
    return not bool(mode & (stat.S_IRWXG | stat.S_IRWXO))
