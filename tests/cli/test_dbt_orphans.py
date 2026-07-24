from __future__ import annotations

from pathlib import Path

import pytest

from dataplat.cli.db.dbt_orphans import _parse_exclusions
from dataplat.core.errors import ValidationError


def test_parse_exclusions_schema_only() -> None:
    schemas, relations = _parse_exclusions(["public"], None)
    assert schemas == frozenset({"public"})
    assert relations == frozenset()


def test_parse_exclusions_schema_dot_name() -> None:
    schemas, relations = _parse_exclusions(["public.foo"], None)
    assert schemas == frozenset()
    assert relations == frozenset({("public", "foo")})


def test_parse_exclusions_mixed_tokens() -> None:
    schemas, relations = _parse_exclusions(
        ["public", "analytics.legacy"], None
    )
    assert schemas == frozenset({"public"})
    assert relations == frozenset({("analytics", "legacy")})


def test_parse_exclusions_rejects_multi_dot() -> None:
    with pytest.raises(ValidationError):
        _parse_exclusions(["a.b.c"], None)


def test_parse_exclusions_rejects_empty_token_after_strip() -> None:
    with pytest.raises(ValidationError):
        _parse_exclusions(["   "], None)


def test_parse_exclusions_strips_whitespace() -> None:
    schemas, relations = _parse_exclusions(["  public  "], None)
    assert schemas == frozenset({"public"})


def test_parse_exclusions_reads_file(tmp_path: Path) -> None:
    exclude_file = tmp_path / "excludes.txt"
    exclude_file.write_text(
        "# comment line\n"
        "public\n"
        "\n"
        "analytics.legacy\n"
    )
    schemas, relations = _parse_exclusions([], str(exclude_file))
    assert schemas == frozenset({"public"})
    assert relations == frozenset({("analytics", "legacy")})


def test_parse_exclusions_merges_cli_and_file(tmp_path: Path) -> None:
    exclude_file = tmp_path / "excludes.txt"
    exclude_file.write_text("analytics.legacy\n")
    schemas, relations = _parse_exclusions(["public"], str(exclude_file))
    assert schemas == frozenset({"public"})
    assert relations == frozenset({("analytics", "legacy")})


def test_parse_exclusions_missing_file_raises(tmp_path: Path) -> None:
    missing = tmp_path / "nope.txt"
    with pytest.raises(ValidationError):
        _parse_exclusions([], str(missing))


def test_parse_exclusions_comma_separated_schemas() -> None:
    schemas, relations = _parse_exclusions(["public,analytics"], None)
    assert schemas == frozenset({"public", "analytics"})
    assert relations == frozenset()


def test_parse_exclusions_comma_separated_mixed() -> None:
    schemas, relations = _parse_exclusions(
        ["public,analytics.legacy", "scratch"], None
    )
    assert schemas == frozenset({"public", "scratch"})
    assert relations == frozenset({("analytics", "legacy")})


def test_parse_exclusions_comma_strips_whitespace() -> None:
    schemas, _ = _parse_exclusions(["  public , analytics  "], None)
    assert schemas == frozenset({"public", "analytics"})


def test_parse_exclusions_comma_rejects_empty_piece() -> None:
    with pytest.raises(ValidationError):
        _parse_exclusions(["public,,analytics"], None)


def test_parse_exclusions_comma_in_file(tmp_path: Path) -> None:
    exclude_file = tmp_path / "excludes.txt"
    exclude_file.write_text("public,analytics\n# skip me\nscratch.tmp\n")
    schemas, relations = _parse_exclusions([], str(exclude_file))
    assert schemas == frozenset({"public", "analytics"})
    assert relations == frozenset({("scratch", "tmp")})
