from __future__ import annotations

import pytest

from dataplat.core.errors import ConfigError, ValidationError
from dataplat.services.db.connection import SqlEngine
from dataplat.services.db.targets import (
    default_target_name,
    load_targets,
    resolve_target,
    resolve_targets,
)


def test_demo_pg_target() -> None:
    target = resolve_target("demo_pg")
    assert target.env_prefix == "DEMO_PG"
    assert target.engine == SqlEngine.postgresql
    assert target.reassign_owner == "demo_pg_root"


def test_demo_rs_target() -> None:
    target = resolve_target("demo_rs")
    assert target.env_prefix == "DEMO_RS"
    assert target.engine == SqlEngine.redshift
    assert target.reassign_owner == "admin"


def test_resolve_target_case_insensitive() -> None:
    assert resolve_target("Demo_PG") == load_targets()["demo_pg"]


def test_resolve_target_unknown_raises() -> None:
    with pytest.raises(ValidationError, match="Unknown target"):
        resolve_target("nope")


def test_resolve_targets_all() -> None:
    targets = resolve_targets("all")
    assert [t.name for t in targets] == ["demo_pg", "demo_rs"]


def test_resolve_targets_single() -> None:
    targets = resolve_targets("demo_rs")
    assert [t.name for t in targets] == ["demo_rs"]


def test_resolve_target_rejects_all() -> None:
    with pytest.raises(ValidationError, match="Unknown target"):
        resolve_target("all")


def test_default_target_is_first_listed() -> None:
    assert default_target_name() == "demo_pg"


def test_default_target_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DP_DEFAULT_TARGET", "demo_rs")
    assert default_target_name() == "demo_rs"


def test_default_target_unknown_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DP_DEFAULT_TARGET", "nope")
    with pytest.raises(ConfigError, match="DP_DEFAULT_TARGET"):
        default_target_name()


def test_no_targets_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DP_TARGETS", "")
    assert load_targets() == {}
    assert default_target_name() is None
    with pytest.raises(ValidationError, match="No targets configured"):
        resolve_target("anything")
    with pytest.raises(ValidationError, match="No targets configured"):
        resolve_targets("all")


def test_targets_parse_whitespace_and_dashes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DP_TARGETS", " My-Lake , demo_pg ")
    targets = load_targets()
    assert list(targets) == ["my-lake", "demo_pg"]
    assert targets["my-lake"].env_prefix == "MY_LAKE"
    assert targets["my-lake"].engine == SqlEngine.postgresql  # default


def test_bad_engine_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DP_TARGETS", "weird")
    monkeypatch.setenv("WEIRD_ENGINE", "oracle")
    with pytest.raises(ConfigError, match="WEIRD_ENGINE"):
        load_targets()


def test_reserved_all_name_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DP_TARGETS", "all")
    with pytest.raises(ConfigError, match="reserved"):
        load_targets()
