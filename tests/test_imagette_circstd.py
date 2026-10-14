import json
import subprocess
import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import numpy as np
import pandas as pd
import pytest

from soba_reference_repo.curation import curate_frame, resolve_rules
from soba_reference_repo.create_test_dataset import main as cli_main
from test_hscat_pipeline import hscat_source, recipe_text

SAFE = "S1A_WV_SLC__1SSV_20260101T000000_20260101T000100_000001_000001_ABCD.SAFE"


def rule(threshold=10, **kwargs):
    return {"id": "direction_spread", "name": "Catalogue direction spread",
            "clauses": [["ref_param_1", "imagette_circstd_le", ["sar_safe_slc", threshold]]],
            **kwargs}


def test_groups_eligible_rows_across_satellites_by_normalized_imagette():
    frame = pd.DataFrame({
        "sar_safe_slc": [f"/a/{SAFE}:WV_001", f"/b/{SAFE}:WV_001",
                         f"{SAFE}:WV_002", f"{SAFE}:WV_002", f"{SAFE}:WV_002"],
        "ref_param_1": [0., 40., 359., 1., 180.],
        "ref_id": ["HY2B", "HY2C", "HY2B", "HY2C", "HY2B"],
        "eligible": [1, 1, 1, 1, 0],
    })
    rules = resolve_rules([
        {"id": "eligible", "name": "Eligible", "clauses": [["eligible", "eq", 1]]},
        rule(),
    ], include_defaults=False)
    kept, steps = curate_frame(frame, rules)
    assert kept.index.tolist() == [2, 3]
    assert steps[-1]["evaluated_multi_matchup_groups"] == 2
    assert steps[-1]["rejected_imagettes"] == 1
    assert steps[-1]["spread_removed_rows"] == 2


@pytest.mark.parametrize("directions,threshold,passes", [
    ([355, 2, 5], 4.191336537, True),
    ([355, 2, 5], 4.1913, False),
    ([40, 45, 60], 10, True),  # Not a maximum pairwise difference.
    ([13.1, 13.1], 0, True),
    ([13.1], 0, True),
    ([0, 180], 1000, False),
    ([0, 120, 240], 1000, False),
    ([0, 360, -360], 0, True),
    ([0, 40], 10, False),
    ([0, 0, 0, 30], 14, True),  # Equal catalogue-row weights, not satellite weights.
])
def test_circular_population_std(directions, threshold, passes):
    frame = pd.DataFrame({"sar_safe_slc": [f"{SAFE}:WV_001"] * len(directions),
                          "ref_param_1": directions})
    kept, _ = curate_frame(frame, [rule(threshold)])
    assert len(kept) == (len(frame) if passes else 0)


def test_boundary_ten_degrees_and_just_above():
    for sigma, passes in [(10, True), (10 + 1e-7, False)]:
        half_angle = np.rad2deg(np.arccos(np.exp(-np.deg2rad(sigma)**2 / 2)))
        frame = pd.DataFrame({"sar_safe_slc": [f"{SAFE}:WV_001"] * 2,
                              "ref_param_1": [-half_angle, half_angle]})
        kept, _ = curate_frame(frame, [rule()])
        assert len(kept) == (2 if passes else 0)


def test_invalid_directions_singletons_and_empty_groups_are_accounted_separately():
    frame = pd.DataFrame({"sar_safe_slc": [f"{SAFE}:WV_{i:03}" for i in [1, 1, 2, 2, 3]],
                          "ref_param_1": [None, np.inf, 23., np.nan, -np.inf]})
    kept, steps = curate_frame(frame, [rule()])
    assert kept.index.tolist() == [2]
    step = steps[0]
    assert step["invalid_direction_rows"] == 4
    assert step["singleton_groups"] == 1
    assert step["all_invalid_groups"] == 2
    assert step["rejected_imagettes"] == 0
    assert step["evaluated_multi_matchup_groups"] == 0
    assert step["before"] == (step["remaining"] + step["invalid_direction_rows"]
                               + step["spread_removed_rows"])
    empty, steps = curate_frame(frame.iloc[:0], [rule()])
    assert empty.empty
    assert steps[0]["all_invalid_groups"] == 0


@pytest.mark.parametrize("identity", [None, "", SAFE, SAFE + ":WV_1", SAFE + ":WV_001junk"])
def test_missing_or_malformed_imagette_is_an_error(identity):
    frame = pd.DataFrame({"sar_safe_slc": [identity], "ref_param_1": [None]})
    with pytest.raises(ValueError, match="imagette identity"):
        curate_frame(frame, [rule()])


@pytest.mark.parametrize("threshold", [-1, np.inf, np.nan, True, "10"])
def test_invalid_threshold(threshold):
    with pytest.raises(ValueError, match="invalid filter"):
        resolve_rules([rule(threshold)], include_defaults=False)


@pytest.mark.parametrize("rules", [
    [rule(), {"id": "later", "name": "Later", "clauses": [["x", "eq", 0]]}],
    [rule(clauses=[rule()["clauses"][0], ["x", "eq", 0]])],
])
def test_group_rule_must_be_sole_clause_and_last_enabled(rules):
    with pytest.raises(ValueError, match="sole clause.*last enabled"):
        resolve_rules(rules, include_defaults=False)
    with pytest.raises(ValueError, match="sole clause.*last enabled"):
        curate_frame(pd.DataFrame(), rules)


def test_disabled_or_absent_rule_does_not_change_rows_or_validate_identity():
    frame = pd.DataFrame({"sar_safe_slc": ["not an imagette"], "ref_param_1": [None]})
    for rules in [[], [rule(enabled=False)]]:
        kept, _ = curate_frame(frame, rules)
        pd.testing.assert_frame_equal(kept, frame)
    rules = resolve_rules([rule(), {"id": "off", "name": "Off", "enabled": False,
                                   "clauses": [["absent", "eq", 1]]}], include_defaults=False)
    valid = pd.DataFrame({"sar_safe_slc": [f"{SAFE}:WV_001"], "ref_param_1": [0.]})
    assert len(curate_frame(valid, rules)[0]) == 1


GROUP_TOML = '''
[[catalogues.rules]]
id = "direction_spread"
name = "Catalogue direction spread"
clauses = [["ref_param_1", "imagette_circstd_le", ["sar_safe_slc", 10]]]
'''


@pytest.mark.parametrize("variable", ["windspeed", "winddirection"])
def test_cli_group_rule_native_curated_and_duplicate_policy(tmp_path, variable):
    source = hscat_source(tmp_path)
    original = pq.read_table(source)
    frame = original.to_pandas().iloc[[0] * 9].reset_index(drop=True)
    frame["primary_key"] = ["spread-a", "spread-b", "duplicate", "duplicate",
                            "unique", "ineligible", "invalid", "all-invalid", "singleton"]
    frame["sar_safe_slc"] = [f"/{prefix}/{SAFE}:WV_{i:03}" for prefix, i in
                             zip("ababababa", [1, 1, 2, 2, 2, 2, 2, 3, 4])]
    frame["ref_id"] = ["HY2B", "HY2C"] * 4 + ["HY2B"]
    frame["ref_param_1"] = [0., 40., 40., 45., 60., 180., np.nan, np.inf, 42.]
    frame["ecmwf_wind_dir_360"] = frame["ref_param_1"]
    frame.loc[5, "ecmwf_wind_speed"] = 0.
    table = pa.Table.from_pandas(frame, preserve_index=False).replace_schema_metadata(
        original.schema.metadata)
    pq.write_table(table, source)
    recipe = tmp_path / "recipe.toml"
    # Disable the rowwise direction filter so invalid directions reach group accounting.
    text = recipe_text(source).replace(
        'id = "ecmwf_scat_direction_difference"',
        'id = "ecmwf_scat_direction_difference"\nenabled = false')
    recipe.write_text(text.replace('"windspeed"', f'"{variable}"') + GROUP_TOML)
    result = subprocess.run(
        [sys.executable, "-m", "soba_reference_repo.create_test_dataset", "--recipe", str(recipe)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    manifest = json.loads((tmp_path / "output/manifest.json").read_text())
    entry = manifest["sources"][0]
    curated = pq.read_table(entry["curated_path"])
    assert curated["primary_key"].to_pylist() == ["duplicate", "duplicate", "unique", "singleton"]
    assert curated["sar_safe_slc"].to_pylist() == frame.loc[[2, 3, 4, 8], "sar_safe_slc"].tolist()
    assert curated["_curation_row"].to_pylist() == [2, 3, 4, 8]
    assert curated.schema.metadata == original.schema.metadata
    for field in table.schema:
        assert curated.schema.field(field.name) == field
    for role in ["test", "target"]:
        assert pq.read_table(manifest[f"{role}_parquet"])["primary_key"].to_pylist() == [
            "unique", "singleton"]
    assert manifest["excluded_duplicate_key_rows"] == 2
    step = entry["quality_filter_steps"][-1]
    assert step["evaluated_multi_matchup_groups"] == 2
    assert step["rejected_imagettes"] == 1
    assert step["invalid_direction_rows"] == 2
    assert step["all_invalid_groups"] == 1
    assert step["spread_removed_rows"] == 2
    report = Path(manifest["report_tex"]).read_text()
    assert "circular population STD" in report
    assert "evaluated multi-matchup groups: 2" in report
    assert "spread-rejected imagettes: 1" in report
    assert "invalid direction rows: 2" in report


@pytest.mark.parametrize("operand", ["missing_direction", "missing_identity"])
def test_active_group_fields_preflight_before_output(tmp_path, operand):
    source = hscat_source(tmp_path)
    text = GROUP_TOML.replace(
        "ref_param_1" if operand == "missing_direction" else "sar_safe_slc", operand)
    recipe = tmp_path / "recipe.toml"
    recipe.write_text(recipe_text(source) + text)
    with pytest.raises(ValueError, match="missing filter column"):
        cli_main(["--recipe", str(recipe)])
    assert not (tmp_path / "output").exists()


def test_ineligible_malformed_identity_does_not_enter_group():
    frame = pd.DataFrame({"sar_safe_slc": [None, f"{SAFE}:WV_001"],
                          "ref_param_1": [0., 23.], "eligible": [0, 1]})
    rules = [{"id": "eligible", "name": "Eligible", "clauses": [["eligible", "eq", 1]]},
             rule()]
    assert curate_frame(frame, rules)[0].index.tolist() == [1]
