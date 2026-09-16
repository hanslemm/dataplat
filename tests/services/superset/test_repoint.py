"""Deciding what moves where, with no Superset in sight.

Everything interesting about this command is decidable from two lists of
datasets and a chart payload, so all of it is tested here rather than through
HTTP.
"""

from __future__ import annotations

import json

import pytest

from dataplat.core.errors import ServiceError, ValidationError
from dataplat.services.superset.repoint import (
    DatasetKey,
    canonical_rows,
    chart_datasource_id,
    compare_rows,
    copy_metadata,
    create_payload,
    dashboard_metadata,
    parse_overrides,
    plan_migration,
    repoint_chart,
    repoint_metadata,
    semantic_payload,
)

SOURCE = [
    {
        "id": 118,
        "table_name": "orders",
        "schema": "public",
        "sql": None,
        "database": {"id": 1},
    },
    {
        "id": 121,
        "table_name": "users",
        "schema": "public",
        "sql": None,
        "database": {"id": 1},
    },
    {
        "id": 130,
        "table_name": "cohort",
        "schema": "analytics",
        "sql": "select 1",
        "database": {"id": 1},
    },
]

TARGET = [
    {"id": 887, "table_name": "users", "schema": "public", "database": {"id": 2}},
    {"id": 904, "table_name": "orders", "schema": "analytics", "database": {"id": 2}},
]


def _plan(overrides=None):
    return plan_migration(
        source_datasets=SOURCE,
        target_datasets=TARGET,
        from_database_id=1,
        overrides=overrides or {},
    )


def test_schema_and_table_match_case_insensitively() -> None:
    plan = _plan()
    matched = {m.source_id: m.target_id for m in plan.matched}
    assert matched[121] == 887


def test_a_table_whose_schema_differs_is_not_matched() -> None:
    # analytics.orders is NOT public.orders. Matching on table name alone
    # would silently point a chart at a different table.
    plan = _plan()
    assert 118 in {m.source_id for m in plan.to_create}


def test_an_override_wins_over_automatic_matching() -> None:
    plan = _plan({DatasetKey("public", "orders"): DatasetKey("analytics", "orders")})
    matched = {m.source_id: m.target_id for m in plan.matched}
    assert matched[118] == 904
    assert all(m.source_id != 118 for m in plan.to_create)


def test_an_override_naming_an_absent_target_is_an_error() -> None:
    with pytest.raises(ValidationError) as excinfo:
        _plan({DatasetKey("public", "orders"): DatasetKey("nope", "gone")})
    assert "nope.gone" in str(excinfo.value)


def test_an_override_for_a_dataset_the_dashboard_does_not_read_is_an_error() -> None:
    # A silently ignored --map is worse than no --map: the user gets a clean
    # exit and a migration that quietly ignored their instruction.
    with pytest.raises(ValidationError) as excinfo:
        _plan({DatasetKey("public", "nope"): DatasetKey("public", "users")})
    assert "public.nope" in str(excinfo.value)


def test_a_dataset_on_another_database_is_reported_and_never_repointed() -> None:
    foreign = {"id": 500, "table_name": "x", "schema": "s", "database": {"id": 9}}
    plan = plan_migration(
        source_datasets=[*SOURCE, foreign],
        target_datasets=TARGET,
        from_database_id=1,
        overrides={},
    )
    assert [m.source_id for m in plan.foreign] == [500]
    assert 500 not in plan.id_map


def test_without_a_source_database_every_dataset_is_movable() -> None:
    # What a coverage report needs: "does the other side have this?", asked
    # without first deciding which connection counts as the source.
    foreign = {
        "id": 500,
        "table_name": "users",
        "schema": "public",
        "database": {"id": 9},
    }
    plan = plan_migration(
        source_datasets=[foreign],
        target_datasets=TARGET,
        from_database_id=None,
        overrides={},
    )
    assert plan.foreign == ()
    assert plan.id_map == {500: 887}


def test_a_virtual_dataset_is_marked_as_one() -> None:
    plan = _plan()
    cohort = next(m for m in plan.to_create if m.source_id == 130)
    assert cohort.is_virtual


def test_a_source_dataset_with_no_usable_id_is_a_service_error() -> None:
    # A bare KeyError/TypeError here would escape the CLI's exit-code
    # machinery as a raw traceback rather than the documented exit code.
    broken = {"table_name": "x", "schema": "s", "sql": None, "database": {"id": 1}}
    with pytest.raises(ServiceError):
        plan_migration(
            source_datasets=[broken],
            target_datasets=TARGET,
            from_database_id=1,
            overrides={},
        )


def test_a_matched_target_dataset_with_no_usable_id_is_a_service_error() -> None:
    # public.orders in SOURCE matches this target by schema+table, so the
    # matched branch -- not just the source-side one -- must guard the id too.
    broken_target = [
        {"table_name": "orders", "schema": "public", "database": {"id": 2}}
    ]
    with pytest.raises(ServiceError):
        plan_migration(
            source_datasets=SOURCE,
            target_datasets=broken_target,
            from_database_id=1,
            overrides={},
        )


def test_parse_overrides_reads_schema_dot_table_pairs() -> None:
    parsed = parse_overrides(["public.orders=analytics.orders"])
    assert parsed == {DatasetKey("public", "orders"): DatasetKey("analytics", "orders")}


@pytest.mark.parametrize("bad", ["orders=analytics.orders", "a.b", "a.b=c", "=x.y"])
def test_a_malformed_override_is_invalid_input(bad: str) -> None:
    with pytest.raises(ValidationError):
        parse_overrides([bad])


CHART = {
    "id": 7,
    "datasource_id": 118,
    "datasource_type": "table",
    "params": json.dumps({"datasource": "118__table", "metrics": ["revenue"]}),
    "query_context": json.dumps(
        {
            "datasource": {"id": 118, "type": "table"},
            "queries": [{"metrics": ["revenue"]}],
        }
    ),
}


def test_repointing_rewrites_all_three_places_a_chart_stores_its_dataset() -> None:
    payload = repoint_chart(CHART, {118: 904})
    assert payload is not None
    assert payload["datasource_id"] == 904
    assert json.loads(payload["params"])["datasource"] == "904__table"
    assert json.loads(payload["query_context"])["datasource"]["id"] == 904


def test_repointing_preserves_everything_else_in_params() -> None:
    payload = repoint_chart(CHART, {118: 904})
    assert json.loads(payload["params"])["metrics"] == ["revenue"]


def test_a_chart_whose_dataset_is_not_in_the_map_is_left_alone() -> None:
    # Returning None rather than an unchanged payload: the caller must not
    # issue a PUT at all for a chart nobody is migrating.
    assert repoint_chart(CHART, {999: 1000}) is None


def test_a_chart_with_no_query_context_still_repoints() -> None:
    chart = {**CHART, "query_context": None}
    payload = repoint_chart(chart, {118: 904})
    assert payload["datasource_id"] == 904
    assert "query_context" not in payload


def test_a_chart_with_unparseable_params_is_a_service_error() -> None:
    # Omitting "params" and still rewriting datasource_id would leave the
    # chart pointing at two different datasets at once -- silently.
    chart = {**CHART, "params": "{not json"}
    with pytest.raises(ServiceError):
        repoint_chart(chart, {118: 904})


def test_a_chart_with_unparseable_query_context_is_a_service_error() -> None:
    chart = {**CHART, "query_context": "{not json"}
    with pytest.raises(ServiceError):
        repoint_chart(chart, {118: 904})


def test_chart_datasource_id_reads_the_top_level_field() -> None:
    # GET /chart/{id}'s own shape -- what CLONE_BY_ID-style fixtures and the
    # repoint loop actually see.
    assert chart_datasource_id({"datasource_id": 118}) == 118


def test_chart_datasource_id_reads_form_data_datasource() -> None:
    # GET /dashboard/{id}/charts's real shape: no top-level datasource_id at
    # all, only form_data.datasource in its "<id>__table" form.
    assert chart_datasource_id({"form_data": {"datasource": "983__table"}}) == 983


def test_chart_datasource_id_reads_a_nested_int_datasource_id() -> None:
    assert chart_datasource_id({"form_data": {"datasource_id": 118}}) == 118


def test_chart_datasource_id_prefers_the_top_level_field_when_both_agree() -> None:
    chart = {
        "datasource_id": 118,
        "form_data": {"datasource": "118__table"},
    }
    assert chart_datasource_id(chart) == 118


def test_chart_datasource_id_is_none_when_neither_is_present() -> None:
    # A legitimate shape -- a markdown header tile reads no dataset -- not a
    # service error, so callers can skip it rather than this raising.
    assert chart_datasource_id({"id": 7, "slice_name": "Header"}) is None


def test_chart_datasource_id_is_none_for_a_malformed_datasource_string() -> None:
    assert chart_datasource_id({"form_data": {"datasource": "not-an-id"}}) is None


def test_native_filters_are_remapped() -> None:
    metadata = json.dumps(
        {
            "native_filter_configuration": [
                {"id": "F1", "targets": [{"datasetId": 118, "column": {"name": "x"}}]},
                {"id": "F2", "targets": [{"datasetId": 121, "column": {"name": "y"}}]},
            ],
            "color_scheme": "supersetColors",
        }
    )
    out = json.loads(repoint_metadata(metadata, {118: 904, 121: 887}))
    ids = [
        t["datasetId"] for f in out["native_filter_configuration"] for t in f["targets"]
    ]
    assert ids == [904, 887]
    # Everything else survives untouched.
    assert out["color_scheme"] == "supersetColors"
    assert out["native_filter_configuration"][0]["targets"][0]["column"] == {
        "name": "x"
    }


def test_copy_metadata_merges_positions_in() -> None:
    # Superset reads `positions` out of the metadata it is SENT to remap
    # chartId onto the clones. GET returns it as a sibling field, so metadata
    # passed through as received produces a copy wired to the original charts.
    dashboard = {
        "json_metadata": json.dumps({"color_scheme": "supersetColors"}),
        "position_json": json.dumps({"CHART-abc": {"meta": {"chartId": 7}}}),
    }
    merged = json.loads(copy_metadata(dashboard))
    assert merged["positions"] == {"CHART-abc": {"meta": {"chartId": 7}}}
    assert merged["color_scheme"] == "supersetColors"


def test_copy_metadata_survives_a_dashboard_with_neither_field() -> None:
    assert json.loads(copy_metadata({})) == {}


def test_copy_metadata_survives_malformed_json() -> None:
    dashboard = {"json_metadata": "{not json", "position_json": "{not json"}
    assert json.loads(copy_metadata(dashboard)) == {}


def test_dashboard_metadata_remaps_but_drops_the_original_layout() -> None:
    # The copy has its OWN layout, which Superset built by remapping chartId
    # onto the clones. Writing the original's positions back would undo that.
    metadata = json.dumps(
        {
            "positions": {"CHART-a": {"meta": {"chartId": 7}}},
            "native_filter_configuration": [
                {"id": "F1", "targets": [{"datasetId": 118}]}
            ],
        }
    )
    out = json.loads(dashboard_metadata(metadata, {118: 904}))
    assert "positions" not in out
    assert out["native_filter_configuration"][0]["targets"][0]["datasetId"] == 904


def test_dashboard_metadata_survives_malformed_json() -> None:
    # repoint_metadata already absorbed this and handed the raw string back;
    # re-parsing it here must not raise the very error it just swallowed.
    assert dashboard_metadata("{not json", {118: 904}) == "{not json"


SOURCE_DATASET = {
    "id": 130,
    "table_name": "cohort",
    "schema": "analytics",
    "sql": "select 1",
    "main_dttm_col": "created_at",
    "description": "cohorts",
    "columns": [
        {
            "id": 1,
            "column_name": "created_at",
            "verbose_name": "Created",
            "is_dttm": True,
            "expression": None,
        },
        {
            "id": 2,
            "column_name": "revenue_eur",
            "verbose_name": "Revenue (EUR)",
            "is_dttm": False,
            "expression": None,
        },
        {
            "id": 3,
            "column_name": "is_new",
            "verbose_name": "New?",
            "is_dttm": False,
            "expression": "revenue_eur > 0",
        },
    ],
    "metrics": [
        {
            "id": 9,
            "metric_name": "revenue",
            "expression": "sum(revenue_eur)",
            "verbose_name": "Revenue",
        }
    ],
}

SYNCED = {
    "id": 904,
    "columns": [
        {"id": 50, "column_name": "created_at"},
        {"id": 51, "column_name": "revenue_eur"},
    ],
}


def test_the_create_payload_carries_sql_for_a_virtual_dataset() -> None:
    payload = create_payload(SOURCE_DATASET, 2, DatasetKey("analytics", "cohort"))
    assert payload["database"] == 2
    assert payload["schema"] == "analytics"
    assert payload["table_name"] == "cohort"
    assert payload["sql"] == "select 1"


def test_the_create_payload_omits_sql_for_a_physical_dataset() -> None:
    physical = {**SOURCE_DATASET, "sql": None}
    assert "sql" not in create_payload(physical, 2, DatasetKey("public", "orders"))


def test_the_semantic_payload_keeps_every_synced_column() -> None:
    # The PUT replaces the column list wholesale. Sending only the calculated
    # columns would DELETE every physical column Superset had just synced.
    payload = semantic_payload(SOURCE_DATASET, SYNCED)
    names = {c["column_name"] for c in payload["columns"]}
    assert {"created_at", "revenue_eur"} <= names


def test_synced_columns_keep_their_ids_so_they_are_updated_not_recreated() -> None:
    payload = semantic_payload(SOURCE_DATASET, SYNCED)
    by_name = {c["column_name"]: c for c in payload["columns"]}
    assert by_name["created_at"]["id"] == 50


def test_metadata_travels_onto_the_synced_columns() -> None:
    payload = semantic_payload(SOURCE_DATASET, SYNCED)
    by_name = {c["column_name"]: c for c in payload["columns"]}
    assert by_name["revenue_eur"]["verbose_name"] == "Revenue (EUR)"
    assert by_name["created_at"]["is_dttm"] is True


def test_calculated_columns_are_created_without_an_id() -> None:
    payload = semantic_payload(SOURCE_DATASET, SYNCED)
    by_name = {c["column_name"]: c for c in payload["columns"]}
    assert by_name["is_new"]["expression"] == "revenue_eur > 0"
    assert "id" not in by_name["is_new"]


def test_metrics_travel_without_their_source_ids() -> None:
    # A chart names its metrics as strings -- params.metrics: ["revenue"].
    # Without this the datasource id is right and the chart renders broken.
    payload = semantic_payload(SOURCE_DATASET, SYNCED)
    assert payload["metrics"][0]["metric_name"] == "revenue"
    assert "id" not in payload["metrics"][0]


def test_rows_in_a_different_order_agree() -> None:
    # Sorted in Python, never by adding an ORDER BY: the engines disagree on
    # text collation, and Redshift ignores trailing blanks in comparison where
    # Postgres does not, so ordering on the engines would invent differences.
    left = [{"a": 1, "b": "x"}, {"a": 2, "b": "y"}]
    right = [{"a": 2, "b": "y"}, {"a": 1, "b": "x"}]
    assert compare_rows(left, right).agrees


def test_a_row_count_difference_is_reported() -> None:
    result = compare_rows([{"a": 1}], [{"a": 1}, {"a": 2}])
    assert not result.agrees
    assert result.left_rows == 1 and result.right_rows == 2


def test_differing_cells_are_counted() -> None:
    # avg(int) truncating on Redshift looks exactly like this.
    result = compare_rows([{"a": 1, "b": 2}], [{"a": 1, "b": 3}])
    assert result.differing_cells == 1


def test_a_trailing_blank_difference_is_reported_not_normalised_away() -> None:
    # Redshift ignores trailing blanks when comparing; we must not, or we hide
    # a real divergence in the stored values.
    assert not compare_rows([{"a": "x"}], [{"a": "x "}]).agrees


def test_a_column_missing_on_one_side_is_named() -> None:
    result = compare_rows([{"a": 1, "b": 2}], [{"a": 1}])
    assert result.missing_columns == ("b",)


def test_canonical_rows_is_stable_for_mixed_types() -> None:
    # None and int in one column must not raise on sort.
    assert len(canonical_rows([{"a": None}, {"a": 1}])) == 2
