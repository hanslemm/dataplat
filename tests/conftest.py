"""Test-wide environment normalization.

Rich consoles are created at import time, so color-forcing variables from the
developer's shell (FORCE_COLOR, CLICOLOR) must be cleared before any
dataplat module is imported. Keeping output colorless makes CliRunner
assertions deterministic across machines and CI.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

os.environ.pop("FORCE_COLOR", None)
os.environ.pop("CLICOLOR", None)
os.environ.pop("CLICOLOR_FORCE", None)
# Rich treats GitHub Actions as an ANSI-capable terminal and force-enables
# styling, which breaks substring assertions on help output in CI.
os.environ.pop("GITHUB_ACTIONS", None)
os.environ["TTY_COMPATIBLE"] = "0"
os.environ["NO_COLOR"] = "1"
os.environ["COLUMNS"] = "200"

# Config-driven DB targets for the whole test suite. Set before any dataplat
# import: the target registry and option defaults read the environment early.
#
# Assigned rather than setdefault-ed on purpose: what the suite's fixtures see
# must not depend on the developer's shell. The one exception is a real Redshift
# target named by DP_TEST_RS_TARGET — the Redshift tier resolves it through
# dataplat's own config, which is the point of that variable, and a plain
# assignment here silently erased it, so the documented target form could never
# work under pytest. Appending keeps the demo targets authoritative and lets a
# named cluster resolve alongside them.
#
# demo_pg2 is a second Postgres target sharing demo_pg's engine, deliberately:
# it exists so a test can prove two same-engine targets stay distinguishable
# from each other (by target name, not by engine family) in a fan-out — see
# tests/cli/test_dbt_orphans.py's same-engine disambiguation tests. It is
# registered here (so `resolve_target("demo_pg2")` and connection-env lookups
# work) but is not declared by demo_project or demo_other by default; tests
# that need it in a project's fan-out monkeypatch that project's own
# `*_DBT_TARGETS` locally, the same way the DuckDB-target tests already
# monkeypatch one in to prove a different capability gate.
_DEMO_TARGETS = "demo_pg,demo_pg2,demo_rs"
_RS_TARGET = os.environ.get("DP_TEST_RS_TARGET", "").strip()
os.environ["DP_TARGETS"] = (
    f"{_DEMO_TARGETS},{_RS_TARGET}"
    if _RS_TARGET and _RS_TARGET not in _DEMO_TARGETS.split(",")
    else _DEMO_TARGETS
)
os.environ["DEMO_PG_ENGINE"] = "postgresql"
os.environ["DEMO_PG_REASSIGN_OWNER"] = "demo_pg_root"
os.environ["DEMO_PG2_ENGINE"] = "postgresql"
os.environ["DEMO_PG2_REASSIGN_OWNER"] = "demo_pg2_root"
os.environ["DP_DBT_PROJECTS"] = "demo_project,demo_other"
os.environ["DEMO_PROJECT_DBT_PATH"] = str(
    Path(__file__).parent / "fixtures" / "demo_project"
)
os.environ["DEMO_PROJECT_DBT_TARGETS"] = "demo_pg,demo_rs"
os.environ["DEMO_OTHER_DBT_PATH"] = str(
    Path(__file__).parent / "fixtures" / "demo_other"
)
os.environ["DEMO_OTHER_DBT_NAME"] = "explicit_name"
os.environ["DEMO_OTHER_DBT_TARGETS"] = "demo_rs"
os.environ["DEMO_RS_ENGINE"] = "redshift"
os.environ["DEMO_RS_REASSIGN_OWNER"] = "admin"
os.environ.pop("DP_DEFAULT_TARGET", None)
# dataplat.main calls load_envrc() at import time, and popping DP_ENVRC_PATH
# alone only drops the developer's *override*: the lookup then falls through
# to the global link (~/.config/dataplat/.envrc, created by `dp config init`)
# and loads that developer's real targets, DP_DEFAULT_TARGET and credentials
# into the suite. Point the override at an empty file instead so the loader
# finds something and stops there. An empty file parses to no exports.
with tempfile.NamedTemporaryFile(
    prefix="dp-test-envrc-", suffix=".envrc", delete=False
) as _empty_envrc:
    pass
os.environ["DP_ENVRC_PATH"] = _empty_envrc.name

# Isolate the suite from connection/config env in the developer's shell.
for _var in (
    "PGHOST",
    "PGPORT",
    "PGUSER",
    "PGPASSWORD",
    "PGDATABASE",
    "PGSSLMODE",
    "PGCLIENTENCODING",
    "DB_HOST",
    "DB_PORT",
    "DB_USER",
    "DB_PASSWORD",
    "DB_NAME",
    "DB_SSLMODE",
    "DP_AWS_PROFILE",
    "DP_AWS_PROFILE_ALIASES",
    "DP_AWS_REGION",
    "DP_RDS_INSTANCE",
    "DP_DBT_PROJECT",
    "DP_DBT_INVOCATION_COMMAND",
    "DP_DBT_ORPHANS_EXCLUDE_SCHEMAS",
    "DP_CI_RUNNER_DNS",
):
    os.environ.pop(_var, None)
