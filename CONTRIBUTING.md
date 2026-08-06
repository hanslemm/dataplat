# Contributing to dataplat

## Setup

```bash
git clone https://github.com/hanslemm/dataplat
cd dataplat
uv sync --group dev --all-extras
```

## Checks

The four gates CI runs, across Python 3.12 and 3.13:

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy dataplat
```

`uv run pytest` is green without Docker: the PostgreSQL-backed tests skip. The
DuckDB ones do not — there is no server for them to miss, so they always run
(see below). To exercise the PostgreSQL half, start a server and point the suite
at it:

```bash
docker run -d --name dp-pg-test \
  -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=dataplat_test \
  -p 55432:5432 postgres:16 -c shared_preload_libraries=pg_stat_statements
docker exec dp-pg-test psql -U postgres -d dataplat_test \
  -c 'CREATE EXTENSION IF NOT EXISTS pg_stat_statements'

DP_TEST_PG_REQUIRED=1 uv run pytest              # everything
uv run pytest -m "not integration"               # deselect the real-database tiers
docker rm -f -v dp-pg-test                       # -v, or the volume dangles
```

`DP_TEST_PG_REQUIRED=1` turns an unreachable server into an error instead of a
skip. CI sets it; without it a broken database would make the whole suite skip
and still report success.

### Coverage

With a server running, one command gives the whole figure:

```bash
DP_TEST_PG_REQUIRED=1 uv run pytest --cov --cov-report=term-missing
```

CI cannot do it that simply, because no single job there runs the whole suite:
`checks` deselects the tier that needs a server and `integration` deselects
everything else. Each uploads its own data file and a third job combines them —
the unit leg alone reads 87%, the integration leg 30%, and only the union means
anything. If you are reproducing what CI reports, combine the same way:

```bash
mkdir -p .covstash
uv run pytest -m "not integration" --cov --cov-report= --cov-fail-under=0
mv .coverage .covstash/unit         # outside the .coverage* pattern, which
                                    # pytest-cov erases when it starts.
                                    # .covstash/ is gitignored.
DP_TEST_PG_REQUIRED=1 uv run pytest -m integration --cov --cov-report= --cov-fail-under=0
mv .coverage .coverage.integration
cp .covstash/unit .coverage.unit
uv run coverage combine && uv run coverage report
```

**What the number is for, and what it is not.** `fail_under` is a floor that
catches a test file which stopped running, or a sizeable module that arrived
without tests. It is not evidence of correctness and should not be treated as a
goal. Coverage records which lines *executed*; the `ESCAPE '\'` that broke
`dp db top-tables` on every Redshift target for five releases was covered the
whole time it was broken — executed by passing tests, against a PostgreSQL server
whose default setting hid it. Every defect found in this project so far came from
running SQL against a real engine. Raise the floor when the figure rises for a
reason; do not chase it.

Commits follow [Conventional Commits](https://www.conventionalcommits.org/).

## Testing against DuckDB

There is nothing to provision. DuckDB is a library that opens a file inside the
test process, so the fixtures in `tests/integration/duckdb/` build a throwaway
database in pytest's `tmp_path`:

```bash
uv run pytest tests/integration/duckdb        # ~1s, no server, no environment
```

This tier deliberately has **none** of the availability machinery the other two
have: no DSN variable, no `_REQUIRED` switch, no disposability gate, no
read-only guard, and no skip path. Its `conftest.py` argues each omission at
length; the short version is that `DP_TEST_*_REQUIRED` exists because a
silently-skipping tier reports green having executed none of the SQL it was
written to validate — and here there is nothing that *can* be unavailable. The
one thing that can be missing is the `duckdb` package, which is a broken dev
environment rather than absent infrastructure (`uv sync --group dev
--all-extras`), so it is a hard collection error carrying the install hint,
never a skip.

It carries the `integration` marker, because everything under
`tests/integration/` does — that is the safety net which keeps
`-m 'not integration'` airtight. Select it by path, as above, when you want it
without the PostgreSQL container.

Isolation is a fresh database file per test rather than the PostgreSQL harness's
BEGIN/ROLLBACK. DuckDB *does* roll back DDL, so the trick would work
mechanically — but a table created inside an open transaction reports
`estimated_size = 0`, and `pragma_database_size()` reports `0 bytes` until a
`CHECKPOINT` that cannot run in a writing transaction, so every row-estimate and
size assertion would have passed against zeros. Both facts are pinned by tests,
so the decision can be re-checked instead of re-derived.

## Testing against a real Redshift cluster

Redshift is a managed service, so there is no container and CI cannot cover it.
If you have a cluster, you can. The suite is in `tests/integration/redshift/` and
is split into two tiers, because they need different permission to run:

| Marker | Mutates? | Needs |
| --- | --- | --- |
| `redshift` | no — read-only, safe against a warehouse in use | a reachable cluster |
| `redshift_ddl` | **yes** | a cluster you can throw away |

Credentials come from an ordinary dataplat target, so they stay in your own
`.envrc` and never reach the repo:

```bash
export DP_TARGETS=warehouse WAREHOUSE_ENGINE=redshift \
    WAREHOUSE_HOST=... WAREHOUSE_USER=... WAREHOUSE_DATABASE=... \
    WAREHOUSE_PASSWORD=...
export DP_TEST_RS_TARGET=warehouse      # or DP_TEST_RS_DSN=... as an escape hatch

DP_TEST_RS_REQUIRED=1 uv run pytest -m redshift        # read-only tier
```

| Variable | Effect |
| --- | --- |
| `DP_TEST_RS_TARGET` | a dataplat target name, resolved by the tool's own config |
| `DP_TEST_RS_DSN` | a raw libpq URL, if you would rather not declare a target |
| `DP_TEST_RS_REQUIRED` | an unreachable cluster becomes an error instead of a skip |
| `DP_TEST_RS_DISPOSABLE` | **required** before any `redshift_ddl` test will run |
| `DP_TEST_RS_SCHEMA` | a schema the read-only tier may inspect (otherwise discovered) |

A plain `uv run pytest` is unaffected: with nothing configured, both tiers skip.

### Why there is a client-side read-only guard

`rs_cursor` refuses anything that is not plainly a read *before it is sent*, on
top of the server-side `READ ONLY` transaction. Two layers, because the cluster
may be production: Redshift roles are cluster-wide, and its transactional-DDL
semantics differ from PostgreSQL's, so the rollback-per-test isolation the
PostgreSQL harness relies on cannot be assumed to clean up a mistake. A
server-side check would refuse the statement too — but only after it crossed the
network to a warehouse someone depends on.

It denies by default: only `SELECT`, `WITH … SELECT`, `EXPLAIN` and `SHOW` pass.
It is not fooled by a leading comment, case, a stray semicolon, a second
statement smuggled after a `SELECT`, a data-modifying CTE, `SELECT … INTO`, or a
side-effecting builtin such as `pg_terminate_backend`. The statement splitter is
hand-written rather than regex-based because a regex that ignores quoting can
*hide* a statement — naive comment stripping turns `SELECT '--' ; DROP TABLE t`
into a harmless-looking fragment plus a `DROP` the server will happily run. The
one hole it cannot close is an unlisted side-effecting UDF; that is what the
server-side layer is for, and `assert_read_only`'s docstring says so.

### What a run does and does not prove

A green read-only run proves dataplat's `SELECT`s are **valid Redshift SQL
against a real server, returning results that unpack** — which is precisely the
class of defect the PostgreSQL suite found repeatedly in these same functions
(an empty `pg_partition_tree`, a masked column, a view that does not cover
schemas). It cannot tell you anything about `GRANT`, `DROP`, `RENAME` or session
termination; those need the DDL tier and a disposable cluster.

The run also prints a conformance table of what the cluster answered, because
the point is learning what the engine does — a green run that recorded nothing
has taught nobody anything.

## Dialect changes: what counts as evidence

`dataplat/services/db` targets three engines, and they are not equally knowable:

| Engine | SQL really executed? | What it costs to find out |
| --- | --- | --- |
| PostgreSQL | yes, in CI | a container |
| DuckDB | yes, in CI — and its tier cannot skip | nothing: a library and a temp file |
| Redshift | **no** — generated and asserted, never run | a cluster you own; CI cannot have one |

That asymmetry is the whole reason this section exists, and it is now lopsided in
one direction only. PostgreSQL has a real integration suite behind it. So does
DuckDB, and DuckDB is the cheap case: there is no server to be unreachable and
nothing to provision, so a claim about it can always be checked *before* it is
made. Redshift has none and cannot get one cheaply — it is a managed service, so
there is no container to run in CI.

Which *commands* an engine refuses, and why, is declared in one place:
`dataplat/services/db/capabilities.py` — one entry per engine with no defaults,
so adding an engine and forgetting what it can do is a construction error there
rather than a wrong answer somewhere downstream. Change what an engine can be
asked there and nowhere else, and cite the code or the probe the entry rests on,
exactly as the existing ones do.

A command that still runs but has to leave a *section* out is the other case, and
it declares that next to the section (`services/db/describe.py`'s
`NotApplicable`) and prints it — `dp db describe` on DuckDB ends with the list of
sections the engine has no concept of, with a reason each. Either way the reason
is written once, next to the fact, and a refusal never says "not implemented"
about something the engine simply is not.

### The `scs` leg: one Redshift setting, on a server CI can have

The `integration` job runs its whole tier twice, once with
`standard_conforming_strings` on and once off. That single setting is the one
documented Redshift behaviour reproducible on PostgreSQL, and it matters out of
proportion to its size: with it off, a backslash inside a string literal escapes
what follows it, so `ESCAPE '\'` leaves the literal unterminated and the statement
never parses. That is what shipped `dp db top-tables` and `dp db dbt-orphans`
broken on every Redshift target from 0.2.1 through 0.4.0 — five releases, with
every test passing against a server whose default hid it.

**Read the leg for what it is.** It is not a Redshift emulation and must not be
cited as one: Redshift also has an 8.0.2 leader node, no `aclexplode()`, and
different catalogs, none of which a setting reproduces. What the leg proves is
narrower — *no SQL a PostgreSQL target runs depends on that setting being on* — and
it raises nothing to evidence class 0 for Redshift.

What it does buy is a standing guard on the one bug class that has actually
escaped this project twice. It earned itself on its first run, failing
`role_admin._LIST_ROLES_SQL`: the same `ESCAPE '\'`, in a Postgres-only query,
harmless on a default server and fatal on one carrying the legacy setting.

Practically: **write SQL that needs no backslash.** `#` as the `LIKE` escape (see
`services/db/_like.py`) and `[[:space:]]` rather than `\s`. Both behave identically
whatever the setting is, on both engines, which sidesteps the class instead of
getting the escaping right twice. One deliberate exception is documented at
`long_queries.build_long_queries_query`, which is Redshift-only and doubles its
backslash *because* that server has the setting off; unifying it needs a real
cluster, so it waits for one.

### DuckDB: the rules apply, and the excuse does not

The evidence classes below apply to DuckDB unchanged in principle — same
ordering, same requirement to record in the code *why* a line is the way it is.
In practice you will never reach past the first one, because the strongest class
(0: confirmed against a real engine) is always available: `uv run pytest
tests/integration/duckdb` runs in about a second, needs no server, no
credentials and no container, and cannot skip.

So, plainly: **"I could not test it" is never an acceptable reason for a DuckDB
change.** If a DuckDB behaviour is in doubt, open a connection and find out —
then turn the probe into a test in `tests/integration/duckdb/`, so the next
person inherits the answer instead of the doubt. A comment recording an
*unverified* DuckDB assumption is not a legitimate outcome here the way it is for
Redshift; it is a probe someone declined to run.

The corollary applies to the capability matrix too. Every DuckDB `False` in
`capabilities.py` names something the engine does not have, and
`tests/integration/duckdb/test_duckdb_services.py` asserts the declaration
*alongside the missing catalog* — so a future DuckDB that grows `pg_roles` fails
the suite instead of quietly going on being refused.

### Redshift: weighing evidence without a server

For a while the rule was simply "don't touch SQL that runs on Redshift, because
you can't test it." That is a good instinct and a bad rule. Applied literally it
blocked seven known defects, and when they were finally looked at one at a time,
six were fixable and only one genuinely needed Redshift-specific SQL. Five did
not touch Redshift SQL at all, and the sixth turned out to use a construct the
codebase was already shipping to Redshift elsewhere.

So the question is not "can I test this?" but **"what evidence do I have?"** A
change affecting the Redshift path needs at least one of the following, in
descending order of strength:

0. **A conformance run confirmed it against a real cluster.** Strongest, and the
   only one that is evidence rather than inference — see the section above. A fix
   currently resting on class 2 should be upgraded to class 0 when someone runs
   the suite, and revisited if the run *refutes* it. `test_conformance.py` names
   the assumptions each shipped fix depends on for exactly this reason.

1. **It changes no Redshift SQL.** The fix is pure Python, or touches only a
   `_*_SQL_POSTGRES` constant. Dialect risk is zero by construction — verify
   that claim honestly, then go ahead. Aggregation bugs, error handling, and
   return-value shaping usually land here.

2. **The construct is already in production on the Redshift path, *and*
   somebody has seen it work there.** Cite the file and line — and be honest
   about whether anyone has actually run it. Internal precedent is the same
   server and the same driver, which is why it ranks this high, but it only
   proves the code *ships*.

   That distinction is not hypothetical. `ESCAPE '\'` was added to `orphans.py`
   on the grounds that `top_tables.py` had always sent it to both engines. It
   had — and it had always been broken there: Redshift runs with
   `standard_conforming_strings` off, so the backslash escapes its own closing
   quote and the statement is a syntax error. The precedent existed, nobody had
   ever run that command against Redshift, and citing it propagated the bug into
   a second module. The escape character is now `#`, and
   `tests/integration/test_top_tables_pg.py` reproduces the failure by opening a
   PostgreSQL session with the Redshift setting.

   So: precedent from a code path with *no* executing coverage is class 4, not
   class 2. Ask "has anyone run this?" before you rely on it.

3. **It withdraws a claim rather than making one.** Replacing a confidently
   wrong value with "unknown" cannot be more wrong than what it replaced. See
   the standing rule below.

4. **Documented Redshift behaviour, cited, plus a fake-cursor test** pinning the
   SQL the Redshift branch emits. Weakest of the four, because documentation and
   deployed reality drift. Use it when the change is worth the residual risk,
   and say so in the commit.

If none of the four applies, **do not guess.** Leave the defect, and record it
in a comment next to the code it affects — not in a tracker nobody reads. The
comment is what lets the next person re-evaluate instead of rediscovering.

### Requirements either way

- **Keep the engine constants split.** Never edit a `_*_SQL_REDSHIFT` constant
  to fix a PostgreSQL bug. If a shared statement needs to diverge, split it and
  leave the Redshift half byte-for-byte as it was. The DuckDB statements are
  split the same way (`_*_SQL_DUCKDB`, `_DUCKDB_*_SQL`), with one difference: you
  can run them, so a DuckDB constant you touched comes back through
  `tests/integration/duckdb/`, not through inspection.
- **Add a fake-cursor test** asserting what the Redshift branch emits. It is the
  only mechanism that covers that path at all, and it catches the common
  accident of "fixed both branches when I meant one". DuckDB's equivalent is an
  *executing* test — a fake cursor there proves less than the real engine does,
  for the same effort.
- **Do not translate placeholders between engines.** psycopg binds `%s`; DuckDB
  binds `?`. Rewriting one into the other means deciding which `%` in a
  statement is a literal, and getting that wrong changes the SQL — so
  `DuckDbCursor` passes statements through untouched, and DuckDB SQL is written
  for DuckDB.
- **Record the evidence in the code**, not just the commit message. A future
  reader deciding whether they may touch the line needs to see why it is the way
  it is.

### Standing rule: prefer "unknown" to a confident falsehood

`dp db role show` used to print `Password set: yes` for every role, including
passwordless ones, because the column it read is masked to `'********'` and can
never be NULL. A report that states something false is worse than one that
admits a gap — especially a report someone is using for an audit. When the
server will not tell you, say so, and say why in the same breath: a bare
"unknown" reads as a tool defect.

`dp db top-tables` on DuckDB is the same rule applied to a whole column, and it
shows where the rule ends. DuckDB has no per-relation size function and no
catalog column carrying bytes, so every `size_bytes` there is `None` — never `0`,
because a table whose size cannot be read is not an empty table, and `--json`
consumers have to be able to tell those apart.

The table rendering then goes one step further and *drops* the Size and
"% of disk" columns instead of filling them with `—`, printing the basis for the
numbers it does show. Withdrawing a claim is not enough on its own when the gap
is an entire column: a report of dashes under a `0 B (0.0% of disk)` footer reads
as a broken tool, not as the engine's answer. Say what you cannot know, then
show what you can.
