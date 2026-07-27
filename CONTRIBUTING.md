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

`uv run pytest` is green without Docker: the database-backed tests skip. To run
them, start a server and point the suite at it:

```bash
docker run -d --name dp-pg-test \
  -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=dataplat_test \
  -p 55432:5432 postgres:16 -c shared_preload_libraries=pg_stat_statements
docker exec dp-pg-test psql -U postgres -d dataplat_test \
  -c 'CREATE EXTENSION IF NOT EXISTS pg_stat_statements'

DP_TEST_PG_REQUIRED=1 uv run pytest              # everything
uv run pytest -m "not integration"               # skip the database half
docker rm -f -v dp-pg-test                       # -v, or the volume dangles
```

`DP_TEST_PG_REQUIRED=1` turns an unreachable server into an error instead of a
skip. CI sets it; without it a broken database would make the whole suite skip
and still report success.

Commits follow [Conventional Commits](https://www.conventionalcommits.org/).

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

`dataplat/services/db` targets PostgreSQL and Redshift. PostgreSQL has a real
integration suite behind it. Redshift has none and cannot get one cheaply — it
is a managed service, so there is no container to run in CI.

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

2. **The construct is already in production on the Redshift path.** Cite the
   file and line. `ESCAPE '\'` was safe to add to `orphans.py` because
   `top_tables.py` had always sent it to both engines;
   `has_schema_privilege(...)` is safe in `describe.py`'s Redshift branch
   because that branch already calls it. Internal precedent beats
   documentation: it is the same server, the same driver, and code someone is
   already running.

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
  leave the Redshift half byte-for-byte as it was.
- **Add a fake-cursor test** asserting what the Redshift branch emits. It is the
  only mechanism that covers that path at all, and it catches the common
  accident of "fixed both branches when I meant one".
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
