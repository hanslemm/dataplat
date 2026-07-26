from __future__ import annotations

import os
from pathlib import Path

import pytest

from dataplat.core import envrc


def _isolate_candidates(monkeypatch, tmp_path: Path) -> Path:
    """Point every lookup candidate inside ``tmp_path``.

    The real candidates are the developer's global link, their shell's cwd and
    dataplat's own checkout, so a provenance test is only meaningful once all
    three are redirected. Returns the (empty) working directory.
    """
    monkeypatch.delenv("DP_ENVRC_PATH", raising=False)
    monkeypatch.delenv("DP_ENVRC_ALLOW_CWD", raising=False)
    monkeypatch.setattr(envrc, "CONFIG_ENVRC", tmp_path / "config" / ".envrc")
    monkeypatch.setattr(envrc, "_repo_root_envrc", lambda: tmp_path / "repo" / ".envrc")
    workdir = tmp_path / "cwd"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    return Path.cwd()


def test_parse_envrc_handles_multiline_and_comments() -> None:
    parsed = envrc.parse_envrc(
        """
# ignored
export FOO=bar
export MULTI="line1
line2"
export EMPTY=
""".strip()
    )

    assert parsed["FOO"] == "bar"
    assert parsed["MULTI"] == "line1\nline2"
    assert parsed["EMPTY"] == ""


def test_parse_envrc_strips_inline_comment_after_quoted_value() -> None:
    parsed = envrc.parse_envrc('export A="value" # comment\nexport B=after\n')

    assert parsed["A"] == "value"
    # Regression: the comment used to trigger multiline collection and
    # swallow every following line.
    assert parsed["B"] == "after"


def test_parse_envrc_strips_inline_comment_after_unquoted_value() -> None:
    parsed = envrc.parse_envrc("export A=value # comment\n")

    assert parsed["A"] == "value"


def test_parse_envrc_preserves_hash_inside_quotes() -> None:
    parsed = envrc.parse_envrc("export A='x#y'\nexport B=\"a # b\"\n")

    assert parsed["A"] == "x#y"
    assert parsed["B"] == "a # b"


def test_parse_envrc_no_space_hash_is_value() -> None:
    # Shell semantics: '#' only starts a comment after whitespace.
    parsed = envrc.parse_envrc("export A=x#y\n")

    assert parsed["A"] == "x#y"


def test_parse_envrc_multiline_ignores_trailing_text_after_close() -> None:
    parsed = envrc.parse_envrc('export PEM="line1\nline2" # trailing\nexport B=after\n')

    assert parsed["PEM"] == "line1\nline2"
    assert parsed["B"] == "after"


def test_parse_envrc_unterminated_quote_collects_to_eof() -> None:
    parsed = envrc.parse_envrc('export A="never closed\nline2\n')

    assert parsed["A"] == "never closed\nline2"


def test_find_envrc_prefers_override(monkeypatch, tmp_path: Path) -> None:
    env_file = tmp_path / ".envrc"
    env_file.write_text("export A=1")

    monkeypatch.setenv("DP_ENVRC_PATH", str(env_file))
    found = envrc.find_envrc()

    assert found == env_file


def test_find_envrc_global_config_beats_cwd(monkeypatch, tmp_path: Path) -> None:
    config_file = tmp_path / "config" / ".envrc"
    config_file.parent.mkdir()
    config_file.write_text("export A=config")
    cwd_file = tmp_path / "cwd" / ".envrc"
    cwd_file.parent.mkdir()
    cwd_file.write_text("export A=cwd")

    monkeypatch.delenv("DP_ENVRC_PATH", raising=False)
    monkeypatch.setattr(envrc, "CONFIG_ENVRC", config_file)
    monkeypatch.chdir(cwd_file.parent)

    assert envrc.find_envrc() == config_file


def test_find_envrc_cwd_beats_repo_root(monkeypatch, tmp_path: Path) -> None:
    cwd_file = tmp_path / ".envrc"
    cwd_file.write_text("export A=cwd")

    monkeypatch.delenv("DP_ENVRC_PATH", raising=False)
    monkeypatch.delenv("DP_ENVRC_ALLOW_CWD", raising=False)
    monkeypatch.setattr(envrc, "CONFIG_ENVRC", tmp_path / "missing" / ".envrc")
    monkeypatch.chdir(tmp_path)

    assert envrc.find_envrc() == cwd_file


def test_locate_envrc_reports_override_provenance(monkeypatch, tmp_path: Path) -> None:
    _isolate_candidates(monkeypatch, tmp_path)
    override = tmp_path / "elsewhere.envrc"
    override.write_text("export A=override")
    monkeypatch.setenv("DP_ENVRC_PATH", str(override))

    assert envrc.locate_envrc() == envrc.EnvrcLocation(
        override, envrc.EnvrcSource.override
    )


def test_locate_envrc_reports_global_link_provenance(
    monkeypatch, tmp_path: Path
) -> None:
    workdir = _isolate_candidates(monkeypatch, tmp_path)
    link = tmp_path / "config" / ".envrc"
    link.parent.mkdir()
    link.write_text("export A=config")
    (workdir / ".envrc").write_text("export A=cwd")

    assert envrc.locate_envrc() == envrc.EnvrcLocation(
        link, envrc.EnvrcSource.global_link
    )


def test_locate_envrc_reports_cwd_provenance(monkeypatch, tmp_path: Path) -> None:
    workdir = _isolate_candidates(monkeypatch, tmp_path)
    cwd_file = workdir / ".envrc"
    cwd_file.write_text("export A=cwd")

    assert envrc.locate_envrc() == envrc.EnvrcLocation(cwd_file, envrc.EnvrcSource.cwd)


def test_locate_envrc_reports_repo_root_provenance(monkeypatch, tmp_path: Path) -> None:
    _isolate_candidates(monkeypatch, tmp_path)
    repo_file = tmp_path / "repo" / ".envrc"
    repo_file.parent.mkdir()
    repo_file.write_text("export A=repo")

    assert envrc.locate_envrc() == envrc.EnvrcLocation(
        repo_file, envrc.EnvrcSource.repo_root
    )


def test_locate_envrc_returns_none_when_no_candidate_exists(
    monkeypatch, tmp_path: Path
) -> None:
    _isolate_candidates(monkeypatch, tmp_path)

    assert envrc.locate_envrc() is None
    assert envrc.find_envrc() is None


@pytest.mark.parametrize("value", ["0", "false", "no", "FALSE", " no "])
def test_locate_envrc_skips_cwd_when_opted_out(
    monkeypatch, tmp_path: Path, value: str
) -> None:
    workdir = _isolate_candidates(monkeypatch, tmp_path)
    (workdir / ".envrc").write_text("export A=cwd")
    monkeypatch.setenv("DP_ENVRC_ALLOW_CWD", value)

    assert envrc.locate_envrc() is None


@pytest.mark.parametrize("value", ["", "1", "true", "yes", "anything"])
def test_locate_envrc_keeps_cwd_for_other_values(
    monkeypatch, tmp_path: Path, value: str
) -> None:
    workdir = _isolate_candidates(monkeypatch, tmp_path)
    cwd_file = workdir / ".envrc"
    cwd_file.write_text("export A=cwd")
    monkeypatch.setenv("DP_ENVRC_ALLOW_CWD", value)

    assert envrc.locate_envrc() == envrc.EnvrcLocation(cwd_file, envrc.EnvrcSource.cwd)


def test_opting_out_of_cwd_keeps_repo_root_candidate(
    monkeypatch, tmp_path: Path
) -> None:
    # The opt-out is about untrusted directories, not about breaking dev
    # installs that rely on their own checkout's .envrc.
    workdir = _isolate_candidates(monkeypatch, tmp_path)
    (workdir / ".envrc").write_text("export A=cwd")
    repo_file = tmp_path / "repo" / ".envrc"
    repo_file.parent.mkdir()
    repo_file.write_text("export A=repo")
    monkeypatch.setenv("DP_ENVRC_ALLOW_CWD", "0")

    assert envrc.locate_envrc() == envrc.EnvrcLocation(
        repo_file, envrc.EnvrcSource.repo_root
    )


def test_find_envrc_returns_the_located_path(monkeypatch, tmp_path: Path) -> None:
    workdir = _isolate_candidates(monkeypatch, tmp_path)
    cwd_file = workdir / ".envrc"
    cwd_file.write_text("export A=cwd")

    location = envrc.locate_envrc()

    assert location is not None
    assert envrc.find_envrc() == location.path == cwd_file


def test_load_envrc_ignores_cwd_when_opted_out(monkeypatch, tmp_path: Path) -> None:
    workdir = _isolate_candidates(monkeypatch, tmp_path)
    (workdir / ".envrc").write_text("export UNTRUSTED_HOST=evil.example.com")
    monkeypatch.setenv("DP_ENVRC_ALLOW_CWD", "0")
    monkeypatch.delenv("UNTRUSTED_HOST", raising=False)

    envrc.load_envrc()

    assert "UNTRUSTED_HOST" not in os.environ


def test_load_envrc_does_not_override_existing(monkeypatch, tmp_path: Path) -> None:
    env_file = tmp_path / ".envrc"
    env_file.write_text("export KEEP=from_file\nexport NEW=from_file")

    monkeypatch.setattr(envrc, "find_envrc", lambda: env_file)

    monkeypatch.setenv("KEEP", "from_shell")
    monkeypatch.delenv("NEW", raising=False)

    envrc.load_envrc()

    assert os.environ["KEEP"] == "from_shell"
    assert os.environ["NEW"] == "from_file"


def test_load_envrc_does_not_create_symlink(monkeypatch, tmp_path: Path) -> None:
    env_file = tmp_path / ".envrc"
    env_file.write_text("export SOMEVAR=x")
    config_link = tmp_path / "config" / ".envrc"

    monkeypatch.delenv("SOMEVAR", raising=False)
    monkeypatch.setattr(envrc, "find_envrc", lambda: env_file)
    monkeypatch.setattr(envrc, "CONFIG_ENVRC", config_link)

    envrc.load_envrc()

    assert not config_link.exists()
    assert os.environ["SOMEVAR"] == "x"
