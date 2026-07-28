from __future__ import annotations

import importlib
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

import pytest
import typer

from dataplat.core import registry
from dataplat.core.deps import AREAS, AreaDeps
from dataplat.core.registry import (
    BUILTIN_AREAS,
    PLUGIN_GROUP,
    AreaMount,
    all_areas,
    area_by_name,
    is_builtin,
    load_app,
    missing_extra_help,
    mount_help,
    plugin_areas,
)

# A stand-in for a plugin-supplied area: it carries its own AreaDeps and is
# deliberately absent from the AREAS global, which is what used to KeyError.
third_party_app = typer.Typer(name="not-the-mount-name", help="Widget tools")

ABSENT_MODULE = "dataplat_no_such_dependency"

THIRD_PARTY = AreaMount(
    name="widget",
    help_text="Widget tools",
    target="tests.core.test_registry:third_party_app",
    deps=AreaDeps(
        area="widget",
        extra="widget",
        modules=(ABSENT_MODULE,),
        enabled_by=("WIDGET_URL",),
    ),
)


def write_plugin_dist(
    site: Path,
    *,
    dist: str,
    version: str = "1.0.0",
    summary: str | None = "Widget tools for dataplat",
    areas: Sequence[str] = (),
    metadata: str | None = None,
    module: str | None = None,
    source: str = "",
) -> Path:
    """Install one real distribution into ``site`` and return ``site``.

    A ``.dist-info`` directory on ``sys.path`` is all ``importlib.metadata``
    reads, so this registers a genuine entry point and exercises the real
    discovery path — including the standard library's own parsing, which is where
    a malformed entry-point value turns into an ``AssertionError``. Monkeypatching
    ``entry_points()`` would test our loop against a shape we invented.

    ``areas`` are ``"name = module:attr"`` lines for the ``[dataplat.areas]``
    section. ``metadata`` replaces the whole METADATA file, for the
    unreadable-distribution case. ``module``/``source`` write an importable
    module; leaving them out is the norm, because discovery must not import one.

    Shared with ``tests/cli/test_cli_smoke.py`` so the unit tests and the
    end-to-end runs cannot drift about what a plugin distribution looks like.
    """
    site.mkdir(parents=True, exist_ok=True)
    info = site / f"{dist.replace('-', '_')}-{version}.dist-info"
    info.mkdir()
    if metadata is None:
        metadata = f"Metadata-Version: 2.1\nName: {dist}\nVersion: {version}\n"
        if summary is not None:
            metadata += f"Summary: {summary}\n"
    info.joinpath("METADATA").write_text(metadata)
    if areas:
        body = "\n".join(areas)
        info.joinpath("entry_points.txt").write_text(f"[{PLUGIN_GROUP}]\n{body}\n")
    if module is not None:
        site.joinpath(f"{module}.py").write_text(source)
    return site


@contextmanager
def installed(*sites: Path) -> Iterator[None]:
    """Put ``sites`` on ``sys.path`` for the block, with clean discovery caches.

    ``invalidate_caches`` because the path entries are new to a long-running
    process, and ``plugin_areas.cache_clear()`` on *both* sides because the
    registry memoizes the scan for the life of a process: a cache left populated
    would either hide this test's plugin or leak it into the next test. Any module
    imported *from* a site goes too — only those, so an unrelated import the block
    happened to trigger is not evicted from under whoever holds a reference to it.

    A context manager rather than a fixture so both test modules that need it can
    import it (there is no shared conftest for tests/core and tests/cli).
    """
    plugin_areas.cache_clear()
    original_path = list(sys.path)
    roots = tuple(str(site) for site in sites)
    sys.path[:0] = roots
    importlib.invalidate_caches()
    try:
        yield
    finally:
        sys.path[:] = original_path
        for name, module in list(sys.modules.items()):
            origin = getattr(module, "__file__", None)
            if origin and roots and origin.startswith(roots):
                del sys.modules[name]
        importlib.invalidate_caches()
        plugin_areas.cache_clear()


def test_all_builtin_areas_present_in_order() -> None:
    assert [m.name for m in all_areas()] == ["db", "ingest", "bi", "cloud", "ci"]


def test_every_deps_contract_comes_from_the_deps_module() -> None:
    for mount in all_areas():
        if mount.deps is not None:
            assert mount.deps is AREAS[mount.name]


def test_builtin_contracts_agree_with_their_mount_name() -> None:
    # deps.missing_modules/area_ready still index AREAS by area name, so a
    # mount whose name drifted from its contract would report another area's
    # dependencies.
    for mount in all_areas():
        if mount.deps is not None:
            assert mount.deps.area == mount.name


def test_load_app_resolves_every_builtin_target() -> None:
    # Dev env has all extras installed, so every target must import.
    for mount in all_areas():
        app = load_app(mount)
        assert isinstance(app, typer.Typer), mount.target


def test_load_app_resolves_a_third_party_target() -> None:
    assert load_app(THIRD_PARTY) is third_party_app


def test_targets_use_entry_point_shape() -> None:
    # "module:attr" is exactly what a plugin's entry point will declare;
    # keep the built-ins on the same contract.
    for mount in all_areas():
        module, sep, attr = mount.target.partition(":")
        assert sep == ":" and module and attr


def test_area_by_name_finds_every_builtin() -> None:
    for mount in all_areas():
        assert area_by_name(mount.name) is mount


def test_area_by_name_is_none_for_an_unknown_name() -> None:
    assert area_by_name("nope") is None


def test_mount_help_is_the_plain_help_when_dependencies_are_installed() -> None:
    # Dev env has every extra, so no built-in advertises a missing one.
    for mount in all_areas():
        assert mount_help(mount) == mount.help_text


def test_mount_help_flags_the_missing_extra_of_a_third_party_mount() -> None:
    assert THIRD_PARTY.name not in AREAS
    assert mount_help(THIRD_PARTY) == "Widget tools (needs extra: widget)"


def test_missing_extra_help_matches_what_mount_help_produces() -> None:
    assert THIRD_PARTY.deps is not None
    assert missing_extra_help(THIRD_PARTY.help_text, THIRD_PARTY.deps) == mount_help(
        THIRD_PARTY
    )


def test_is_builtin_separates_dp_areas_from_everything_else() -> None:
    # What error handling branches on: a built-in that will not import is a bug
    # in dp, a third-party area that will not import is news about the machine.
    for mount in BUILTIN_AREAS:
        assert is_builtin(mount)
    assert not is_builtin(THIRD_PARTY)


# --- plugin discovery --------------------------------------------------------


def _widget(
    tmp_path: Path,
    *,
    summary: str | None = "Widget tools for dataplat",
    areas: Sequence[str] = ("widget = widget_dp:app",),
) -> Path:
    """A ``widget-dp 1.2.0`` distribution declaring one ``widget`` area."""
    return write_plugin_dist(
        tmp_path / "widget",
        dist="widget-dp",
        version="1.2.0",
        summary=summary,
        areas=areas,
    )


def test_a_declared_entry_point_becomes_a_mount(tmp_path: Path) -> None:
    with installed(_widget(tmp_path)):
        assert plugin_areas() == (
            AreaMount(
                name="widget",
                help_text="Widget tools for dataplat",
                target="widget_dp:app",
                deps=None,
            ),
        )


def test_a_plugin_mount_carries_no_dependency_contract(tmp_path: Path) -> None:
    # deps=None is a decision, not an omission: the missing-extra installer
    # prescribes `dataplat[<extra>]`, which for a third party's extra would be a
    # command that installs the wrong thing.
    with installed(_widget(tmp_path)):
        (mount,) = plugin_areas()
        assert mount.deps is None
        assert mount_help(mount) == mount.help_text


def test_discovery_imports_nothing(tmp_path: Path) -> None:
    """The whole reason the contract is a string and not an AreaMount.

    ``widget_dp`` is never written to disk here, so a mount that describes it
    proves nothing was imported to produce that description — and `dp --help`
    keeps costing zero imports however many plugins are installed.
    """
    with installed(_widget(tmp_path)):
        assert [m.name for m in plugin_areas()] == ["widget"]
        assert "widget_dp" not in sys.modules


def test_all_areas_appends_plugins_after_the_builtins(tmp_path: Path) -> None:
    with installed(_widget(tmp_path)):
        assert [m.name for m in all_areas()] == [
            "db",
            "ingest",
            "bi",
            "cloud",
            "ci",
            "widget",
        ]


def test_area_by_name_finds_a_plugin_area(tmp_path: Path) -> None:
    with installed(_widget(tmp_path)):
        mount = area_by_name("widget")
        assert mount is not None and mount.target == "widget_dp:app"


def test_plugin_areas_are_ordered_by_name(tmp_path: Path) -> None:
    # Scan order is whatever order the filesystem lists sys.path entries in:
    # stable on one machine, arbitrary between two. `dp --help` must not reorder
    # itself when a colleague runs it.
    zebra = write_plugin_dist(
        tmp_path / "z", dist="zebra-dp", areas=("zebra = zebra_dp:app",)
    )
    alpha = write_plugin_dist(
        tmp_path / "a", dist="alpha-dp", areas=("alpha = alpha_dp:app",)
    )
    with installed(zebra, alpha):
        assert [m.name for m in plugin_areas()] == ["alpha", "zebra"]


# --- help text --------------------------------------------------------------


def test_help_text_comes_from_the_distribution_summary(tmp_path: Path) -> None:
    with installed(_widget(tmp_path, summary="Widgets, sprockets and gears")):
        assert plugin_areas()[0].help_text == "Widgets, sprockets and gears"


def test_help_text_names_the_distribution_when_there_is_no_summary(
    tmp_path: Path,
) -> None:
    # Truthful and useful: it answers "why does my dp have a widget command".
    with installed(_widget(tmp_path, summary=None)):
        assert plugin_areas()[0].help_text == "Provided by widget-dp"


def test_the_setuptools_unknown_summary_is_not_shown_as_help(tmp_path: Path) -> None:
    # Old setuptools wrote `Summary: UNKNOWN` for an absent summary. Printing it
    # would be a help line no reader could explain.
    with installed(_widget(tmp_path, summary="UNKNOWN")):
        assert plugin_areas()[0].help_text == "Provided by widget-dp"


def test_a_paragraph_summary_is_clipped_to_one_help_line(tmp_path: Path) -> None:
    with installed(_widget(tmp_path, summary="Widgets. " * 40)):
        help_text = plugin_areas()[0].help_text
        assert len(help_text) == 60
        assert help_text.endswith("…")


def test_a_multiline_summary_becomes_one_line(tmp_path: Path) -> None:
    # A folded METADATA header arrives with its continuation indented; a help
    # line with an embedded newline breaks the table it is rendered in.
    with installed(_widget(tmp_path, summary="Widget tools\n  for dataplat")):
        assert plugin_areas()[0].help_text == "Widget tools for dataplat"


# --- refusals ---------------------------------------------------------------


def test_a_plugin_may_not_take_a_builtin_areas_name(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Refuse, never shadow.

    ``dp db`` is the command people have muscle memory for; letting any installed
    distribution redefine it would make an accident and a supply-chain attack
    look identical from the outside.
    """
    site = write_plugin_dist(
        tmp_path / "rude",
        dist="rude-dp",
        version="3.0.0",
        areas=("db = rude_dp:app", "fine = rude_dp:app"),
    )
    with installed(site):
        assert [m.name for m in plugin_areas()] == ["fine"]
        db = area_by_name("db")
        assert db is not None and db.target == "dataplat.cli.db:app"

    err = capsys.readouterr().err
    assert "ignoring plugin area 'db' from rude-dp 3.0.0" in err
    assert "dp db is a built-in area" in err


def test_two_distributions_claiming_one_name_get_neither(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """No winner is defensible, so there is no winner.

    Picking one means picking by scan order, which is the filesystem's; and the
    loser would fail as a handful of missing subcommands inside an area that
    otherwise works — far harder to diagnose than an area that is plainly absent
    with a line saying why.
    """
    other = write_plugin_dist(
        tmp_path / "other",
        dist="other-dp",
        version="0.9.0",
        areas=("widget = other_dp:app",),
    )
    with installed(_widget(tmp_path), other):
        assert plugin_areas() == ()
        assert area_by_name("widget") is None

    err = capsys.readouterr().err
    assert "ignoring plugin area 'widget'" in err
    assert "widget-dp 1.2.0" in err and "other-dp 0.9.0" in err


@pytest.mark.parametrize(
    "value",
    [
        "not-a-target",
        "widget_dp",
        "widget_dp:",
        ":app",
        "widget_dp:app.deeper",
        "widget_dp:app [extra]",
    ],
)
def test_a_target_load_app_could_not_import_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], value: str
) -> None:
    """Checked against the raw value, and that is load-bearing.

    ``EntryPoint.module`` asserts on a malformed value, so asking the standard
    library to parse these would turn a plugin author's typo into a bare
    ``AssertionError`` out of dp. Refusing at discovery also keeps `dp --help`
    from advertising an area that provably cannot run.
    """
    with installed(_widget(tmp_path, areas=(f"widget = {value}",))):
        assert plugin_areas() == ()

    err = capsys.readouterr().err
    assert "is not module:attr" in err
    assert "widget-dp 1.2.0" in err


@pytest.mark.parametrize("name", ["BadName", "-x", "has space", "wîdget"])
def test_a_name_that_could_not_be_typed_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], name: str
) -> None:
    with installed(_widget(tmp_path, areas=(f"{name} = widget_dp:app",))):
        assert plugin_areas() == ()
    assert "not a usable area name" in capsys.readouterr().err


# The filter is the point of the test: the standard library warns that
# `dist.name` on metadata with no Name header will stop answering None and start
# raising KeyError, which is exactly the failure `_origin` wraps in try/except.
@pytest.mark.filterwarnings("ignore:Implicit None on return values:DeprecationWarning")
def test_an_unreadable_distribution_still_yields_its_area(
    tmp_path: Path,
) -> None:
    """Broken METADATA is not a reason to withhold a working area.

    ``dist.name`` and ``dist.version`` both answer ``None`` here, so the fallback
    has to be a sentence rather than an f-string of two ``None``s.
    """
    site = write_plugin_dist(
        tmp_path / "junk",
        dist="junk-dp",
        metadata="\x00 this is not metadata",
        areas=("widget = widget_dp:app", "db = widget_dp:app"),
    )
    with installed(site):
        assert [m.name for m in plugin_areas()] == ["widget"]
        assert plugin_areas()[0].help_text == "Third-party area"


def test_a_scan_that_raises_leaves_the_builtins_working(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """One corrupt distribution anywhere on sys.path must not take dp with it."""

    def boom(**kwargs: object) -> object:
        raise OSError("Errno 5: metadata went missing")

    with installed():
        monkeypatch.setattr(registry.metadata, "entry_points", boom)
        assert plugin_areas() == ()
        assert all_areas() == BUILTIN_AREAS

    err = capsys.readouterr().err
    assert "could not read plugin areas, using built-ins only" in err
    assert "OSError: Errno 5: metadata went missing" in err


# --- cost -------------------------------------------------------------------


def _count_scans(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record the entry-point groups scanned, without changing the result."""
    scanned: list[str] = []
    real = registry.metadata.entry_points

    def counted(**kwargs: str) -> object:
        scanned.append(kwargs.get("group", ""))
        return real(**kwargs)

    monkeypatch.setattr(registry.metadata, "entry_points", counted)
    return scanned


def test_discovery_is_scanned_once_per_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The scan costs milliseconds and several call sites ask for it.

    Warning once rather than once per lookup is the other half: `dp db query`
    resolves a name, reads a help line and dispatches, and a refused plugin that
    warned at each of those would print the same line three times.
    """
    site = write_plugin_dist(
        tmp_path / "rude", dist="rude-dp", areas=("db = rude_dp:app",)
    )
    with installed(site):
        scanned = _count_scans(monkeypatch)
        for _ in range(3):
            plugin_areas()
        assert scanned == [PLUGIN_GROUP]

    assert capsys.readouterr().err.count("ignoring plugin area 'db'") == 1


def test_area_by_name_answers_a_builtin_without_a_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `dp db query` resolves one name and must not pay ~2 ms to learn that no
    # plugin could have claimed it. A plugin cannot take a built-in's name, so
    # the scan could not change the answer.
    with installed():
        scanned = _count_scans(monkeypatch)
        assert area_by_name("db") is not None
        assert scanned == []
        assert area_by_name("nothing-claims-this") is None
        assert scanned == [PLUGIN_GROUP]
