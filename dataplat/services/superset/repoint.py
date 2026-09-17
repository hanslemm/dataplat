"""What moves where, and what each payload has to say -- decided without HTTP.

The interesting half of duplicating a dashboard is arithmetic on payloads:
which dataset corresponds to which, the three separate places a chart records
the dataset it reads, the native filters that record it again, and whether two
result sets agree. None of that needs a Superset to decide, so none of it is
tested against one. ``services/impact.py`` is pure for the same reason.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass

from dataplat.core.errors import ServiceError, ValidationError

__all__ = [
    "Comparison",
    "DatasetKey",
    "DatasetMatch",
    "MigrationPlan",
    "canonical_rows",
    "chart_datasource_id",
    "compare_rows",
    "copy_metadata",
    "create_payload",
    "dashboard_metadata",
    "parse_overrides",
    "plan_migration",
    "repoint_chart",
    "repoint_metadata",
    "semantic_payload",
    "verification_context",
]


@dataclass(frozen=True)
class DatasetKey:
    """Where a dataset lives, as the pair that identifies it on a database."""

    schema: str
    table: str

    def __str__(self) -> str:
        return f"{self.schema}.{self.table}"

    @classmethod
    def parse(cls, text: str) -> DatasetKey:
        """Read ``schema.table``, rejecting anything else."""
        schema, _, table = text.partition(".")
        if not schema or not table or "." in table:
            raise ValidationError(f"Expected schema.table, got: {text or '(empty)'}")
        return cls(schema.lower(), table.lower())

    @classmethod
    def of(cls, dataset: dict) -> DatasetKey:
        """The key of a Superset dataset payload."""
        return cls(
            str(dataset.get("schema") or "").lower(),
            str(dataset.get("table_name") or "").lower(),
        )


@dataclass(frozen=True)
class DatasetMatch:
    """One source dataset and what it becomes on the target database."""

    source_id: int
    source_key: DatasetKey
    target_key: DatasetKey
    via: str  # "map", "schema+table", "create", or "foreign"
    is_virtual: bool
    name: str
    target_id: int | None = None


@dataclass(frozen=True)
class MigrationPlan:
    """Everything decided before anything is written."""

    matched: tuple[DatasetMatch, ...]
    to_create: tuple[DatasetMatch, ...]
    foreign: tuple[DatasetMatch, ...]

    @property
    def id_map(self) -> dict[int, int]:
        """Old dataset id to new, for the matches that already have a target."""
        return {
            m.source_id: m.target_id for m in self.matched if m.target_id is not None
        }


def _require_dataset_id(dataset: dict) -> int:
    """The int id of a Superset dataset payload, or a loud, typed failure.

    A payload with no usable id is not the user's mistake to fix -- it means
    Superset returned something unusable, which is what :class:`ServiceError`
    is for. Falling back to a bare ``dataset["id"]`` would raise ``KeyError``
    instead, which is not a :class:`DataplatError` and so escapes ``fail()``
    as a raw traceback rather than the documented exit code.
    """
    dataset_id = dataset.get("id")
    if not isinstance(dataset_id, int):
        raise ServiceError(
            f"Superset returned a dataset with no usable id: {DatasetKey.of(dataset)}"
        )
    return dataset_id


def parse_overrides(pairs: Iterable[str]) -> dict[DatasetKey, DatasetKey]:
    """Read repeated ``schema.table=schema.table`` flags."""
    overrides: dict[DatasetKey, DatasetKey] = {}
    for pair in pairs:
        source, sep, target = pair.partition("=")
        if not sep:
            raise ValidationError(f"Expected schema.table=schema.table, got: {pair}")
        overrides[DatasetKey.parse(source)] = DatasetKey.parse(target)
    return overrides


def plan_migration(
    *,
    source_datasets: list[dict],
    target_datasets: list[dict],
    from_database_id: int | None,
    overrides: dict[DatasetKey, DatasetKey],
) -> MigrationPlan:
    """Decide, for every dataset the dashboard reads, what happens to it.

    A dataset on another database is reported and never repointed. A dashboard
    can read from several connections, and a chart already pointing at the
    target -- or at a third warehouse -- is not this command's business;
    repointing it would be a change nobody asked for.

    ``from_database_id=None`` drops that classification entirely and treats
    every source dataset as movable. That is what a coverage report wants: "is
    there a counterpart for this?" is a useful question before anyone has
    decided which connection counts as the source. Only the reporting command
    passes None; ``duplicate`` always names a source.

    Every key in ``overrides`` must name a dataset the dashboard actually
    reads, on the source database. An entry that does not is an error, raised
    only after the whole pass so it can be reported alongside every other
    problem: a silently ignored ``--map`` is worse than no ``--map`` at all,
    because it looks identical to one that worked.
    """
    by_key = {DatasetKey.of(d): d for d in target_datasets}

    matched: list[DatasetMatch] = []
    to_create: list[DatasetMatch] = []
    foreign: list[DatasetMatch] = []
    used: set[DatasetKey] = set()

    for dataset in source_datasets:
        source_id = _require_dataset_id(dataset)
        source_key = DatasetKey.of(dataset)
        is_virtual = bool(dataset.get("sql"))
        name = str(dataset.get("table_name") or source_id)
        database_id = (dataset.get("database") or {}).get("id")

        if from_database_id is not None and database_id != from_database_id:
            foreign.append(
                DatasetMatch(
                    source_id, source_key, source_key, "foreign", is_virtual, name
                )
            )
            continue

        override = overrides.get(source_key)
        if override is not None:
            used.add(source_key)
            target = by_key.get(override)
            if target is None:
                raise ValidationError(
                    f"--map points {source_key} at {override}, which does not "
                    f"exist on the target database"
                )
            matched.append(
                DatasetMatch(
                    source_id,
                    source_key,
                    override,
                    "map",
                    is_virtual,
                    name,
                    _require_dataset_id(target),
                )
            )
            continue

        target = by_key.get(source_key)
        if target is not None:
            matched.append(
                DatasetMatch(
                    source_id,
                    source_key,
                    source_key,
                    "schema+table",
                    is_virtual,
                    name,
                    _require_dataset_id(target),
                )
            )
            continue

        to_create.append(
            DatasetMatch(source_id, source_key, source_key, "create", is_virtual, name)
        )

    unused = sorted(str(key) for key in overrides if key not in used)
    if unused:
        raise ValidationError(
            "--map names dataset(s) this dashboard does not read: " + ", ".join(unused)
        )

    return MigrationPlan(tuple(matched), tuple(to_create), tuple(foreign))


def copy_metadata(dashboard: dict) -> str:
    """The ``json_metadata`` the copy endpoint needs, with ``positions`` merged in.

    ``GET /dashboard/{id}`` returns the layout as ``position_json``, a sibling
    of ``json_metadata`` -- and the copy endpoint reads ``positions`` out of
    the metadata it is SENT in order to rewrite every ``chartId`` onto the
    cloned charts. Passing the metadata through as received therefore produces
    a copy whose layout still points at the original's charts, silently.
    """
    try:
        metadata = json.loads(dashboard.get("json_metadata") or "{}") or {}
    except ValueError:
        metadata = {}
    try:
        positions = json.loads(dashboard.get("position_json") or "{}") or {}
    except ValueError:
        positions = {}
    if positions:
        metadata["positions"] = positions
    return json.dumps(metadata)


def chart_datasource_id(chart: dict) -> int | None:
    """The dataset a chart reads, wherever this endpoint chose to put it.

    ``GET /chart/{id}`` returns a real ``datasource_id``; ``GET
    /dashboard/{id}/charts`` does not -- there the dataset survives only
    inside ``form_data.datasource``, in its ``"983__table"`` form. Reading
    the top-level field alone works against every fixture and against no
    live Superset, which is how this reached a release candidate.

    None when nothing here yields an id -- a legitimate shape (a markdown
    header tile reads no dataset at all), not a service error, so callers
    skip it rather than this raising.
    """
    top_level = chart.get("datasource_id")
    if isinstance(top_level, int):
        return top_level

    form_data = chart.get("form_data")
    if isinstance(form_data, dict):
        nested = form_data.get("datasource_id")
        if isinstance(nested, int):
            return nested

        datasource = form_data.get("datasource")
        if isinstance(datasource, str):
            raw_id, _, _ = datasource.partition("__")
            if raw_id.isdigit():
                return int(raw_id)

    return None


def repoint_chart(chart: dict, id_map: dict[int, int]) -> dict | None:
    """The PUT body that moves ``chart`` to its new dataset, or None.

    None when the chart's dataset is not being migrated: the caller must issue
    no PUT at all rather than a no-op one.

    All three sites move together. A chart whose ``params`` disagree with its
    ``datasource_id`` renders from whichever the viz plugin happened to read.
    """
    old_id = chart.get("datasource_id")
    if not isinstance(old_id, int) or old_id not in id_map:
        return None
    new_id = id_map[old_id]
    datasource_type = str(chart.get("datasource_type") or "table")

    payload: dict[str, object] = {
        "datasource_id": new_id,
        "datasource_type": datasource_type,
    }

    raw_params = chart.get("params")
    if raw_params:
        try:
            params = json.loads(raw_params)
        except ValueError as exc:
            # Omitting "params" here would still rewrite datasource_id below,
            # leaving the chart pointing at two different datasets at once --
            # exactly what the docstring says never happens.
            raise ServiceError(
                f"Chart {chart.get('id')} has unparseable params; refusing to "
                "rewrite its datasource, which would leave it pointing at two "
                "different datasets at once"
            ) from exc
        if isinstance(params, dict):
            if params.get("datasource"):
                params["datasource"] = f"{new_id}__{datasource_type}"
            payload["params"] = json.dumps(params)

    raw_context = chart.get("query_context")
    if raw_context:
        try:
            context = json.loads(raw_context)
        except ValueError as exc:
            raise ServiceError(
                f"Chart {chart.get('id')} has unparseable query_context; "
                "refusing to rewrite its datasource, which would leave it "
                "pointing at two different datasets at once"
            ) from exc
        if isinstance(context, dict):
            datasource = context.get("datasource")
            if isinstance(datasource, dict):
                datasource["id"] = new_id
                datasource["type"] = datasource_type
            payload["query_context"] = json.dumps(context)

    return payload


def verification_context(
    clone: dict, original: dict | None, id_map: dict[int, int]
) -> dict | None:
    """The query context to verify a cloned chart with.

    Superset's copy endpoint does not carry ``query_context`` onto a cloned
    slice -- measured on a live instance: 14 of 14 originals had one, 0 of 14
    fresh clones did, with no repoint involved. Without a fallback the whole
    verification step has nothing to run and reports "no query context" for
    every chart, which is honest and useless.

    So: the clone's own context when it has one, otherwise the original's with
    its datasource rewritten to the clone's. Returns None when neither yields
    one, which stays a reported status rather than an error.
    """
    raw_clone = clone.get("query_context")
    if raw_clone:
        try:
            context = json.loads(raw_clone)
        except ValueError:
            return None
        return context if isinstance(context, dict) else None

    if original is None:
        return None

    raw_original = original.get("query_context")
    if not raw_original:
        return None
    try:
        context = json.loads(raw_original)
    except ValueError:
        return None
    if not isinstance(context, dict):
        return None

    # Same lookup repoint_chart does -- the OLD id this context names, mapped
    # to the new one -- just sourced from the original chart rather than the
    # one being mutated. Any failure to resolve or rewrite it means returning
    # a query that would verify the copy against the OLD dataset, which is
    # worse than not verifying at all: it would report success for a chart
    # that was never actually tested.
    old_id = original.get("datasource_id")
    if not isinstance(old_id, int) or old_id not in id_map:
        return None
    new_id = id_map[old_id]
    datasource_type = str(
        clone.get("datasource_type") or original.get("datasource_type") or "table"
    )

    datasource = context.get("datasource")
    if not isinstance(datasource, dict):
        return None
    datasource["id"] = new_id
    datasource["type"] = datasource_type
    return context


def repoint_metadata(json_metadata: str, id_map: dict[int, int]) -> str:
    """Remap native filter dataset ids, leaving the rest of the metadata alone.

    A native filter still pointing at the old dataset keeps that warehouse
    connection alive and fills its dropdown from the wrong one.
    """
    try:
        metadata = json.loads(json_metadata or "{}") or {}
    except ValueError:
        return json_metadata or "{}"

    filters = metadata.get("native_filter_configuration")
    if isinstance(filters, list):
        for native_filter in filters:
            if not isinstance(native_filter, dict):
                continue
            for target in native_filter.get("targets") or []:
                if not isinstance(target, dict):
                    continue
                dataset_id = target.get("datasetId")
                if isinstance(dataset_id, int) and dataset_id in id_map:
                    target["datasetId"] = id_map[dataset_id]

    return json.dumps(metadata)


def dashboard_metadata(json_metadata: str, id_map: dict[int, int]) -> str:
    """Remapped metadata for the COPY, with the original's layout removed.

    The copy has its own layout: Superset built it during the copy by
    rewriting every ``chartId`` onto the cloned charts. Writing the original's
    ``positions`` back over it would undo exactly that, and the dashboard
    would render the original's charts inside the copy.
    """
    try:
        remapped = json.loads(repoint_metadata(json_metadata, id_map))
    except ValueError:
        # repoint_metadata hands back the raw string when it could not parse
        # it, so a second parse here would raise the very error it just
        # absorbed.
        return json_metadata or "{}"
    remapped.pop("positions", None)
    return json.dumps(remapped)


def create_payload(
    source: dict, target_database_id: int, target_key: DatasetKey
) -> dict:
    """The POST body that creates ``source``'s counterpart.

    POST accepts only these fields -- columns and metrics exist solely on the
    PUT schema, which is what ``semantic_payload`` is for.
    """
    payload: dict[str, object] = {
        "database": target_database_id,
        "schema": target_key.schema,
        "table_name": target_key.table,
    }
    if source.get("sql"):
        payload["sql"] = source["sql"]
    return payload


_COLUMN_FIELDS = (
    "verbose_name",
    "description",
    "is_dttm",
    "groupby",
    "filterable",
    "python_date_format",
    "type",
    "advanced_data_type",
    "extra",
)

_METRIC_FIELDS = (
    "metric_name",
    "expression",
    "verbose_name",
    "description",
    "metric_type",
    "d3format",
    "currency",
    "warning_text",
    "extra",
)


def semantic_payload(source: dict, synced: dict) -> dict:
    """The PUT body that gives a freshly created dataset its semantic layer.

    ``columns`` is a FULL REPLACEMENT keyed by id: entries with an id update,
    entries without are created, and anything omitted is DELETED. So the
    synced physical columns are carried through with their ids -- sending only
    the calculated ones would drop every column Superset had just discovered.

    Metrics are merged the same way, by ``metric_name`` rather than id --
    measured live: Superset pre-creates a ``count`` metric on every new
    dataset, so a source metric also named ``count``, sent with no id, reads
    as "create another one" and the whole PUT is rejected with 422
    (``metrics: One or more metrics already exist``). A name match carries
    the synced id forward so it updates instead of duplicating; a synced
    metric the source does not name is kept too, for the same reason synced
    columns are -- this list is a full replacement, and leaving one out
    deletes it. None of this would matter if a chart referenced its metrics
    by id, but it names them as strings (``params.metrics: ["revenue"]``):
    get the name wrong or the metric missing and the datasource id is
    perfectly correct while the chart renders broken.
    """
    synced_by_name = {
        str(c.get("column_name")): c for c in (synced.get("columns") or [])
    }
    source_by_name = {
        str(c.get("column_name")): c for c in (source.get("columns") or [])
    }

    columns: list[dict] = []
    for name, synced_column in synced_by_name.items():
        column: dict[str, object] = {"column_name": name}
        synced_id = synced_column.get("id")
        if isinstance(synced_id, int):
            column["id"] = synced_id
        origin = source_by_name.get(name)
        if origin:
            for field in _COLUMN_FIELDS:
                if origin.get(field) is not None:
                    column[field] = origin[field]
        columns.append(column)

    for name, origin in source_by_name.items():
        if name in synced_by_name or not origin.get("expression"):
            continue
        calculated: dict[str, object] = {
            "column_name": name,
            "expression": origin["expression"],
        }
        for field in _COLUMN_FIELDS:
            if origin.get(field) is not None:
                calculated[field] = origin[field]
        columns.append(calculated)

    source_metrics = source.get("metrics") or []
    synced_metrics_by_name = {
        str(m.get("metric_name")): m for m in (synced.get("metrics") or [])
    }
    source_metric_names = {
        str(m.get("metric_name")) for m in source_metrics if m.get("metric_name")
    }

    metrics: list[dict] = []
    for origin in source_metrics:
        if not origin.get("metric_name"):
            continue
        metric: dict[str, object] = {
            field: origin[field]
            for field in _METRIC_FIELDS
            if origin.get(field) is not None
        }
        synced_metric = synced_metrics_by_name.get(str(origin["metric_name"]))
        if synced_metric is not None:
            synced_id = synced_metric.get("id")
            if isinstance(synced_id, int):
                metric["id"] = synced_id
        metrics.append(metric)

    # Whatever Superset auto-created that the source never named -- "count",
    # almost always -- survives the full replacement above only if it is sent
    # back explicitly.
    for name, synced_metric in synced_metrics_by_name.items():
        if name in source_metric_names:
            continue
        retained: dict[str, object] = {"metric_name": name}
        synced_id = synced_metric.get("id")
        if isinstance(synced_id, int):
            retained["id"] = synced_id
        metrics.append(retained)

    payload: dict[str, object] = {"columns": columns, "metrics": metrics}
    for field in (
        "main_dttm_col",
        "description",
        "fetch_values_predicate",
        "extra",
        "template_params",
    ):
        if source.get(field) is not None:
            payload[field] = source[field]
    return payload


@dataclass(frozen=True)
class Comparison:
    """Whether two result sets say the same thing."""

    left_rows: int
    right_rows: int
    differing_cells: int
    missing_columns: tuple[str, ...]

    @property
    def agrees(self) -> bool:
        return (
            self.left_rows == self.right_rows
            and self.differing_cells == 0
            and not self.missing_columns
        )


def _sort_key(row: dict, columns: tuple[str, ...]) -> tuple:
    # Type name first: None, int and str in one column must not raise on sort,
    # and a mixed column is itself worth surfacing rather than crashing on.
    return tuple((type(row.get(c)).__name__, str(row.get(c))) for c in columns)


def canonical_rows(rows: list[dict]) -> list[tuple]:
    """Rows as comparable tuples, ordered deterministically in Python.

    Never by asking the engines to ORDER BY: Postgres and Redshift disagree on
    text collation, and Redshift ignores trailing blanks in comparison where
    Postgres does not, so an engine-side sort invents differences that belong
    to the comparison rather than to the data.
    """
    if not rows:
        return []
    columns = tuple(sorted({key for row in rows for key in row}))
    ordered = sorted(rows, key=lambda row: _sort_key(row, columns))
    return [tuple(row.get(column) for column in columns) for row in ordered]


def compare_rows(left: list[dict], right: list[dict]) -> Comparison:
    """Whether the two sides agree, and by how much they do not."""
    left_columns = {key for row in left for key in row}
    right_columns = {key for row in right for key in row}
    missing = tuple(sorted(left_columns ^ right_columns))

    differing = 0
    if not missing:
        # Canonical tuples are positional. With columns missing on one side,
        # zipping them would pair up unrelated cells and count a mismatch
        # that is really the shape difference `missing_columns` already
        # reports -- so only count cells when both sides have the same
        # columns to compare.
        canonical_left = canonical_rows(left)
        canonical_right = canonical_rows(right)
        for row_left, row_right in zip(canonical_left, canonical_right, strict=False):
            differing += sum(
                1 for a, b in zip(row_left, row_right, strict=False) if a != b
            )

    return Comparison(len(left), len(right), differing, missing)
