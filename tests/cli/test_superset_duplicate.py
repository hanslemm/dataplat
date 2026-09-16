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
    ):
        self.writes: list[tuple[str, str, dict]] = []
        self.target_has_orders = target_has_orders
        self.clone_shared = clone_shared
        self.virtual_sql_error = virtual_sql_error
        self.with_foreign_dataset = with_foreign_dataset

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
            charts = CHARTS + ([FOREIGN_CHART] if self.with_foreign_dataset else [])
            return httpx.Response(200, json={"result": charts}, request=request)
        if path.endswith("/dashboard/318/charts"):
            clones = CLONES + ([FOREIGN_CLONE] if self.with_foreign_dataset else [])
            return httpx.Response(200, json={"result": clones}, request=request)
        if path.endswith("/dashboard/42"):
            return httpx.Response(200, json={"result": DASHBOARD}, request=request)
        if path.endswith("/dashboard/318"):
            return httpx.Response(200, json={"result": {"id": 318}}, request=request)
        if path.endswith("/copy/"):
            return httpx.Response(201, json={"result": {"id": 318}}, request=request)
        if path.endswith("/dataset/118"):
            return httpx.Response(200, json={"result": DATASET_118}, request=request)
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
        if "/chart/" in path:
            chart_id = int(path.rsplit("/", 1)[-1])
            dashboards = [{"id": 318}]
            if self.clone_shared:
                dashboards.append({"id": 99})
            return httpx.Response(
                200,
                json={"result": {**CLONE_BY_ID[chart_id], "dashboards": dashboards}},
                request=request,
            )
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

CHARTS = [
    {
        "id": 7,
        "slice_name": "Revenue",
        "datasource_id": 118,
        "datasource_type": "table",
    },
    {
        "id": 8,
        "slice_name": "Signups",
        "datasource_id": 121,
        "datasource_type": "table",
    },
]

CLONES = [
    {
        "id": 70,
        "slice_name": "Revenue",
        "datasource_id": 118,
        "datasource_type": "table",
    },
    {
        "id": 80,
        "slice_name": "Signups",
        "datasource_id": 121,
        "datasource_type": "table",
    },
]
CLONE_BY_ID = {
    70: {
        "id": 70,
        "datasource_id": 118,
        "datasource_type": "table",
        "params": json.dumps({"datasource": "118__table"}),
        "query_context": json.dumps({"datasource": {"id": 118, "type": "table"}}),
    },
    80: {
        "id": 80,
        "datasource_id": 121,
        "datasource_type": "table",
        "params": json.dumps({"datasource": "121__table"}),
        "query_context": json.dumps({"datasource": {"id": 121, "type": "table"}}),
    },
    90: {
        "id": 90,
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
    "datasource_id": 140,
    "datasource_type": "table",
}
FOREIGN_CLONE = {
    "id": 90,
    "slice_name": "External",
    "datasource_id": 140,
    "datasource_type": "table",
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
