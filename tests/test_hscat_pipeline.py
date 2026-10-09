import json
from pathlib import Path

from matplotlib import image as mpimg
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from soba_reference_repo.create_test_dataset import main as cli_main
from soba_reference_repo.curation import curate_frame, resolve_rules
from soba_reference_repo.hscat_report import (
    _annual_tick_indices,
    _filter_expression,
    _filter_tables,
)
from soba_reference_repo.hscat_test import (
    HSCAT_DEFAULT_RULES,
    build_scat_frames,
    write_scat_pair_chunked,
)
from soba_reference_repo.validator import main as validate_main, validate_file


def hscat_source(folder, mission="S1A", include_ecmwf=True):
    path = folder / (
        f"{mission}_coaligned_catalogue_WV_20160101_20160102_20260101_"
        "SV_KNMI-HSCAT-HY2-25km_0.1.parquet"
    )
    frame = pd.DataFrame(
        {
            "primary_key": [f"{mission}_key_{i}" for i in range(3)],
            "sar_time": [pd.Timestamp("2026-01-01")] * 3,
            "sar_lat": [2.1] * 3,
            "sar_lon": [-1.1] * 3,
            "sar_incidence_angle": [30.0] * 3,
            "sar_elevation_angle": [15.0] * 3,
            "sar_ground_heading": [180.0] * 3,
            "sar_safe_slc": [f"/{mission}_WV_SLC_SAFE"] * 3,
            "sar_safe_ocn": [f"/{mission}_WV_OCN_SAFE"] * 3,
            "ref_lon": [-1.0] * 3,
            "ref_lat": [2.0] * 3,
            "ref_param_1": [45.0] * 3,
            "ref_param_2": [5.0] * 3,
            "ref_time": [pd.Timestamp("2026-01-01")] * 3,
            "ref_id": ["scatterometer.granule"] * 3,
            "ref_ice_prob": [0.0] * 3,
            "ref_flag": [0] * 3,
            "ecmwf_wind_dir_360": [45.0, 44.0, 46.0],
        }
    )
    if include_ecmwf:
        frame["ecmwf_wind_speed"] = [3.0, 1.0, 3.01]
    table = pa.Table.from_pandas(frame, preserve_index=False)
    attrs = {
        "source ref": "HY-2B/HY-2C/HY-2D 25km KNMI",
        "ref_param_1": "scat_ref_wind_direction (degree), clockwise",
        "ref_param_2": "scat_ref_wind_speed (m/s)",
    }
    pandas_metadata = json.loads(table.schema.metadata[b"pandas"])
    pandas_metadata["attributes"] = attrs
    pq.write_table(
        table.replace_schema_metadata(
            {**table.schema.metadata, b"pandas": json.dumps(pandas_metadata).encode()}
        ),
        path,
    )
    return path


def recipe_text(source, output="output"):
    text = (
        'reference = "scat"\nreference_variable = "windspeed"\n'
        f'output_dir = "{output}"\nproduction_date = "20260103"\n'
        'version = "0.1"\ncompile = false\n\n[[catalogues]]\n'
        'mission = "S1A"\n'
        f'path = "{source.name}"\n'
    )
    return text + '''
[[catalogues.rules]]
id = "ecmwf_scat_speed_difference"
name = "ECMWF-HSCAT wind-speed difference <= 2 m/s"
clauses = [["ecmwf_wind_speed", "abs_diff_le", ["ref_param_2", 2]]]

[[catalogues.rules]]
id = "ecmwf_scat_direction_difference"
name = "Circular ECMWF-HSCAT wind-direction difference <= 30 degrees"
clauses = [["ecmwf_wind_dir_360", "angle_diff_le", ["ref_param_1", 30]]]

[[catalogues.rules]]
id = "zero_ice_probability"
name = "ref_ice_prob == 0 (WV)"
clauses = [["ref_ice_prob", "eq", 0]]

[[catalogues.rules]]
id = "zero_ref_flag"
name = "ref_flag == 0"
clauses = [["ref_flag", "eq", 0]]

[[catalogues.rules]]
id = "sar_start"
name = "SAR acquisitions after 2025-12-31"
clauses = [["sar_time", "date_gt", "2025-12-31"]]
'''


def test_monthly_chart_ticks_show_years_and_final_month():
    months = pd.date_range("2019-01-01", periods=30, freq="MS").strftime("%Y-%m").tolist()
    indices = _annual_tick_indices(months)

    assert [months[index] for index in indices] == [
        "2019-01",
        "2020-01",
        "2021-01",
        "2021-06",
    ]


def test_mission_specific_start_dates_are_rendered_per_mission():
    cutoffs = {
        "S1A": "2024-11-25",
        "S1C": "2025-05-06",
        "S1D": "2026-06-03",
    }
    sources = [
        {
            "mission": mission,
            "quality_filter_steps": [
                {
                    "id": "sar_start",
                    "name": f"SAR acquisitions after {cutoff}",
                    "clauses": [["sar_time", "date_gt", cutoff]],
                    "remaining": 4,
                    "removed": 2,
                }
            ],
        }
        for mission, cutoff in cutoffs.items()
    ]

    report_tables = _filter_tables(sources)

    assert "Mission-specific quality filters" in report_tables
    assert "SAR acquisition start-date cutoff (mission-specific)" in report_tables
    assert r"\newpage" in report_tables
    assert report_tables.index(r"\newpage") < report_tables.index(r"\begin{table}[H]")
    specific_table = report_tables.split(r"\begin{table}[H]", 1)[1].split(
        r"\end{table}", 1
    )[0]
    assert r"\begin{tabular}" in specific_table
    assert r"\begin{longtable}" not in specific_table
    for mission, cutoff in cutoffs.items():
        assert f"{mission} & SAR acquisitions after {cutoff}" in report_tables
        assert f"UTC on {cutoff}" in report_tables


def test_hscat_recipe_filters_direction_circularity_ice_and_ref_flag():
    rules = resolve_rules(
        [
            {
                "id": "ecmwf_scat_direction_difference",
                "name": "circular direction difference <= 30 degrees",
                "clauses": [["ecmwf_wind_dir_360", "angle_diff_le", ["ref_param_1", 30]]],
            },
            {
                "id": "zero_ice_probability",
                "name": "ref_ice_prob == 0",
                "clauses": [["ref_ice_prob", "eq", 0]],
            },
            {
                "id": "zero_ref_flag",
                "name": "ref_flag == 0",
                "clauses": [["ref_flag", "eq", 0]],
            },
        ],
        defaults=HSCAT_DEFAULT_RULES,
    )
    frame = pd.DataFrame(
        {
            "ecmwf_wind_speed": [5.0] * 5,
            "ref_param_2": [5.0] * 5,
            "ecmwf_wind_dir_360": [359.0, 359.0, 359.0, 0.0, 0.0],
            "ref_param_1": [1.0, 1.0, 1.0, 31.0, 330.0],
            "ref_ice_prob": [0.0, 0.0, 0.01, 0.0, 0.0],
            "ref_flag": [0.0, 1.0, 0.0, 0.0, 0.0],
        }
    )

    kept, steps = curate_frame(frame, rules)

    assert kept.index.tolist() == [0, 4]
    assert [step["id"] for step in steps] == [
        "ecmwf_scat_speed_difference",
        "ecmwf_scat_direction_difference",
        "zero_ice_probability",
        "zero_ref_flag",
    ]


def test_hscat_run_applies_recipe_wind_and_quality_filters(tmp_path, capsys):
    source = hscat_source(tmp_path)
    recipe = tmp_path / "recipe.toml"
    recipe.write_text(recipe_text(source))

    assert cli_main(["--recipe", str(recipe)]) == 0

    tex = (tmp_path / "output/manifest.json").read_text()
    manifest = json.loads(tex)
    assert manifest["status"] == "complete"
    report = Path(manifest["report_tex"]).read_text()
    assert report
    assert Path(manifest["report_tex"]).parent == tmp_path / "output/report"
    assert r"\texttt{scat\_windspeed}" in report
    sections = [
        "Global Coverage Map",
        "Monthly Distribution of Matchups",
        "Reference Parameter Distribution",
        "Catalogue Columns",
        "Global Metadata",
        "Dataset Production Steps",
    ]
    positions = [report.index(section) for section in sections]
    assert positions == sorted(positions)
    captions = [
        "Global spatial distribution of all retained Sentinel-1 WV matchups",
        "Monthly count of retained HSCAT matchups",
        "Normalized HSCAT windspeed step histograms by mission.",
    ]
    caption_positions = [report.index(caption) for caption in captions]
    assert caption_positions == sorted(caption_positions)
    assert "Versioning of the documentation" in report
    assert "Versioning of test catalogue files" in report
    assert "Creation of the HSCAT TEST dataset document & Ilias Reguig" in report
    assert "circular difference (ecmwf\\_wind\\_dir\\_360, ref\\_param\\_1)" in report
    assert "at most 30 degrees" in report
    assert "ref\\_ice\\_prob == 0" in report
    assert "ref\\_flag == 0" in report
    assert "SAR acquisitions after 2025-12-31" in report
    assert "strictly later than 00:00 UTC on 2025-12-31" in report
    assert _filter_expression([["sar_time", "date_lt", "2025-12-31"]]) == (
        r"sar\_time is strictly earlier than 00:00 UTC on 2025-12-31"
    )
    assert "\\path|S1A_curated_coaligned_dataset_WV_20260103_windspeed_0.1.parquet|" in report
    assert report.count("\\section*{References}") == 0
    assert report.count("\\begin{thebibliography}") == 1
    assert "shortest angular distance" in report
    assert report.count(r"\begin{figure}[H]") == 3
    figure_paths = [Path(path) for path in manifest["figures"].values()]
    assert len(figure_paths) == 3
    reference_plot = mpimg.imread(manifest["figures"]["reference_distribution"])
    assert reference_plot.shape[:2] == (750, 1350)
    assert all(path.parent == tmp_path / "output/report" and path.is_file()
               for path in figure_paths)
    assert not any(path.is_dir() for path in (tmp_path / "output/report").iterdir())
    assert not (tmp_path / "output/merged").exists()
    assert "curated_merge" not in manifest
    assert [entry["id"] for entry in manifest["filters_applied"]] == [
        "ecmwf_scat_speed_difference",
        "ecmwf_scat_direction_difference",
        "zero_ice_probability",
        "zero_ref_flag",
        "sar_start",
    ]
    assert manifest["sources"][0]["curated_rows"] == 2
    test = pq.read_table(manifest["test_parquet"])
    target = pq.read_table(manifest["target_parquet"])
    assert test["primary_key"].to_pylist() == target["primary_key"].to_pylist()
    assert validate_file(manifest["test_parquet"], "test", "scat")
    assert validate_file(manifest["target_parquet"], "target", "scat")
    for role, output_path in (("test", manifest["test_parquet"]),
                              ("target", manifest["target_parquet"])):
        assert validate_main([
            "--type", role, "--file", output_path, "--reference", "scat"
        ]) == 0
        assert "Selected SCAT reference variable is present" in capsys.readouterr().out


def test_hscat_preflight_requires_ecmwf_wind_speed(tmp_path):
    source = hscat_source(tmp_path, include_ecmwf=False)
    recipe = tmp_path / "recipe.toml"
    recipe.write_text(recipe_text(source))

    with pytest.raises(ValueError, match="ecmwf_wind_speed"):
        cli_main(["--recipe", str(recipe)])
    assert not (tmp_path / "output").exists()


def test_reference_recipe_rejects_non_hscat_catalogue_before_output(tmp_path):
    source = hscat_source(tmp_path)
    invalid = tmp_path / source.name.replace("HSCAT-HY2-25km", "UNSUPPORTED")
    source.rename(invalid)
    recipe = tmp_path / "recipe.toml"
    recipe.write_text(recipe_text(invalid))

    with pytest.raises(ValueError, match="unsupported HSCAT catalogue"):
        cli_main(["--recipe", str(recipe)])
    assert not (tmp_path / "output").exists()


def test_hscat_pair_excludes_every_row_with_a_duplicate_primary_key(tmp_path):
    source = hscat_source(tmp_path)
    frame = pq.read_table(source).to_pandas()
    frame.loc[1, "primary_key"] = frame.loc[0, "primary_key"]
    frame.loc[1, "ref_param_2"] = 5.5
    frame["_curation_mission"] = "S1A"

    test, target, summary = build_scat_frames(frame, "windspeed")

    assert test["primary_key"].to_list() == ["S1A_key_2"]
    assert target["primary_key"].to_list() == ["S1A_key_2"]
    assert summary["excluded_duplicate_key_rows"] == 2
    assert summary["duplicate_key_rows_by_mission"] == {"S1A": 2}


def test_chunked_pair_excludes_duplicate_keys_across_batch_boundaries(tmp_path):
    source = hscat_source(tmp_path)
    frame = pq.read_table(source).to_pandas()
    frame["primary_key"] = ["duplicate", "unique-1", "duplicate"]
    frame["_curation_mission"] = "S1A"
    source = tmp_path / "merged.parquet"
    pq.write_table(pa.Table.from_pandas(frame, preserve_index=False), source)
    test_path, target_path = tmp_path / "TEST.parquet", tmp_path / "TARGET.parquet"

    summary = write_scat_pair_chunked(source, test_path, target_path, "windspeed", batch_size=2)

    test, target = pq.read_table(test_path), pq.read_table(target_path)
    assert test["primary_key"].to_pylist() == ["unique-1"]
    assert target["primary_key"].to_pylist() == ["unique-1"]
    assert summary["excluded_duplicate_key_rows"] == 2
    assert summary["duplicate_key_rows_by_mission"] == {"S1A": 2}
    assert summary["accepted_rows"] == 1


def test_chunked_duplicate_exclusions_do_not_overlap_incomplete_rows(tmp_path):
    source = hscat_source(tmp_path)
    frame = pq.read_table(source).to_pandas()
    frame["primary_key"] = ["ambiguous", "ambiguous", "complete"]
    frame.loc[1, "sar_lat"] = float("nan")
    frame["_curation_mission"] = "S1A"
    source = tmp_path / "merged_with_incomplete.parquet"
    pq.write_table(pa.Table.from_pandas(frame, preserve_index=False), source)

    summary = write_scat_pair_chunked(
        source, tmp_path / "TEST2.parquet", tmp_path / "TARGET2.parquet", "windspeed", batch_size=1
    )

    assert summary["accepted_rows"] == 1
    assert summary["excluded_incomplete_rows"] == 1
    assert summary["excluded_duplicate_key_rows"] == 1
    assert summary["accepted_rows"] + summary["excluded_incomplete_rows"] + summary[
        "excluded_duplicate_key_rows"
    ] == len(frame)
    assert pq.read_table(tmp_path / "TEST2.parquet")["primary_key"].to_pylist() == ["complete"]
