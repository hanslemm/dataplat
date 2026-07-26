"""Test-wide environment normalization.

Rich consoles are created at import time, so color-forcing variables from the
developer's shell (FORCE_COLOR, CLICOLOR) must be cleared before any
dataplat module is imported. Keeping output colorless makes CliRunner
assertions deterministic across machines and CI.
"""

from __future__ import annotations

import os

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
os.environ["DP_TARGETS"] = "demo_pg,demo_rs"
os.environ["DEMO_PG_ENGINE"] = "postgresql"
os.environ["DEMO_PG_REASSIGN_OWNER"] = "demo_pg_root"
os.environ["DEMO_RS_ENGINE"] = "redshift"
os.environ["DEMO_RS_REASSIGN_OWNER"] = "admin"
os.environ.pop("DP_DEFAULT_TARGET", None)
os.environ.pop("DP_ENVRC_PATH", None)

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
