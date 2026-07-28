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

Requires Python 3.12 or newer, on Linux or macOS. Windows is untested and parts
of it will not work: `dp config init` creates a symlink, the dependency
self-install re-execs the process, and `dp ci github runner` drives `docker`.

```bash
uv tool install "dataplat[all]"     # recommended: everything
# or
pipx install "dataplat[all]"
# or
pip install "dataplat[all]"
```

Each area's dependencies are an optional extra, so you can also install
only what your platform uses:

| Extra | Enables | Pulls in |
| --- | --- | --- |
| `db` | `dp db` | psycopg |
| `ingest` | `dp ingest` | httpx, textual, croniter |
| `bi` | `dp bi` | httpx |
| `cloud` | `dp cloud` | boto3, plotext |
| `all` | everything | all of the above |

A bare `pip install dataplat` gives you the core (`status`, `open`,
`config`) with every other area stubbed. You don't have to plan this in
advance: `dp` knows which areas your configuration enables and installs
what's missing on demand — see below.

## Auto-installing dependencies

Two ways, both of which detect whether `dp` runs from a uv tool, pipx, or
plain-venv install and use the matching installer:

```bash
dp config sync            # detect enabled areas, install missing deps (confirms)
dp config sync --check    # report only; exit 1 if something is missing (CI-friendly)
```

Or just use a command: if your config enables an area whose extra is
missing, `dp db query ...` shows exactly what it will run, asks, installs,
and re-runs your original command. Non-interactive sessions never install
silently — they print the command and exit instead. `dp config doctor`
also reports per-area dependency status.

Either path only ever *adds* to your install: the command is pinned to the
`dataplat` version you already run, so installing an extra never upgrades the
tool underneath you, and it carries your existing extras along, so adding
`db` cannot drop an `ingest` you already had.

## Shell completion

```bash
dp --install-completion    # detect the shell, write the script, hook it up
dp --show-completion       # print it instead, and install it yourself
```

Completion takes effect in the next shell. bash, zsh and fish are supported;
`--show-completion` is the escape hatch when your rc file is managed by
something else (Nix, chezmoi, a dotfiles repo) or when shell detection fails.

`dp <TAB>` is answered from the area names alone and imports nothing.
Completing *inside* an area has to import it, because the subcommands it owes
the shell are that area's own — so the first `dp db <TAB>` pays for psycopg, and
an area whose extra is not installed completes to nothing rather than offering
to install it mid-keystroke.

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
4. the `.envrc` beside a development checkout of dataplat itself

Candidate 3 makes every command sensitive to where you run it: standing in a
cloned repo points `dp` at whatever host and credentials that repo's `.envrc`
exports. `dp config show` and `dp config doctor` always name the active file
*and* which candidate produced it, and warn when it came from the current
directory. Set `DP_ENVRC_ALLOW_CWD=0` to drop that candidate entirely and
rely only on the global link you chose.

## Configuration reference

| Variable | Purpose |
| --- | --- |
| `DP_ENVRC_PATH` | Explicit `.envrc` to load, ahead of every other candidate. |
| `DP_ENVRC_ALLOW_CWD` | Set to `0` to stop picking up `.envrc` from the current directory. |
| `DP_VERBOSE` | Set to `1` to trace every statement and request to stderr, for a whole session — same switch as `--verbose`. |
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
- **`--verbose`** — a root flag: show what the tool actually sent, on stderr.

### Exit codes

Exit codes are a contract, not an implementation detail — a wrapper script
branches on them long after it has stopped reading our output:

| Code | Meaning | Retry? |
| --- | --- | --- |
| `0` | Success. | — |
| `1` | Unexpected or not-yet-classified failure. Also a declined confirmation: "no" is not an error, but it is not "done" either. | No — you don't know what happened. |
| `2` | Invalid input: an unknown flag or target, a value that cannot be parsed, a combination of arguments that cannot work. | No — the command itself is wrong. |
| `3` | Configuration problem: missing connection settings, an unknown engine, an unset `AIRBYTE_BASE_URL` or `DP_DBT_PROJECT`. | No — a human has to fix the config. |
| `4` | Authentication failure: credentials rejected, a login endpoint that would not authenticate, `aws sso login` failed. | No — a new credential is needed. |
| `5` | External service failure: a call to Airbyte, Superset or AWS failed, timed out or returned something unusable; a warehouse that refused the operation. | **Yes** — the only class where a retry can help. |

`0`, `1` and `2` keep their conventional meanings. `2` is Click's own code for a
usage error, which is why invalid input shares it: `dp db query --format nope`
(Click's complaint) and `-t nosuchtarget` (ours) are one condition to the
caller — "you passed something I cannot use" — and splitting them by who noticed
would be a distinction with no use.

The point of the codes above `2` is that `5` is the one worth retrying, and `3`
and `4` are the ones you must never retry: no amount of sleeping and trying
again creates a missing config file or repairs a rejected password. Cap the
retries anyway — `5` means "the other end failed", which covers a warehouse
that was restarting *and* a `DROP` the server refused because something still
depends on it, and only the first of those gets better on its own.

```bash
dp db long-queries -t warehouse --json > queries.json
case $? in
  0) ;;
  5) echo "service unavailable; will retry" >&2; exit 75 ;;   # EX_TEMPFAIL
  *) echo "not retryable; fix and re-run" >&2; exit 1 ;;
esac
```

Treat `1` as "unknown", never as "retryable": it is the code for a failure
dataplat has not classified, so retrying it is a guess.

### Verbose tracing

`--verbose` (or `DP_VERBOSE=1`) answers the one question logs cannot: what did
`dp` actually send?

```bash
dp --verbose db query 'SELECT 1'              # root flag, before the subcommand
DP_VERBOSE=1 dp db long-queries 2> trace.log  # or for a whole session
dp --verbose db describe public 2>&1 >/dev/null | grep '\[dp:sql\]'
```

Every line is prefixed with its category — `[dp:sql]` or `[dp:http]` — and
collapsed onto one line, so the output greps cleanly:

```text
[dp:sql] connect me@db.example.com:5432/analytics engine=postgresql
[dp:sql] SELECT 1 FROM pg_namespace WHERE nspname = %s | 1 params bound
[dp:http] GET https://api.airbyte.com/v1/jobs?limit=20
[dp:http] GET https://api.airbyte.com/v1/jobs?limit=20 -> 200 143.8ms
```

SQL is traced *before* the statement runs, which is the point: the trace you
need is the one for the query that never came back, and a line written afterwards
would never be written at all. That is also why there is no duration on it — use
`dp db long-queries` for how long. HTTP gets two lines for the same reason, one
on the way out and one on the response; a line with no `-> status` partner *is*
the signal that a request hung, was refused, or never connected.

**It writes to stderr and never to stdout**, so `--json` and `--format csv` stay
machine-readable with tracing on. Piping into `jq` is still valid, and
`2>/dev/null` drops the trace without touching the data:

```bash
dp --verbose db query --format json 'SELECT 1' 2>/dev/null | jq
```

**Secrets are never traced.** Every message is redacted on the way out —
passwords (including the SQL `PASSWORD '…'` literal that role creation sends),
tokens, API keys, `Authorization` headers and credentials embedded in a URL all
become `***`. Parameter values, result rows and response bodies are not traced
at all: they are your warehouse's data, and a trace that scrolls the answer past
you has hidden the request it exists to show.

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

See [CONTRIBUTING.md](CONTRIBUTING.md) for the integration suite and the rules
for changing SQL that runs on Redshift.

```bash
git clone https://github.com/hanslemm/dataplat
cd dataplat
uv sync --group dev --all-extras
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy dataplat
```

CI runs those four across Python 3.12 and 3.13 — the floor the wheel
advertises as well as the pinned dev version.

Redshift cannot be containerized, so CI cannot cover it. If you run dataplat
against a Redshift cluster, you can verify your own deployment: point
`DP_TEST_RS_TARGET` at one of your targets and run the read-only tier
(`uv run pytest -m redshift`). It only issues `SELECT`s — a guard refuses
anything else before it reaches the server — and prints what your cluster
answered. See [CONTRIBUTING.md](CONTRIBUTING.md#testing-against-a-real-redshift-cluster).

### Integration tests against a real PostgreSQL

Most of the suite drives a fake database cursor. That proves a code path
*called* `execute`, never that the SQL it built is valid. The tests in
`tests/integration/` close that gap: they run the real statements against a
live PostgreSQL and check the results, so an invalid column reference or a
broken `GRANT` fails here instead of on your warehouse.

`uv run pytest` stays green **without Docker** — the suite skips itself when
no server is reachable. You only need the container to actually exercise it:

```bash
docker run -d --name dp-pg-test -p 55432:5432 \
    -e POSTGRES_PASSWORD=postgres \
    -e POSTGRES_DB=dataplat_test \
    postgres:16 -c shared_preload_libraries=pg_stat_statements

# Once per database: pg_stat_statements is a per-database extension.
docker exec dp-pg-test psql -U postgres -d dataplat_test \
    -c 'CREATE EXTENSION IF NOT EXISTS pg_stat_statements'

DP_TEST_PG_REQUIRED=1 uv run pytest -m integration
```

The `-c shared_preload_libraries=pg_stat_statements` is not optional for full
coverage: without it the extension installs but every *read* of the view fails
with `pg_stat_statements must be loaded via shared_preload_libraries`, so
`dp db long-queries --history` stays untested.

| Variable | Purpose |
| --- | --- |
| `DP_TEST_PG_DSN` | Connection string for the test server. Default: `postgresql://postgres:postgres@127.0.0.1:55432/dataplat_test`. |
| `DP_TEST_PG_REQUIRED` | Truthy ⇒ an unreachable server is a hard **error**. Unset ⇒ the tests skip. |

Set `DP_TEST_PG_REQUIRED=1` whenever a skip would be a lie — that is, always
in CI. Without it a broken container makes the tests vanish and the run goes
green having validated no SQL at all. CI runs the integration job with it set,
against a pinned PostgreSQL major, and the release workflow runs the same job
as a gate before publishing.

Each test runs in a transaction that is rolled back afterwards, so tests never
see each other's objects and nothing survives a failed run. To select the
fast, database-free subset explicitly, use `-m "not integration"`.

**Known gap: Redshift.** Redshift cannot be containerized, so every
Redshift-specific code path remains fake-tested only — its SQL is generated and
asserted against a fake cursor, never executed. Treat changes to Redshift
paths as unverified by CI and test them against a real cluster.

## Releasing

Bump `[project].version`, then tag `X.Y.Z` (bare semver, no `v` prefix) on
`main`. The release workflow refuses to publish unless the tag matches that
version, and runs the full check matrix *plus* the integration suite against a
real PostgreSQL first; only then does GitHub Actions build and publish to PyPI
via Trusted Publishing. A published version can be yanked but never replaced,
so the extra minutes buy a guarantee that the shipped SQL has at least been
parsed by a server. Commits follow
[Conventional Commits](https://www.conventionalcommits.org/).

## License

[MIT](LICENSE)
