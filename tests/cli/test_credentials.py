"""Where generated passwords land, and who can read them.

These moved here from ``test_role_create`` when a second command
(``role grant --create-missing-users``) started generating credentials: the
guarantees are the module's, not one command's.
"""

from __future__ import annotations

import stat
from pathlib import Path

from dataplat.cli.db import _credentials


def test_credentials_default_path_is_not_the_working_directory(
    monkeypatch, tmp_path
) -> None:
    """A generated password must not land in whatever directory you ran from.

    The old default was ``./dp-credentials-<stamp>.csv``. A data engineer's cwd is
    usually a checkout, nothing gitignores that name, and the file holds a real
    password — so the default was one `git add -A` away from committing a
    credential.
    """
    monkeypatch.setattr(_credentials, "CREDENTIALS_DIR", tmp_path / "credentials")
    monkeypatch.chdir(tmp_path)

    path = _credentials.credentials_default_path()

    assert path.parent == tmp_path / "credentials"
    assert path.parent != Path.cwd()
    assert path.name.startswith("dp-credentials-")
    # The directory listing alone leaks which roles were created and when.
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_credentials_file_is_created_unreadable_to_others(tmp_path) -> None:
    """0600, and created that way atomically rather than chmod-ed after."""
    target = tmp_path / "creds.csv"
    handle, is_new = _credentials.open_credentials_file(target)
    handle.close()

    assert is_new is True
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert _credentials.file_mode_secure(target) is True


def test_existing_file_keeps_its_mode_but_is_reported_insecure(tmp_path) -> None:
    """An operator's own 0644 file is appended to, not silently chmod-ed.

    Changing the mode of a file the caller pointed at with --credentials-out
    would be a surprise; saying so is not.
    """
    target = tmp_path / "loose.csv"
    target.touch(mode=0o644)

    handle, is_new = _credentials.open_credentials_file(target)
    handle.close()

    assert is_new is False
    assert stat.S_IMODE(target.stat().st_mode) == 0o644
    assert _credentials.file_mode_secure(target) is False


def test_mode_check_tolerates_a_missing_file(tmp_path) -> None:
    """Never block a command over a file that is not there to inspect."""
    assert _credentials.file_mode_secure(tmp_path / "gone.csv") is True
