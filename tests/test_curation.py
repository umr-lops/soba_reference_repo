"""The recipe's curation boundary preserves raw catalogue data."""

from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from test_swot_test import source_row, catalogue_path
from soba_reference_repo.swot_test import apply_quality_filters, read_swot_catalogue

import pytest

from soba_reference_repo import curation


def recipe_text(source, **changes):
    fields = {
        "reference": '"swot"',
        "output_dir": '"output"',
        "production_date": '"20260930"',
        "version": '"0.1"',
    }
    fields.update(changes)
    return "\n".join(f"{key} = {value}" for key, value in fields.items()) + (
        f'\n[[catalogues]]\nmission = "S1A"\npath = "{source}"\n'
    )


def source_path(tmp_path, mission="S1A"):
    path = tmp_path / f"{mission}_coaligned_catalogue_WV_20260101_0.1.parquet"
    path.touch()
    return path


def test_read_recipe_resolves_relative_paths_and_defaults_compile(tmp_path):
    source = source_path(tmp_path)
    recipe = tmp_path / "recipe.toml"
    recipe.write_text(recipe_text(source.name))

    parsed = curation.read_recipe(recipe)

    assert parsed["output_dir"] == tmp_path / "output"
    assert parsed["catalogues"] == [{"mission": "S1A", "path": source, "rules": []}]
    assert parsed["compile"] is False


@pytest.mark.parametrize(
    ("change", "extra", "message"),
    [
        ({"reference": '"alti"'}, "", "reference"),
        ({"production_date": '"20260230"'}, "", "production_date"),
        ({"production_date": '"2026-09-30"'}, "", "production_date"),
        ({"version": '"v1"'}, "", "version"),
        ({"surprise": '"x"'}, "", "unknown"),
        ({}, '[[catalogues]]\nmission = "S1A"\npath = "other.parquet"\n', "duplicate"),
        ({}, '[[catalogues]]\nmission = "S1B"\npath = "absent.parquet"\n', "missing"),
        ({}, '[[catalogues.rules]]\nid = "sea_proxy"\nwrong = 4\n', "unknown"),
        ({}, '[[catalogues.rules]]\nid = "sea_proxy"\nclauses = [["x", "bad", 1]]\n', "operation"),
        ({}, '[[catalogues]]\nmission = "S1B"\npath = "other.parquet"\n', "missing"),
    ],
)
def test_read_recipe_rejects_invalid_configuration(tmp_path, change, extra, message):
    source = source_path(tmp_path)
    recipe = tmp_path / "recipe.toml"
    recipe.write_text(recipe_text(source.name, **change) + extra)
    with pytest.raises((ValueError, FileNotFoundError), match=message):
        curation.read_recipe(recipe)


def test_read_recipe_rejects_mission_filename_conflict(tmp_path):
    source = source_path(tmp_path, "S1C")
    recipe = tmp_path / "recipe.toml"
    recipe.write_text(recipe_text(source.name))
    with pytest.raises(ValueError, match="mission"):
        curation.read_recipe(recipe)


def test_read_recipe_rejects_existing_output_even_if_empty(tmp_path):
    source = source_path(tmp_path)
    (tmp_path / "output").mkdir()
    recipe = tmp_path / "recipe.toml"
    recipe.write_text(recipe_text(source.name))
    with pytest.raises(FileExistsError, match="output"):
        curation.read_recipe(recipe)


def test_resolve_s1c_override_does_not_change_s1a():
    base = curation.resolve_rules([])
    altered = curation.resolve_rules([
        {"id": "imerg_rain", "clauses": [["max_rainrate_IMERG", "le", 0.1]]},
        {"id": "classification_present", "enabled": False},
        {"id": "extra", "name": "extra quality", "clauses": [["extra", "present"]]},
    ])
    ids = [rule["id"] for rule in base]
    assert ids == ["sea_proxy", "no_dynamic_ice", "time_present", "karin_positive",
                   "ocn_positive", "altimeter_positive", "ww3_positive",
                   "classification_present", "time_under_two_hours", "full_overlap",
                   "imerg_rain", "no_swot_rain_flag", "accepted_classes"]
    assert [rule["id"] for rule in altered] == ids + ["extra"]
    assert base[10]["clauses"] == [["max_rainrate_IMERG", "le", 0]]
    assert altered[10]["clauses"] == [["max_rainrate_IMERG", "le", 0.1]]
    assert base[7]["enabled"] is True
    assert altered[7]["enabled"] is False


@pytest.mark.parametrize("overrides", [
    [{"id": "extra", "clauses": [["x", "present"]]}],
    [{"id": "extra", "name": "extra"}],
    [{"id": "sea_proxy"}, {"id": "sea_proxy"}],
    [{"id": "sea_proxy", "clauses": [["x", "bad", 1]]}],
    [{"id": "imerg_rain", "clauses": [["max_rainrate_IMERG", "le", float("nan")]]}],
])
def test_resolve_rules_rejects_invalid_overrides(overrides):
    with pytest.raises(ValueError):
        curation.resolve_rules(overrides)


def test_curate_applies_conditions_in_order_and_preserves_input():
    raw = pd.DataFrame([source_row(max_rainrate_IMERG=0), source_row(max_rainrate_IMERG=0.1)])
    original = raw.copy(deep=True)
    selected, steps = curation.curate_frame(raw, curation.resolve_rules([]))
    assert selected.index.tolist() == [0]
    assert steps[10]["removed"] == 1
    assert steps[10]["remaining"] == 1
    assert len(steps) == 13
    pd.testing.assert_frame_equal(raw, original)


def test_curate_disabled_rule_skips_missing_column_and_logs_zero_removals():
    rules = curation.resolve_rules([{"id": "classification_present", "enabled": False}])
    frame = pd.DataFrame([source_row()]).drop(columns="prob_1")
    kept, steps = curation.curate_frame(frame, rules)
    assert len(kept) == 1
    assert steps[7]["status"] == "disabled"
    assert steps[7]["removed"] == 0


def test_curate_rejects_missing_enabled_column_on_empty_frame():
    frame = pd.DataFrame(columns=["another"])
    with pytest.raises(ValueError, match="missing filter column"):
        curation.curate_frame(frame, curation.resolve_rules([]))


def test_default_rules_match_current_thirteen_filter_verdicts(tmp_path):
    invalid = [{"oswLandCoverage": 1}, {"sar_distance_to_coast": 0},
               {"swot_dynamic_ice_flag": 1}, {"ref_time_delta": None},
               {"ref_mean_hs_karin": 0}, {"oswTotalHs": 0},
               {"ref_hs_alti_closest": 0}, {"ww3_hs": 0},
               {"prob_1": -1}, {"ref_time_delta": 7200},
               {"overlap_pct": 99}, {"max_rainrate_IMERG": 0.1},
               {"swot_rain_flag": 1}, {"class_1": "SI"}]
    path = tmp_path / Path(catalogue_path("S1A")).name
    raw = pd.DataFrame([source_row(), *(source_row(**change) for change in invalid)])
    raw.to_parquet(path, index=False)
    actual, steps = curation.curate_frame(raw, curation.resolve_rules([]))
    legacy, old_steps = apply_quality_filters(read_swot_catalogue(path))
    assert actual.index.tolist() == legacy.index.tolist() == [0]
    assert [step["removed"] for step in steps] == [step["removed"] for step in old_steps]


def test_curated_filename_uses_mission_mode_date_variable_version():
    assert curation.curated_filename("S1A", "WV", "20260930", "swh", "0.1") == (
        "S1A_curated_coaligned_dataset_WV_20260930_swh_0.1.parquet")
    with pytest.raises(ValueError, match="WV"):
        curation.curated_filename("S1A", "IW", "20260930", "swh", "0.1")


def test_curated_parquet_keeps_source_schema_and_original_row_ordinals(tmp_path):
    source = tmp_path / Path(catalogue_path("S1A")).name
    target = tmp_path / "curated.parquet"
    original = pa.Table.from_pandas(pd.DataFrame([
        source_row(max_rainrate_IMERG=0.1), source_row(max_rainrate_IMERG=0),
    ]), preserve_index=False).replace_schema_metadata({b"source": b"keep"})
    pq.write_table(original, source)
    ledger = curation.write_curated(source, target, curation.resolve_rules([]), "S1A")
    saved = pq.read_table(target)
    assert list(saved.schema)[:len(original.schema)] == list(original.schema)
    assert saved.schema.metadata == original.schema.metadata
    assert saved.column_names == original.column_names + ["_curation_mission", "_curation_row"]
    assert saved.column("_curation_mission").to_pylist() == ["S1A"]
    assert saved.column("_curation_row").to_pylist() == [1]
    assert saved.schema.field("_curation_row").type == pa.int64()
    assert ledger["input_rows"] == 2
    assert ledger["curated_rows"] == 1
    assert ledger["steps"][10]["removed"] == 1


def test_curated_rejects_reserved_fields_without_writing(tmp_path):
    source, target = tmp_path / "source.parquet", tmp_path / "target.parquet"
    pq.write_table(pa.table({"x": [1], "_curation_row": [0]}), source)
    with pytest.raises(ValueError, match="reserved"):
        curation.write_curated(source, target, [], "S1A")
    assert not target.exists()


def test_merge_reads_saved_curated_files_in_mission_order(tmp_path):
    paths = {}
    for mission, ordinal in (("S1C", 3), ("S1A", 1)):
        path = tmp_path / f"{mission}.parquet"
        pq.write_table(pa.table({"x": [ordinal], "_curation_mission": [mission],
                                 "_curation_row": pa.array([ordinal], type=pa.int64())}), path)
        paths[mission] = path
    merged = tmp_path / "merged.parquet"
    result = curation.merge_curated(paths, merged)
    table = pq.read_table(merged)
    assert result == merged
    assert table.column("x").to_pylist() == [1, 3]
    assert table.column("_curation_mission").to_pylist() == ["S1A", "S1C"]
    assert table.column("_curation_row").to_pylist() == [1, 3]


def test_merge_rejects_incompatible_saved_schema_before_write(tmp_path):
    paths = {}
    for mission, values in (("S1A", pa.array([1])), ("S1C", pa.array(["1"]))):
        path = tmp_path / f"{mission}.parquet"
        pq.write_table(pa.table({"x": values}), path)
        paths[mission] = path
    merged = tmp_path / "merged.parquet"
    with pytest.raises(ValueError, match="schema"):
        curation.merge_curated(paths, merged)
    assert not merged.exists()
