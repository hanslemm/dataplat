"""`dp bi superset dashboards duplicate`, against a fake Superset.

The assertions that matter are about what is NOT sent: no write before every
dataset resolves, and no write at all to a chart that was never cloned.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from dataplat.cli.bi import superset as superset_cli
from dataplat.core.errors import ExitCode

runner = CliRunner()
WIDE = {"COLUMNS": "200"}


class FakeSuperset:
    """Records every write, so a test can assert that none happened."""

    def __init__(
        self,
        *,
        target_has_orders: bool = True,
        clone_shared: bool = False,
        virtual_sql_error: str | None = None,
        with_foreign_dataset: bool = False,
        duplicate_original_chart: bool = False,
        dataset_118_sql: str | None = None,
        dashboards_field: str = "normal",  # "normal" | "missing" | "empty"
        original_no_query_context: bool = False,
        original_bad_query_context: bool = False,
    ):
        self.writes: list[tuple[str, str, dict]] = []
        self.target_has_orders = target_has_orders
        self.clone_shared = clone_shared
        self.virtual_sql_error = virtual_sql_error
        self.with_foreign_dataset = with_foreign_dataset
        self.duplicate_original_chart = duplicate_original_chart
        # Overrides DATASET_118's "sql" field, so a test can give the ONE
        # dataset that goes through plan.to_create a construct the live check
        # would accept but the scan should still flag (fix round 12, finding
        # 1) -- the live check is a real Postgres-shaped SELECT, the scan
        # target does not need to be.
        self.dataset_118_sql = dataset_118_sql
        self.dashboards_field = dashboards_field
        # Chart 7 pairs with clone 70 ("Revenue", datasource 118) in every
        # --compare test; these two flip its query_context specifically, to
        # exercise the ORIGINAL's context being missing or unreadable without
        # touching any other chart's pairing (fix round 12, finding 3).
        self.original_no_query_context = original_no_query_context
        self.original_bad_query_context = original_bad_query_context
        self.chart_rows: list[dict] = [{"a": 1}]
        self.original_rows: list[dict] | None = None
        self.chart_error: str | None = None
        # Fails only the ORIGINAL's query (datasource id 118/121), so a test
        # can exercise "the original could not run" without also breaking the
        # clone's own query -- keying by datasource id is how /chart/data
        # already tells the two apart below.
        self.original_chart_error: str | None = None
        self.repointed: dict[int, int] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content) if request.content else {}
        # sqllab/execute is a read-only validation query (SELECT ... LIMIT 0)
        # run through SQL Lab, not a mutation of any Superset resource -- see
        # _validate_virtual's docstring. Recording it here would make "nothing
        # was written" false for a call that changes nothing.
        if (
            request.method in {"POST", "PUT"}
            and "login" not in path
            and "/sqllab/execute/" not in path
        ):
            self.writes.append((request.method, path, body))

        if request.method == "PUT" and "/chart/" in path and "datasource_id" in body:
            self.repointed[int(path.rsplit("/", 1)[-1])] = int(body["datasource_id"])

        if path.endswith("/security/login"):
            return httpx.Response(200, json={"access_token": "tok"}, request=request)
        if path.endswith("/database/"):
            return httpx.Response(
                200,
                json={
                    "result": [
                        {"id": 1, "database_name": "DataOcean"},
                        {"id": 2, "database_name": "BetterData"},
                    ]
                },
                request=request,
            )
        if path.endswith("/dashboard/42/charts"):
            charts = (
                CHARTS
                + ([FOREIGN_CHART] if self.with_foreign_dataset else [])
                + ([DUPLICATE_ORIGINAL_CHART] if self.duplicate_original_chart else [])
            )
            return httpx.Response(200, json={"result": charts}, request=request)
        if path.endswith("/dashboard/318/charts"):
            clones = CLONES + ([FOREIGN_CLONE] if self.with_foreign_dataset else [])
            return httpx.Response(200, json={"result": clones}, request=request)
        if path.endswith("/dashboard/42"):
            return httpx.Response(200, json={"result": DASHBOARD}, request=request)
        if path.endswith("/dashboard/318"):
            return httpx.Response(
                200, json={"result": {"id": 318, **COPY_DASHBOARD}}, request=request
            )
        if path.endswith("/copy/"):
            return httpx.Response(201, json={"result": {"id": 318}}, request=request)
        if path.endswith("/dataset/118"):
            dataset = dict(DATASET_118)
            if self.dataset_118_sql is not None:
                dataset["sql"] = self.dataset_118_sql
            return httpx.Response(200, json={"result": dataset}, request=request)
        if path.endswith("/dataset/121"):
            return httpx.Response(200, json={"result": DATASET_121}, request=request)
        if path.endswith("/dataset/140"):
            return httpx.Response(
                200, json={"result": FOREIGN_DATASET}, request=request
            )
        if path.endswith("/dataset/904"):
            return httpx.Response(
                200,
                json={
                    "result": {"id": 904, "columns": [{"id": 50, "column_name": "x"}]}
                },
                request=request,
            )
        if path.endswith("/dataset/") and request.method == "POST":
            return httpx.Response(201, json={"id": 904}, request=request)
        if path.endswith("/dataset/"):
            rows = [TARGET_USERS] + ([TARGET_ORDERS] if self.target_has_orders else [])
            return httpx.Response(200, json={"result": rows}, request=request)
        if path.endswith("/chart/data"):
            # 118/121 are the originals' datasets; anything else is the copy's.
            is_original = (body.get("datasource") or {}).get("id") in {118, 121}
            if is_original and self.original_chart_error:
                return httpx.Response(
                    400, json={"message": self.original_chart_error}, request=request
                )
            if self.chart_error:
                return httpx.Response(
                    400, json={"message": self.chart_error}, request=request
                )
            rows = (
                self.original_rows
                if is_original and self.original_rows is not None
                else self.chart_rows
            )
            return httpx.Response(
                200, json={"result": [{"data": rows}]}, request=request
            )
        if "/chart/" in path:
            chart_id = int(path.rsplit("/", 1)[-1])
            chart = dict(CLONE_BY_ID[chart_id])
            if chart_id in self.repointed:
                new = self.repointed[chart_id]
                chart["datasource_id"] = new
                chart["query_context"] = json.dumps(
                    {"datasource": {"id": new, "type": "table"}}
                )
            # Chart 7 is the original paired with clone 70 ("Revenue",
            # datasource 118) in every --compare test; these flip only ITS
            # query_context, so the "original missing/unreadable context"
            # cases can be exercised without disturbing chart 8/80's pairing.
            if chart_id == 7 and self.original_no_query_context:
                chart.pop("query_context", None)
            elif chart_id == 7 and self.original_bad_query_context:
                chart["query_context"] = "{not json"
            if self.dashboards_field == "missing":
                pass  # no "dashboards" key at all -- the absent-field case
            elif self.dashboards_field == "empty":
                chart["dashboards"] = []
            else:
                dashboards = [{"id": 318}]
                if self.clone_shared:
                    dashboards.append({"id": 99})
                chart["dashboards"] = dashboards
            return httpx.Response(200, json={"result": chart}, request=request)
        if path.endswith("/sqllab/execute/"):
            if self.virtual_sql_error:
                return httpx.Response(
                    400,
                    json={"errors": [{"message": self.virtual_sql_error}]},
                    request=request,
                )
            return httpx.Response(200, json={"status": "success"}, request=request)
        return httpx.Response(404, json={"message": path}, request=request)


DASHBOARD: dict[str, Any] = {
    "id": 42,
    "dashboard_title": "Revenue overview",
    "json_metadata": json.dumps(
        {
            "color_scheme": "supersetColors",
            "native_filter_configuration": [
                {"id": "F1", "targets": [{"datasetId": 121, "column": {"name": "x"}}]}
            ],
        }
    ),
    "position_json": json.dumps({"CHART-a": {"meta": {"chartId": 7}}}),
    "css": "",
}

# GET /dashboard/318's own json_metadata -- deliberately DIFFERENT from
# DASHBOARD's (color_scheme, and F2 rather than F1) so a test can prove phase
# 4's metadata PUT reads and remaps the COPY's metadata rather than silently
# putting the original's back (fix round 12, finding 4). Superset rewrote
# every chartId into THIS metadata during /copy/; that rewrite is what would
# be reverted by reusing DASHBOARD's own json_metadata here instead.
COPY_DASHBOARD: dict[str, Any] = {
    "json_metadata": json.dumps(
        {
            "color_scheme": "copy-own-scheme",
            "native_filter_configuration": [
                {"id": "F2", "targets": [{"datasetId": 121, "column": {"name": "x"}}]}
            ],
        }
    ),
}

# GET /dashboard/{id}/charts -- the listing shape -- never carries a
# top-level datasource_id; the dataset survives only inside
# form_data.datasource, as "<id>__table". CLONE_BY_ID below is the OTHER
# shape, GET /chart/{id}, which does carry it for real; both id and
# datasource_id there still matter, so it stays as-is.
CHARTS = [
    {
        "id": 7,
        "slice_name": "Revenue",
        "datasource_type": "table",
        "form_data": {"datasource": "118__table"},
    },
    {
        "id": 8,
        "slice_name": "Signups",
        "datasource_type": "table",
        "form_data": {"datasource": "121__table"},
    },
]

CLONES = [
    {
        "id": 70,
        "slice_name": "Revenue",
        "datasource_type": "table",
        "form_data": {"datasource": "118__table"},
    },
    {
        "id": 80,
        "slice_name": "Signups",
        "datasource_type": "table",
        "form_data": {"datasource": "121__table"},
    },
]
CLONE_BY_ID = {
    # The originals (7, 8, 9), never repointed -- kept here too, since
    # pairing a clone to its original for --compare reads them the same way
    # (a GET /chart/{id}), before the repoint erases the clone's old id.
    # slice_name is included here (not just in CHARTS/CLONES) because the
    # --compare pairing key is (slice_name, datasource_id): a chart fetched
    # by id alone, without its name, cannot be paired at all.
    7: {
        "id": 7,
        "slice_name": "Revenue",
        "datasource_id": 118,
        "datasource_type": "table",
        "params": json.dumps({"datasource": "118__table"}),
        "query_context": json.dumps({"datasource": {"id": 118, "type": "table"}}),
    },
    8: {
        "id": 8,
        "slice_name": "Signups",
        "datasource_id": 121,
        "datasource_type": "table",
        "params": json.dumps({"datasource": "121__table"}),
        "query_context": json.dumps({"datasource": {"id": 121, "type": "table"}}),
    },
    9: {
        "id": 9,
        "slice_name": "External",
        "datasource_id": 140,
        "datasource_type": "table",
        "params": json.dumps({"datasource": "140__table"}),
        "query_context": json.dumps({"datasource": {"id": 140, "type": "table"}}),
    },
    # A second original sharing chart 7's (slice_name, datasource_id): the
    # fixture for the ambiguous-pairing case (finding 3, fix round 1). Only
    # ever added to dashboard 42's charts when duplicate_original_chart=True.
    10: {
        "id": 10,
        "slice_name": "Revenue",
        "datasource_id": 118,
        "datasource_type": "table",
        "params": json.dumps({"datasource": "118__table"}),
        "query_context": json.dumps({"datasource": {"id": 118, "type": "table"}}),
    },
    70: {
        "id": 70,
        "slice_name": "Revenue",
        "datasource_id": 118,
        "datasource_type": "table",
        "params": json.dumps({"datasource": "118__table"}),
        "query_context": json.dumps({"datasource": {"id": 118, "type": "table"}}),
    },
    80: {
        "id": 80,
        "slice_name": "Signups",
        "datasource_id": 121,
        "datasource_type": "table",
        "params": json.dumps({"datasource": "121__table"}),
        "query_context": json.dumps({"datasource": {"id": 121, "type": "table"}}),
    },
    90: {
        "id": 90,
        "slice_name": "External",
        "datasource_id": 140,
        "datasource_type": "table",
        "params": json.dumps({"datasource": "140__table"}),
        "query_context": json.dumps({"datasource": {"id": 140, "type": "table"}}),
    },
}

DATASET_118 = {
    "id": 118,
    "table_name": "orders",
    "schema": "public",
    # Virtual: this is what exercises _validate_virtual's execute_sql call,
    # which every other test in this file leaves untouched.
    "sql": "select * from orders",
    "database": {"id": 1},
    "columns": [{"column_name": "x"}],
    "metrics": [],
}
DATASET_121 = {
    "id": 121,
    "table_name": "users",
    "schema": "public",
    "sql": None,
    "database": {"id": 1},
    "columns": [],
    "metrics": [],
}
TARGET_USERS = {
    "id": 887,
    "table_name": "users",
    "schema": "public",
    "database": {"id": 2},
}
TARGET_ORDERS = {
    "id": 904,
    "table_name": "orders",
    "schema": "public",
    "database": {"id": 2},
}

# A dataset on a THIRD connection -- not --from-database, not --to-database.
# plan_migration must report it and never repoint it; --map is not involved.
FOREIGN_CHART = {
    "id": 9,
    "slice_name": "External",
    "datasource_type": "table",
    "form_data": {"datasource": "140__table"},
}
FOREIGN_CLONE = {
    "id": 90,
    "slice_name": "External",
    "datasource_type": "table",
    "form_data": {"datasource": "140__table"},
}
FOREIGN_DATASET = {
    "id": 140,
    "table_name": "external",
    "schema": "other",
    "sql": None,
    "database": {"id": 9},
    "columns": [],
    "metrics": [],
}

# A second original chart reading the SAME dataset as chart 7, under the same
# name -- nothing distinguishes which one clone 70 was duplicated from once
# only (slice_name, datasource_id) survive to pair against. plan_migration
# never sees this (it operates on datasets, not charts), only the --compare
# pairing step does.
DUPLICATE_ORIGINAL_CHART = {
    "id": 10,
    "slice_name": "Revenue",
    "datasource_type": "table",
    "form_data": {"datasource": "118__table"},
}


@pytest.fixture
def superset_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPERSET_BASE_URL", "https://superset.test")
    monkeypatch.setenv("SUPERSET_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("SUPERSET_ADMIN_PASSWORD", "secret")


@pytest.fixture
def install(monkeypatch: pytest.MonkeyPatch):
    def _install(fake: FakeSuperset) -> FakeSuperset:
        def build(**kwargs: Any) -> httpx.Client:
            return httpx.Client(transport=httpx.MockTransport(fake.handler), **kwargs)

        monkeypatch.setattr("dataplat.cli.bi.dashboards_duplicate.build_client", build)
        return fake

    return _install


def _run(*args: str):
    return runner.invoke(
        superset_cli.app,
        [
            "dashboards",
            "duplicate",
            "42",
            "--from-database",
            "DataOcean",
            "--to-database",
            "BetterData",
            "--yes",
            "--no-verify",
            *args,
        ],
        env=WIDE,
    )


def _run_compare(*args: str):
    # Verify defaults on, so this is the one place --compare actually runs
    # the pairing and diff -- unlike _run, which turns verify off.
    return runner.invoke(
        superset_cli.app,
        [
            "dashboards",
            "duplicate",
            "42",
            "--from-database",
            "DataOcean",
            "--to-database",
            "BetterData",
            "--yes",
            "--compare",
            *args,
        ],
        env=WIDE,
    )


def test_a_dry_run_writes_nothing(superset_env, install) -> None:
    fake = install(FakeSuperset())

    result = _run("--dry-run")

    assert result.exit_code == ExitCode.SUCCESS, result.output
    assert fake.writes == []


def test_an_unresolvable_dataset_writes_nothing_and_exits_one(
    superset_env, install
) -> None:
    fake = install(FakeSuperset(target_has_orders=False))

    result = _run("--no-create-missing")

    assert result.exit_code == ExitCode.FAILURE, result.output
    assert fake.writes == []
    assert "public.orders" in result.output


def test_the_copy_is_sent_metadata_with_positions_merged_in(
    superset_env, install
) -> None:
    fake = install(FakeSuperset())

    result = _run()

    assert result.exit_code == ExitCode.SUCCESS, result.output
    copy = next(b for m, p, b in fake.writes if p.endswith("/copy/"))
    # Without this Superset cannot remap chartId and the copy silently stays
    # wired to the original's charts.
    assert json.loads(copy["json_metadata"])["positions"] == {
        "CHART-a": {"meta": {"chartId": 7}}
    }
    assert copy["duplicate_slices"] is True


def test_every_cloned_chart_is_repointed_in_all_three_places(
    superset_env, install
) -> None:
    fake = install(FakeSuperset())

    result = _run()

    assert result.exit_code == ExitCode.SUCCESS, result.output
    chart_writes = {
        int(p.rsplit("/", 1)[-1]): b
        for m, p, b in fake.writes
        if m == "PUT" and "/chart/" in p
    }
    assert chart_writes[70]["datasource_id"] == 904
    assert json.loads(chart_writes[70]["params"])["datasource"] == "904__table"
    assert json.loads(chart_writes[70]["query_context"])["datasource"]["id"] == 904
    assert chart_writes[80]["datasource_id"] == 887


def test_native_filters_are_remapped_on_the_copy(superset_env, install) -> None:
    fake = install(FakeSuperset())

    result = _run()

    assert result.exit_code == ExitCode.SUCCESS, result.output
    update = next(
        b for m, p, b in fake.writes if m == "PUT" and p.endswith("/dashboard/318")
    )
    metadata = json.loads(update["json_metadata"])
    assert metadata["native_filter_configuration"][0]["targets"][0]["datasetId"] == 887


def test_phase_4_metadata_put_carries_the_copys_own_metadata_not_the_originals(
    superset_env, install
) -> None:
    # PUTting the ORIGINAL's metadata back would revert every chartId
    # rewrite Superset made during /copy/ -- filter_scopes,
    # chart_configuration, expanded_slices would all still name the
    # ORIGINAL's charts. dashboard/318 and dashboard/42 are given
    # deliberately different json_metadata in this fixture (COPY_DASHBOARD
    # vs DASHBOARD) so this can tell which one the PUT actually reflects.
    fake = install(FakeSuperset())

    result = _run()

    assert result.exit_code == ExitCode.SUCCESS, result.output
    update = next(
        b for m, p, b in fake.writes if m == "PUT" and p.endswith("/dashboard/318")
    )
    metadata = json.loads(update["json_metadata"])
    assert metadata["color_scheme"] == "copy-own-scheme"


def test_a_chart_on_another_dashboard_refuses_without_writing_to_it(
    superset_env, install
) -> None:
    # duplicate_slices did not take effect; writing here would edit the
    # ORIGINAL dashboard's chart.
    fake = install(FakeSuperset(clone_shared=True))

    result = _run()

    assert result.exit_code == ExitCode.FAILURE, result.output
    assert not [p for m, p, _ in fake.writes if m == "PUT" and "/chart/" in p]
    assert "318" in result.output  # the copy's id, so it can be cleaned up


def test_a_clone_with_no_dashboards_field_at_all_is_refused_with_no_chart_put(
    superset_env, install
) -> None:
    # The guard used to check only for a FOREIGN id in "dashboards" -- an
    # absent list passed straight through, since it contains no foreign id
    # either. It must fail CLOSED instead: refuse whenever the list is not
    # confirmed to be exactly [new_dashboard_id] (fix round 12, finding 7).
    fake = install(FakeSuperset(dashboards_field="missing"))

    result = _run()

    assert result.exit_code == ExitCode.FAILURE, result.output
    assert not [p for m, p, _ in fake.writes if m == "PUT" and "/chart/" in p]


def test_a_clone_with_an_empty_dashboards_list_is_refused_with_no_chart_put(
    superset_env, install
) -> None:
    fake = install(FakeSuperset(dashboards_field="empty"))

    result = _run()

    assert result.exit_code == ExitCode.FAILURE, result.output
    assert not [p for m, p, _ in fake.writes if m == "PUT" and "/chart/" in p]


def test_a_created_dataset_is_given_its_metrics(superset_env, install) -> None:
    fake = install(FakeSuperset(target_has_orders=False))

    result = _run()

    assert result.exit_code == ExitCode.SUCCESS, result.output
    put = next(
        b for m, p, b in fake.writes if m == "PUT" and p.endswith("/dataset/904")
    )
    # Every synced column survives: the PUT replaces the list wholesale.
    assert {c["column_name"] for c in put["columns"]} >= {"x"}
    assert "metrics" in put


def test_a_construct_scan_finding_is_reported_even_when_the_live_check_passes(
    superset_env, install
) -> None:
    # The live check only proves the SQL RUNS on the target -- a bare
    # ::numeric cast is a perfectly ordinary SELECT as far as Redshift is
    # concerned, so it passes the live check every time. The scan is the
    # only thing that would ever catch the truncation this construct stands
    # in for, so it has to run over every dataset that would be created, not
    # only the ones the live check rejected (fix round 12, finding 1).
    fake = install(
        FakeSuperset(
            target_has_orders=False, dataset_118_sql="select x::numeric from orders"
        )
    )

    result = _run("--dry-run")

    assert result.exit_code == ExitCode.SUCCESS, result.output
    assert fake.writes == []
    assert "bare ::numeric cast" in result.output


def test_a_malformed_map_is_invalid_input(superset_env, install) -> None:
    install(FakeSuperset())

    result = _run("--map", "orders=analytics.orders")

    assert result.exit_code == ExitCode.INVALID_INPUT, result.output


def test_a_well_formed_map_for_an_unread_dataset_is_invalid_input(
    superset_env, install
) -> None:
    # A syntactically fine --map that names a dataset the dashboard does not
    # read must not be a silent no-op: that looks identical to one that
    # worked. plan_migration is what raises; this confirms the CLI actually
    # surfaces it, at the documented exit code, before anything is written.
    fake = install(FakeSuperset())

    result = _run("--map", "public.nope=public.users")

    assert result.exit_code == ExitCode.INVALID_INPUT, result.output
    assert fake.writes == []
    assert "public.nope" in result.output


def test_a_virtual_dataset_whose_sql_fails_on_the_target_writes_nothing(
    superset_env, install
) -> None:
    # The whole point of phase 1: a dataset that cannot be ported is found
    # BEFORE anything is created, and the engine's own words are reported.
    fake = install(FakeSuperset(target_has_orders=False))
    fake.virtual_sql_error = 'relation "borg.events" does not exist'

    result = _run()

    assert result.exit_code == ExitCode.FAILURE, result.output
    assert fake.writes == []
    assert 'relation "borg.events" does not exist' in result.output


def test_a_dataset_on_another_database_is_reported_and_left_alone(
    superset_env, install
) -> None:
    # A dashboard can read from several connections. Repointing one nobody
    # was migrating would be a change nobody asked for.
    fake = install(FakeSuperset(with_foreign_dataset=True))

    result = _run()

    assert result.exit_code == ExitCode.SUCCESS, result.output
    assert "Left alone" in result.output
    # The foreign dataset's cloned chart (90) is never the subject of a PUT.
    assert not [p for m, p, _ in fake.writes if m == "PUT" and p.endswith("/chart/90")]


def test_verify_reports_a_chart_that_returns_rows(superset_env, install) -> None:
    fake = install(FakeSuperset())
    fake.chart_rows = [{"a": 1}, {"a": 2}]

    result = runner.invoke(
        superset_cli.app,
        [
            "dashboards",
            "duplicate",
            "42",
            "--from-database",
            "DataOcean",
            "--to-database",
            "BetterData",
            "--yes",
        ],
        env=WIDE,
    )

    assert result.exit_code == ExitCode.SUCCESS, result.output
    assert "2" in result.output


def test_a_chart_that_fails_to_run_exits_one_not_five(superset_env, install) -> None:
    # 5 is documented as the one retryable code. A chart that cannot run will
    # not start running because a wrapper tried again.
    fake = install(FakeSuperset())
    fake.chart_error = "metric 'revenue' does not exist"

    result = runner.invoke(
        superset_cli.app,
        [
            "dashboards",
            "duplicate",
            "42",
            "--from-database",
            "DataOcean",
            "--to-database",
            "BetterData",
            "--yes",
        ],
        env=WIDE,
    )

    assert result.exit_code == ExitCode.FAILURE, result.output
    assert "revenue" in result.output


def test_compare_reports_agreement_per_chart(superset_env, install) -> None:
    fake = install(FakeSuperset())
    fake.chart_rows = [{"a": 1}]

    result = _run_compare()

    assert result.exit_code == ExitCode.SUCCESS, result.output
    assert "ok" in result.output.lower()


def test_compare_flags_a_row_count_difference(superset_env, install) -> None:
    fake = install(FakeSuperset())
    fake.chart_rows = [{"a": 1}]
    fake.original_rows = [{"a": 1}, {"a": 2}]

    result = _run_compare()

    assert result.exit_code == ExitCode.FAILURE, result.output


def test_compare_reports_which_cells_differ_at_identical_row_counts(
    superset_env, install
) -> None:
    # The motivating case: same row count, different values. A row-count
    # check cannot see this, which is why the cell count has to reach the
    # user rather than just the exit code.
    fake = install(FakeSuperset())
    fake.original_rows = [{"a": 1, "b": 2}]
    fake.chart_rows = [{"a": 1, "b": 3}]

    result = _run_compare()

    assert result.exit_code == ExitCode.FAILURE, result.output
    assert "1" in result.output  # the differing-cell count is visible
    # "1" alone also matches dashboard id 318 printed elsewhere in the
    # output, so it does not by itself prove the cell count is what is
    # showing -- this does, and would fail if the status text reverted to
    # the old unconditional "ok".
    assert "differs" in result.output.lower()


def test_compare_reports_when_the_original_cannot_run(superset_env, install) -> None:
    # The original's query can fail even when the new one succeeds -- e.g.
    # the source warehouse is already being decommissioned. That must read
    # differently from "they disagree": one is missing evidence, the other
    # is evidence, and only the latter should look like a content mismatch.
    fake = install(FakeSuperset())
    fake.original_chart_error = "relation orders does not exist"

    result = _run_compare()

    assert result.exit_code == ExitCode.FAILURE, result.output
    assert "relation orders does not exist" in result.output
    assert "differs" not in result.output.lower()


def test_compare_reports_the_original_has_no_query_context(
    superset_env, install
) -> None:
    # Every other fixture chart carries a query_context; this is the one
    # case with none. It must read as missing evidence, not a disagreement --
    # chart 8/80 still agrees normally, so the run succeeds overall.
    fake = install(FakeSuperset())
    fake.original_no_query_context = True

    result = _run_compare()

    assert result.exit_code == ExitCode.SUCCESS, result.output
    assert "not compared: original has no query context" in result.output.lower()


def test_compare_reports_an_unreadable_original_query_context_without_crashing(
    superset_env, install
) -> None:
    # A malformed (or non-str) query_context on the ORIGINAL used to raise
    # ValueError/TypeError past every except clause here -- at a point where
    # the copy and its datasets already exist, so it produced a raw
    # traceback instead of the "Left in place" report. Being unable to check
    # is not the same as checking and finding a divergence, so this must not
    # flip the run to a failure by itself (fix round 12, finding 3).
    fake = install(FakeSuperset())
    fake.original_bad_query_context = True

    result = _run_compare()

    assert result.exit_code == ExitCode.SUCCESS, result.output
    assert "not compared: unreadable original query context" in result.output.lower()


def test_compare_marks_an_ambiguous_pairing_not_compared_and_still_succeeds(
    superset_env, install
) -> None:
    # Two originals share (slice_name, datasource_id) -- nothing links clone
    # 70 back to a SPECIFIC one of them, so comparing against either would be
    # a coin flip. Refusing to compare is missing evidence, not evidence of
    # disagreement, and must not fail a run where everything else agrees.
    fake = install(FakeSuperset(duplicate_original_chart=True))
    fake.chart_rows = [{"a": 1}]
    fake.original_rows = [{"a": 1}]

    result = _run_compare()

    assert result.exit_code == ExitCode.SUCCESS, result.output
    assert "ambiguous" in result.output.lower()
    assert "could not be paired" in result.output.lower()


def test_compare_without_verify_says_it_is_ignored(superset_env, install) -> None:
    # --compare's only consumer is gated on --verify; running the pairing
    # calls anyway would spend API calls to produce nothing. The combination
    # must say so rather than silently doing nothing.
    install(FakeSuperset())

    result = _run("--compare")  # _run already passes --no-verify

    assert result.exit_code == ExitCode.SUCCESS, result.output
    assert "compare" in result.output.lower()
    assert "verify" in result.output.lower()


def test_json_output_carries_verification_rows(superset_env, install) -> None:
    # --json is the only way a non-interactive caller sees --compare's
    # evidence; without this key, that caller has no output at all to act on.
    fake = install(FakeSuperset())
    fake.chart_rows = [{"a": 1}, {"a": 2}]

    result = runner.invoke(
        superset_cli.app,
        [
            "dashboards",
            "duplicate",
            "42",
            "--from-database",
            "DataOcean",
            "--to-database",
            "BetterData",
            "--yes",
            "--json",
        ],
        env=WIDE,
    )

    assert result.exit_code == ExitCode.SUCCESS, result.output
    # duplicate's --json only replaces the final "Done" line; the phase-1
    # plan table and the verification table print unconditionally before it,
    # same as every other output in this command -- so only the tail is JSON.
    payload = json.loads(result.output[result.output.index('{\n  "dashboard_id"') :])
    assert {row["rows"] for row in payload["verification"]} == {2}


def test_compare_json_prints_the_evidence_even_on_a_disagreement(
    superset_env, install
) -> None:
    # The disagreement exit used to raise BEFORE the as_json block ever ran,
    # so a non-interactive caller got no output at all in exactly the case
    # --compare's evidence exists for (fix round 12, finding 5).
    fake = install(FakeSuperset())
    fake.original_rows = [{"a": 1}, {"a": 2}]
    fake.chart_rows = [{"a": 1}]

    result = runner.invoke(
        superset_cli.app,
        [
            "dashboards",
            "duplicate",
            "42",
            "--from-database",
            "DataOcean",
            "--to-database",
            "BetterData",
            "--yes",
            "--compare",
            "--json",
        ],
        env=WIDE,
    )

    assert result.exit_code == ExitCode.FAILURE, result.output
    payload = json.loads(result.output[result.output.index('{\n  "dashboard_id"') :])
    assert payload["verification"]
