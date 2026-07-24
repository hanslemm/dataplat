# dataplat

**One command to manage any shape of data platform.**

`dataplat` ships a single CLI, `dp`, with an area for each type of component
in a data platform: warehouses/databases, ingestion, BI, cloud, and CI. Point
it at *your* stack through environment variables — nothing about your
infrastructure is hardcoded.

```text
dp
├── status                 # one-shot health overview (--json, --no-aws)
├── open                   # airbyte | superset | rds [id] | redshift | secrets [name]
├── config                 # init | show | doctor [--connect]
├── db                     # warehouses & databases (Postgres, Redshift)
│   ├── query              # ad-hoc SQL (--format table|csv|json, --write guard)
│   ├── describe           # schema/table/view report (--json)
│   ├── long-queries       # triage per target (--history, --json)
│   ├── kill               # cancel/terminate queries by PID
│   ├── role               # list | show | create | drop
│   ├── top-tables         # rank big tables (--drop-sql, --drop)
│   └── dbt-orphans        # scan/rename | revert | purge (--older-than)
├── ingest                 # data ingestion
│   └── airbyte
│       ├── connections    # list | get | create | update | set-cursor
│       │                  # sync | refresh | reset | delete
│       ├── jobs           # list | get | cancel
│       ├── sources        # list | get | create | update | delete
│       ├── destinations   # list | get | create | update | delete
│       ├── definitions    # list-sources | list-destinations
│       ├── workspaces     # list | get
│       ├── tags           # list | create
│       └── templates      # source | destination | connection
├── bi                     # business intelligence
│   └── superset
│       ├── users          # list | create | update | delete
│       ├── roles          # list
│       └── groups         # list
├── cloud                  # cloud providers
│   └── aws
│       ├── secrets        # list | get | compare | set | edit | rename-key
│       │                  # describe | versions | rollback | delete | restore
│       ├── rds            # metrics | plot | list
│       └── redshift       # metrics
└── ci                     # build infrastructure
    └── github
        └── runner         # start | stop | status
```

## Installation

```bash
uv tool install dataplat     # recommended
# or
pipx install dataplat
# or
pip install dataplat
```

## Quick start

1. Declare your database targets — any names you like:

   ```bash
   # ~/.envrc (or any env mechanism you prefer)
   export DP_TARGETS="warehouse,lake"
   export DP_DEFAULT_TARGET="warehouse"

   export WAREHOUSE_ENGINE=postgresql
   export WAREHOUSE_HOST=db.example.com
   export WAREHOUSE_DATABASE=analytics
   export WAREHOUSE_USER=me
   export WAREHOUSE_PASSWORD=…

   export LAKE_ENGINE=redshift
   export LAKE_HOST=lake.abc123.eu-central-1.redshift-serverless.amazonaws.com
   export LAKE_DATABASE=dev
   export LAKE_USER=me
   export LAKE_PASSWORD=…
   export LAKE_REASSIGN_OWNER=admin      # role drop reassigns owned objects here
   ```

2. Optionally link that file globally and check your setup:

   ```bash
   dp config init --envrc ~/.envrc   # set the global link
   dp config show                    # which .envrc is active, what's set
   dp config doctor --connect        # validate config; probe live systems
   ```

3. Go:

   ```bash
   dp status
   dp db query 'SELECT 1'
   dp db query -t lake 'SELECT 1'
   ```

## Environment loading

`dp` loads variables from `.envrc` on startup without overriding values
already set in your shell. Lookup order:

1. `DP_ENVRC_PATH`
2. `~/.config/dataplat/.envrc` (the global link — set it with `dp config init`)
3. `.envrc` in the current directory

## Configuration reference

| Variable | Purpose |
| --- | --- |
| `DP_TARGETS` | Comma-separated DB target names (e.g. `warehouse,lake`). |
| `DP_DEFAULT_TARGET` | Target used when `--target` is omitted (default: first of `DP_TARGETS`). |
| `<NAME>_ENGINE` | `postgresql` (default) or `redshift`, per target. |
| `<NAME>_HOST/_PORT/_USER/_PASSWORD/_DATABASE/_SSLMODE` | Connection settings, per target. |
| `<NAME>_REASSIGN_OWNER` | Default owner for `dp db role drop` ownership transfer. |
| `AIRBYTE_BASE_URL` + `AIRBYTE_CLIENT_ID`/`AIRBYTE_CLIENT_SECRET` (cloud) or `AIRBYTE_EMAIL`/`AIRBYTE_PASSWORD` (OSS) | Airbyte API access. |
| `SUPERSET_BASE_URL`, `SUPERSET_ADMIN_USERNAME`, `SUPERSET_ADMIN_PASSWORD` | Superset API access. |
| `DP_AWS_PROFILE` | Default AWS profile for `dp cloud aws` commands. |
| `DP_AWS_PROFILE_ALIASES` | Short aliases, e.g. `prod=AdminAccess-Prod,qa=AdminAccess-QA`. |
| `DP_AWS_REGION` | Default AWS region (falls back to `AWS_REGION`, then the profile). |
| `DP_RDS_INSTANCE` | Default RDS instance for `dp cloud aws rds` / `dp status`. |
| `DP_DBT_PROJECT` | dbt project name — required for `dp db dbt-orphans`. |
| `DP_DBT_INVOCATION_COMMAND` | Optional filter on dbt_artifacts invocations. |
| `DP_DBT_ORPHANS_EXCLUDE_SCHEMAS` | Comma-separated schemas to skip (default `raw,_raw,dbt_artifacts`). |
| `GHA_APP_ID`, `GHA_APP_PRIVATE_KEY` | GitHub App creds for `dp ci github runner`. |
| `DP_CI_RUNNER_DNS` | Comma-separated DNS servers for the runner container. |

## Conventions

- **`-t/--target`** — named DB target from `DP_TARGETS`. Multi-target
  commands accept `all`. Sets the engine and env prefix in one flag;
  `--engine` / `--env-prefix` remain as overrides.
- **`--json`** — every read command can emit machine-readable output.
- **`--yes/-y`** — every destructive or bulk-mutating command confirms first;
  pass `--yes` in scripts. Bulk mutators also support `--dry-run`.
- **`--limit/-n`** — row caps share one spelling everywhere.
- **Secrets stay off argv** — prefer `--value-stdin` / hidden prompts; values
  are never echoed back.

## Examples

### Daily overview

```bash
dp status                  # DBs, Airbyte jobs (24h), runners, RDS at a glance
dp open superset           # jump to a web UI
```

### DB query

```bash
dp db query 'SELECT 1'                         # default target
dp db query -t lake 'SELECT 1'                 # named target
dp db query --format csv -n 0 'SELECT ...' > out.csv
echo 'SELECT 1' | dp db query
dp db query --write 'UPDATE t SET x = 1'       # writes need --write or a confirm
```

### Long queries and kill

```bash
dp db long-queries                       # all targets: running + recent failures
dp db long-queries -t warehouse --history    # pg_stat_statements aggregate
dp db kill 12345 -t warehouse            # terminate a backend (confirms first)
```

### DB roles

```bash
dp db role list --users-only
dp db role show alice -t lake --json
dp db role create svc_reporting --table-select reporting --databases analytics
dp db role create readers --no-login --table-select reporting   # passwordless group role
dp db role create readers --no-login --grant-to alice,bob
dp db role drop old_user --all-databases --dry-run
```

`list`, `create`, and `drop` work against both Postgres and Redshift targets.
`create` makes login roles with generated passwords by default; `--no-login`
creates a passwordless group-style role instead. `drop` transfers owned
objects to the target's `<NAME>_REASSIGN_OWNER` before `DROP USER`.

### Cleanup

```bash
dp db top-tables --schema-prefix dev_ -n 30
dp db top-tables --drop-sql > review.sql       # emit a script
dp db dbt-orphans                              # dry-run scan (default)
dp db dbt-orphans --no-dry-run                 # apply renames (confirms)
dp db dbt-orphans purge --older-than 7 --no-dry-run
dp db dbt-orphans revert                       # undo from the audit log
```

### AWS secrets

```bash
dp cloud aws secrets list --prefix /kubernetes
dp cloud aws secrets get /my/secret --key password
dp cloud aws secrets compare /my/secret -p prod -p qa
echo -n "hunter2" | dp cloud aws secrets set my/secret --value-stdin -p qa
dp cloud aws secrets versions my/secret
dp cloud aws secrets rollback my/secret        # AWSCURRENT -> AWSPREVIOUS
```

All writes show their targets and confirm (or `--yes`).

### AWS monitoring

```bash
dp cloud aws rds metrics --json
dp cloud aws rds plot -m cpu -m connections --hours 12
dp cloud aws redshift metrics -w my-workgroup
```

### Airbyte

```bash
dp ingest airbyte connections list -w <workspace-id> --json
dp ingest airbyte connections sync -c <connection-id> --wait
dp ingest airbyte connections reset -c <connection-id>

# Move every date-based cursor to a date, across all connections from one source
dp ingest airbyte connections set-cursor --source-id <id> --to 2024-01-01 --dry-run
dp ingest airbyte connections set-cursor -c <connection-id> --to 2024-01-01 --yes

# xmin (Postgres transaction-id) cursors: set them directly
dp ingest airbyte connections set-cursor -c <connection-id> --xmin 0 --yes
```

## Development

```bash
git clone https://github.com/hanslemm/dataplat
cd dataplat
uv sync --group dev
uv run pytest
uv run ruff check .
uv run mypy dataplat
```

## Releasing

Tag `vX.Y.Z` on `main`; GitHub Actions builds and publishes to PyPI via
Trusted Publishing.

## License

[MIT](LICENSE)
