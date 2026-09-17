# cell() inside f-strings: fix report

## Bug

`cell()` returns a `rich.text.Text`; its markup protection is a property of
the object, recognised by `Console.print()`. Interpolating it into an
f-string calls `str()` on the `Text`, which discards that protection and
hands Rich a plain `str` it then markup-parses — either silently swallowing
bracketed content (e.g. `[bold]`) or raising `MarkupError` on anything
shaped like a closing tag (e.g. `[/dim]`). `esc()` is the correct helper for
f-string interpolation.

## Sites changed (25 total, `cell(` → `esc(`)

- `dataplat/cli/people/onboard.py:192,193,231,232,233,238,355,369,370,433,436` (11)
- `dataplat/cli/people/offboard.py:122,145,266,270,271` (5)
- `dataplat/cli/db/schema_impact.py:169,193,194,198,199,203` (6)
- `dataplat/cli/db/schema_drop.py:203,209` (2)
- `dataplat/cli/db/role_grant.py:280` (1)

None of these sites passed `max_length`, so no `shorten()` wrapping was
needed anywhere — confirmed by inspecting each site before editing, not
assumed.

Every legitimate `cell(...)` usage as a direct argument to `table.add_row(...)`
or `console.print(cell(...))` was left untouched, e.g.:
- `onboard.py:209-211`, `offboard.py:137-139` (table rows)
- `schema_impact.py:183-184` (table row)
- `schema_drop.py:74-75` (table row)

## Import changes

- **Kept `cell` import** (still used for `table.add_row`):
  `onboard.py`, `offboard.py`, `schema_impact.py`
- **`schema_drop.py`**: already imported `esc` alongside `cell` before this
  change (used elsewhere in the file); no import edit needed, `cell` stays
  for its table-row use.
- **`role_grant.py`**: `cell(` at line 280 was its *only* use of `cell` in
  the file, so the import was changed from
  `from dataplat.cli._render import cell, esc` to
  `from dataplat.cli._render import esc`.

## Formatting note

After the swap, `ruff format` reflowed one call site in `offboard.py`
(lines 269-271) from two lines to one, because `esc(...)` is shorter than
`cell(...)` and now fits under the line-length limit. This is a pure
formatting consequence of the mechanical swap, not a logic change; `ruff
format` was run once, scoped to that file, to satisfy the format gate.

## Mutation evidence (tests discriminate)

1. **Structural guard**, mutated a real call site back to `cell(`
   (`onboard.py:355`): `test_no_call_site_wraps_cell_in_an_f_string` failed,
   naming exactly `dataplat/cli/people/onboard.py:355` in the assertion
   message. Reverted; test passes again.
2. **Behavioural test 1** (silent-swallow), mutated
   `test_esc_survives_a_bracketed_value_that_looks_like_a_style_name` to use
   `cell(...)` instead of `esc(...)`: failed with
   `assert 'user db_[bold]prod' in 'user db_prod\n'` (the `[bold]` text was
   consumed as markup). Reverted; test passes again.
3. **Behavioural test 2** (crash), mutated
   `test_esc_does_not_crash_inside_our_own_markup` to use `cell(...)`:
   failed with `rich.errors.MarkupError: closing tag '[/dim]' at position 20
   doesn't match any open tag`. Reverted; test passes again.

All three mutations were reverted and the full suite reconfirmed green
before finishing.

## Gate results (final, on the restored/fixed tree)

- `uv run pytest -q`: **2213 passed, 273 skipped** (2210 passed on `main`
  baseline + 3 new tests in `tests/cli/test_render.py`)
- `uv run ruff check .`: **All checks passed!**
- `uv run ruff format --check .`: **216 files already formatted**
- `uv run mypy dataplat`: **Success: no issues found in 116 source files**

## Environment note (not a code issue)

While writing the behavioural tests, an ad hoc script run directly (outside
pytest) showed ANSI escape codes appearing around brackets even with
`no_color=True` passed to `Console`. This was a red herring caused by
`FORCE_COLOR`/`CLICOLOR` being set in the shell — `tests/conftest.py`
already pops both before any `dataplat` import, which is why the real test
suite (and the new tests) render deterministically. No code change was
needed; noting it here in case it resurfaces for someone testing ad hoc.

## Same class of bug, but not this literal pattern

While reviewing every `cell(` call site to distinguish legitimate uses from
this bug (`grep -rn '\bcell(' dataplat`), everything found in `describe.py`,
`role.py`, `role_create.py`, `role_drop.py`, `role_list.py`, `schema_list.py`,
`top_tables.py`, `long_queries.py`, `bi/superset.py`, `cloud/aws/*.py`,
`ingest/airbyte/*.py`, `open.py`, `config.py`, and `db/__init__.py` passes
`cell(...)` directly as a `table.add_row`/`add_column` argument or straight
into `console.print(cell(...))` — never inside an f-string. Nothing else in
the package matched the anti-pattern. No additional instances of "the same
class of bug" (a Rich-renderable's markup protection lost via implicit
`str()`) were found elsewhere, e.g. no site interpolates a `Text` object
into an f-string by another route.
