"""WV x altimeter curation: a curation-only reference with no SWOT pair/report."""

import json

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from soba_reference_repo import alti_test, curation

CCI_PRODUCT = "CCI-SEATSTATE_ALTI_L2P-V5"
CMEMS_PRODUCT = "CMEMS-ALTI-L3-VAVH"


def alti_name(mission, product):
    return (
        f"{mission}_coaligned_catalogue_WV_20150319T134347_20251231T192542_"
        f"20261018T021205_SV_{product}_1.0.parquet"
    )


def write_alti_source(tmp_path, mission, product):
    path = tmp_path / alti_name(mission, product)
    table = pa.table({
        "imerg_rainrate": pa.array([0.0, 0.5, 0.0, 0.0]),
        "sar_safe_slc": pa.array(["a", "b", "c", "d"]),
        "ref_lon": pa.array([1.0, 2.0, 3.0, 4.0]),
        "ref_lat": pa.array([10.0, 20.0, 30.0, 40.0]),
    })
    pq.write_table(table, path)
    return path


def recipe_text(entries, output_dir="output"):
    blocks = ['reference = "altimeter"',
              f'output_dir = "{output_dir}"',
              'production_date = "20261019"',
              'version = "1.0"',
              '']
    for mission, path in entries:
        blocks += ['[[catalogues]]', f'mission = "{mission}"', f'path = "{path}"',
                   '[[catalogues.rules]]', 'id = "no_rain"', 'name = "IMERG rain rate == 0"',
                   'clauses = [["imerg_rainrate", "eq", 0]]', '']
    return "\n".join(blocks)


def test_read_recipe_altimeter_allows_multiple_catalogues_per_mission(tmp_path):
    p1 = write_alti_source(tmp_path, "S1A", CCI_PRODUCT)
    p2 = write_alti_source(tmp_path, "S1A", CMEMS_PRODUCT)
    recipe = tmp_path / "recipe.toml"
    recipe.write_text(recipe_text([("S1A", str(p1)), ("S1A", str(p2))]))

    parsed = curation.read_recipe(recipe)

    assert parsed["reference"] == "altimeter"
    assert [c["mission"] for c in parsed["catalogues"]] == ["S1A", "S1A"]
    assert len(parsed["catalogues"]) == 2


def test_read_recipe_altimeter_rejects_non_altimeter_filename(tmp_path):
    # S1A mission but an HSCAT (date, not timestamp) filename.
    path = tmp_path / "S1A_coaligned_catalogue_WV_20190116_20260629_20260908_" \
                       "SV_KNMI-HSCAT-HY2-25km_0.2.parquet"
    path.touch()
    recipe = tmp_path / "recipe.toml"
    recipe.write_text(recipe_text([("S1A", str(path))]))
    with pytest.raises(ValueError, match="mission/filename conflict"):
        curation.read_recipe(recipe)


def test_curated_filename_altimeter_strips_time_from_catalogue_dates():
    assert curation.curated_filename(alti_name("S1A", CCI_PRODUCT), "20261019", "1.0") == (
        "S1A_curated_WV_20150319_20251231_20261019_"
        f"SV_{CCI_PRODUCT}_1.0.parquet"
    )


def test_curated_filename_rejects_start_after_stop():
    with pytest.raises(ValueError, match="startdate"):
        curation.curated_filename("S1A_coaligned_catalogue_WV_20251231T192542_20150319T134347_"
                                   "20261018T021205_SV_CCI_1.0.parquet", "20261019", "1.0")


def test_run_alti_recipe_curates_no_rain_and_writes_manifest(tmp_path):
    p1 = write_alti_source(tmp_path, "S1A", CCI_PRODUCT)
    p2 = write_alti_source(tmp_path, "S1A", CMEMS_PRODUCT)
    recipe = tmp_path / "recipe.toml"
    recipe.write_text(recipe_text([("S1A", str(p1)), ("S1A", str(p2))], output_dir="run"))

    assert alti_test.run_alti_recipe(recipe) == 0

    root = tmp_path / "run"
    curated = sorted((root / "curated" / "S1A").glob("*.parquet"))
    assert len(curated) == 2
    # each source had 4 rows, 3 with imerg_rainrate == 0 -> 3 kept
    for path in curated:
        kept = pq.read_table(path)
        assert kept.num_rows == 3
        assert "imerg_rainrate" in kept.column_names
        assert kept.column("imerg_rainrate").to_pylist() == [0.0, 0.0, 0.0]
        assert "_curation_mission" in kept.column_names
        assert "_curation_row" in kept.column_names

    manifest = json.loads((root / "manifest.json").read_text())
    assert manifest["status"] == "complete"
    assert manifest["input_rows"] == 8
    assert manifest["curated_rows"] == 6
    assert len(manifest["sources"]) == 2


def test_run_alti_recipe_applies_no_default_swot_rules(tmp_path):
    # A source with only the filter column. If the 13 SWOT defaults ran, this
    # would fail on missing columns (oswLandCoverage, ref_mean_hs_karin, ...).
    path = tmp_path / alti_name("S1C", CMEMS_PRODUCT)
    pq.write_table(pa.table({"imerg_rainrate": pa.array([0.0, 0.3])}), path)
    recipe = tmp_path / "recipe.toml"
    recipe.write_text(recipe_text([("S1C", str(path))], output_dir="run"))

    assert alti_test.run_alti_recipe(recipe) == 0
    kept = pq.read_table(sorted((tmp_path / "run" / "curated" / "S1C").glob("*.parquet"))[0])
    assert kept.num_rows == 1


def test_run_alti_recipe_rejects_existing_output_dir(tmp_path):
    p1 = write_alti_source(tmp_path, "S1A", CCI_PRODUCT)
    (tmp_path / "run").mkdir()
    recipe = tmp_path / "recipe.toml"
    recipe.write_text(recipe_text([("S1A", str(p1))], output_dir="run"))
    with pytest.raises(FileExistsError):
        alti_test.run_alti_recipe(recipe)
