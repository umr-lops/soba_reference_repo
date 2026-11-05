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
from soba_reference_repo.quality_disclaimers import (
    apply_flagged_mask,
    load_flagged_safe_basenames,
)
from soba_reference_repo.validator import main as validate_main, validate_file
from report_checks import assert_report_layout


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


def test_load_flagged_safe_basenames_from_qd_database(tmp_path):
    database = tmp_path / "database"
    database.mkdir()
    pd.DataFrame(
        {
            "product_name": [
                "/data/S1A_WV_SLC__1SSV_20200101T000000.FAFE",
                "S1B_WV_OCN__2SSV_20200102.FAFE",
            ]
        }
    ).to_parquet(database / "impacted_products.parquet", index=False)
    pd.DataFrame(
        {"product_name": ["S1C_WV_SLC__1SSV_20200103T000000.FAFE:WV_001", None]}
    ).to_parquet(database / "product_mentions.parquet", index=False)

    flagged = load_flagged_safe_basenames(database)

    assert flagged == {
        "S1A_WV_SLC__1SSV_20200101T000000.FAFE",
        "S1B_WV_OCN__2SSV_20200102.FAFE",
        "S1C_WV_SLC__1SSV_20200103T000000.FAFE",
    }
    frame = pd.DataFrame(
        {
            "sar_safe_slc": [
                "/x/A.FAFE",
                "/x/S1B_WV_OCN__2SSV_20200102.FAFE:WV_001",
                "/x/C.FAFE",
            ],
            "sar_safe_ocn": ["/x/D.FAFE", "/x/E.FAFE", "/x/F.FAFE"],
        }
    )
    kept, diagnostics = apply_flagged_mask(frame, ("sar_safe_slc", "sar_safe_ocn"), flagged)
    # Row 1's SLC SAFE basename is flagged -> dropped; rows 0 and 2 are kept.
    assert kept["sar_safe_slc"].tolist() == ["/x/A.FAFE", "/x/C.FAFE"]
    assert diagnostics["flagged_rows"] == 1


def test_qd_flagged_rule_drops_listed_slc_or_ocn_products():
    flagged = frozenset({"BAD_SLC.FAFE", "BAD_OCN.FAFE"})
    context = {"qd_flagged": flagged}
    frame = pd.DataFrame(
        {
            "sar_safe_slc": ["/x/GOOD.SAFE", "/x/BAD_SLC.FAFE", "/x/GOOD2.SAFE"],
            "sar_safe_ocn": ["/x/GOOD3.SAFE", "/x/GOOD4.SAFE", "/x/BAD_OCN.FAFE"],
            "ref_param_1": [1.0, 1.0, 1.0],
            "sar_time": [pd.Timestamp("2026-01-01")] * 3,
        }
    )
    rules = [
        {
            "id": "quality_disclaimer_filter",
            "name": "SLC or OCN SAFE not in the quality-disclaimer list",
            "clauses": [
                ["quality_disclaimer_filter", "qd_flagged", ["sar_safe_slc", "sar_safe_ocn"]]
            ],
        }
    ]

    kept, steps = curate_frame(frame, rules, context)

    assert kept.index.tolist() == [0]
    assert steps[0]["removed"] == 2


def test_qd_flagged_rule_requires_config():
    frame = pd.DataFrame(
        {
            "sar_safe_slc": ["/x/A.FAFE"],
            "sar_safe_ocn": ["/x/B.FAFE"],
        }
    )
    rules = [
        {
            "id": "quality_disclaimer_filter",
            "name": "q",
            "clauses": [
                ["quality_disclaimer_filter", "qd_flagged", ["sar_safe_slc", "sar_safe_ocn"]]
            ],
        }
    ]

    with pytest.raises(ValueError, match="quality_disclaimers config"):
        curate_frame(frame, rules)


def test_relaxed_ref_flag_keeps_only_small_wind_bit():
    frame = pd.DataFrame(
        {
            "ref_flag": [0.0, 2048.0, 32768.0, 2080.0, 4096.0],
            "ref_param_2": [5.0, 2.5, 5.0, 2.5, 31.0],
        }
    )
    rules = [
        {
            "id": "zero_ref_flag",
            "name": "no doubtful KNMI quality bits (small wind allowed)",
            "clauses": [["ref_flag", "in", [0, 2048]]],
        }
    ]

    kept, _ = curate_frame(frame, rules)

    # keep flag 0 (clean) and 2048 (small wind <=3 m/s only); reject over_land,
    # small-wind+over_land, and large_wind.
    assert kept.index.tolist() == [0, 1]


def test_hscat_run_with_quality_disclaimer_and_rename(tmp_path, capsys):
    source = hscat_source(tmp_path)
    # Row 1 is dropped by the built-in ECMWF speed rule; row 2 is QD-flagged; row 0 keeps the
    # small-wind bit (2048). Expect only row 0 curated.
    frame = pd.read_parquet(source)
    frame.loc[0, "ref_flag"] = 2048
    frame["sar_safe_slc"] = [
        "/x/GOOD1:WV_001",
        "/x/GOOD2:WV_001",
        "/x/FLAGGED:WV_001",
    ]
    frame.to_parquet(source, index=False)
    database = tmp_path / "qd"
    database.mkdir()
    pd.DataFrame({"product_name": ["FLAGGED"]}).to_parquet(
        database / "impacted_products.parquet", index=False
    )
    recipe = tmp_path / "recipe.toml"
    # quality_disclaimers / rename_columns sit at the TOML root, before [[catalogues]].
    recipe.write_text(
        recipe_text(source)
        .replace(
            'compile = false\n\n[[catalogues]]',
            'compile = false\n'
            'quality_disclaimers = "qd"\n'
            'rename_columns = { ref_param_1 = "scat_wind_direction", '
            'ref_param_2 = "scat_wind_speed" }\n\n[[catalogues]]',
        )
        .replace(
            'id = "zero_ref_flag"\nname = "ref_flag == 0"\n'
            'clauses = [["ref_flag", "eq", 0]]\n',
            'id = "zero_ref_flag"\nname = "no doubtful KNMI quality bits (small wind allowed)"\n'
            'clauses = [["ref_flag", "in", [0, 2048]]]\n',
        )
        .replace(
            '[[catalogues.rules]]\nid = "sar_start"\n',
            '[[catalogues.rules]]\n'
            'id = "quality_disclaimer_filter"\n'
            'name = "SLC or OCN SAFE not in the quality-disclaimer list"\n'
            'clauses = [["quality_disclaimer_filter", "qd_flagged", '
            '["sar_safe_slc", "sar_safe_ocn"]]]\n\n'
            '[[catalogues.rules]]\nid = "sar_start"\n',
        )
    )

    assert cli_main(["--recipe", str(recipe)]) == 0

    manifest = json.loads((tmp_path / "output/manifest.json").read_text())
    curated = pq.read_table(manifest["sources"][0]["curated_path"])
    # speed rule drops row 1, QD rule drops row 2 -> only the small-wind row 0 remains.
    assert manifest["sources"][0]["curated_rows"] == 1
    assert "scat_wind_speed" in curated.column_names
    assert "scat_wind_direction" in curated.column_names
    assert "ref_param_1" not in curated.column_names
    assert "ref_param_2" not in curated.column_names
    # TEST/TARGET outputs use the native HSCAT output columns.
    test = pq.read_table(manifest["test_parquet"])
    assert "scat_windspeed" in test.column_names
    assert [step["id"] for step in manifest["sources"][0]["quality_filter_steps"]] == [
        "ecmwf_scat_speed_difference",
        "ecmwf_scat_direction_difference",
        "zero_ice_probability",
        "zero_ref_flag",
        "quality_disclaimer_filter",
        "sar_start",
    ]
    qd_step = manifest["sources"][0]["quality_filter_steps"][4]
    assert qd_step["id"] == "quality_disclaimer_filter"
    assert qd_step["removed"] == 1


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


@pytest.mark.parametrize("bad_column", ["sar_lon", "ref_param_2"])
def test_hscat_report_handles_nonfinite_curated_values(tmp_path, bad_column):
    source = hscat_source(tmp_path)
    frame = pd.read_parquet(source)
    frame.loc[0, bad_column] = float("nan")
    frame.to_parquet(source, index=False)
    text = recipe_text(source)
    if bad_column == "ref_param_2":
        text = text.replace(
            'id = "ecmwf_scat_speed_difference"\n',
            'id = "ecmwf_scat_speed_difference"\nenabled = false\n',
        )
    recipe = tmp_path / "recipe.toml"
    recipe.write_text(text, encoding="utf-8")
    assert cli_main(["--recipe", str(recipe)]) == 0
    manifest = json.loads((tmp_path / "output/manifest.json").read_text())
    assert manifest["sources"][0]["curated_rows"] == (2 if bad_column == "sar_lon" else 3)
    assert manifest["accepted_rows"] == manifest["sources"][0]["curated_rows"] - 1
    assert_report_layout(manifest)


def test_hscat_run_applies_recipe_wind_and_quality_filters(tmp_path, capsys):
    source = hscat_source(tmp_path)
    recipe = tmp_path / "recipe.toml"
    recipe.write_text(recipe_text(source))

    assert cli_main(["--recipe", str(recipe)]) == 0

    tex = (tmp_path / "output/manifest.json").read_text()
    manifest = json.loads(tex)
    test_path = Path(manifest["test_parquet"])
    target_path = Path(manifest["target_parquet"])
    assert Path(manifest["report_tex"]).name == (
        "S1_curated_WV_SV_KNMI-HSCAT-HY2-25km.tex"
    )
    assert Path(manifest["figures"]["coverage"]).parent.name == (
        "images_S1_curated_WV_SV_KNMI-HSCAT-HY2-25km"
    )
    report = Path(manifest["report_tex"]).read_text()
    assert "Catalogue Columns" in report
    assert "Selection of Tiles / Filtered Co-locations" in report
    assert "TEST" not in report and "TARGET" not in report
    assert r"ref\_param\_1" in report and r"ref\_param\_2" in report
    assert Path(manifest["report_tex"]).parent == tmp_path / "output/report"
    assert test_path.name == "S1_reference_test_dataset_WV_20260103_windspeed_0.1.parquet"
    assert target_path.name == "S1_target_dataset_WV_20260103_windspeed_0.1.parquet"
    sections = [
        "Version History", "Dataset Content Description", "Global Coverage Map",
        "Monthly Distribution of Matchups", "HSCAT Wind-Speed and Wind-Direction Distributions",
        "Catalogue Columns", "Global Metadata", "Dataset Production Steps",
        "Selected Products", "Selection of Tiles / Filtered Co-locations",
    ]
    positions = [report.index(section) for section in sections]
    assert positions == sorted(positions)
    assert "wind-direction agreement with meteorological estimates" in report
    assert "ice-free reference observations" in report
    assert "strictly after 2025-12-31 at 00:00 UTC" in report
    assert _filter_expression([["sar_time", "date_lt", "2025-12-31"]]) == (
        r"sar\_time is strictly earlier than 00:00 UTC on 2025-12-31"
    )
    assert "S1A_curated_WV_20160101_20160102_20260103_" in report
    assert "KNMI for EUMETSAT OSI SAF" in report
    assert report.count(r"\includegraphics[") == 6
    assert_report_layout(manifest)
    reference_plot = mpimg.imread(manifest["figures"]["reference_distribution"])
    assert reference_plot.shape[1] > reference_plot.shape[0]
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
