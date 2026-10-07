"""Recipe-path integration tests for saved Curated SWOT catalogues."""

import json
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq
import pytest

from soba_reference_repo.create_test_dataset import main as cli_main
from soba_reference_repo import swot_test
from test_swot_test import source_row


def source_file(folder, mission, **changes):
    path = folder / f"{mission}_coaligned_catalogue_WV_sample.parquet"
    row = source_row(
        sar_safe_slc=f"/archive/{mission}_WV_SLC__1SSV_20260107.SAFE:WV_033",
        sar_safe_ocn=f"/archive/{mission}_WV_OCN__2SSV_20260107.SAFE:WV_033",
        **changes,
    )
    pd.DataFrame([row]).to_parquet(path, index=False)
    return path


def test_generator_uses_saved_merge_without_reapplying_default_filters(tmp_path):
    from soba_reference_repo.curation import merge_curated, resolve_rules, write_curated

    paths = {
        mission: source_file(tmp_path, mission, **changes)
        for mission, changes in (("S1A", {}), ("S1C", {"max_rainrate_IMERG": 0.1}))
    }
    curated = {}
    provenance = {}
    for mission, source in paths.items():
        rules = resolve_rules(
            [{"id": "imerg_rain", "clauses": [["max_rainrate_IMERG", "le", 0.1]]}]
            if mission == "S1C"
            else []
        )
        destination = tmp_path / f"{mission}_curated.parquet"
        info = write_curated(source, destination, rules, mission)
        curated[mission] = destination
        provenance[mission] = {
            "path": str(source), "curated_path": str(destination),
            "input_rows": info["input_rows"], "steps": info["steps"],
        }
    merged = tmp_path / "merged.parquet"
    merge_curated(curated, merged)

    test, target, manifest = swot_test.build_swot_test_from_merged(merged, provenance)
    assert test.sar_safe_slc.str.startswith(("S1A", "S1C")).all()
    assert len(test) == 2
    assert target.primary_key.tolist() == test.primary_key.tolist()
    assert [item["mission"] for item in manifest["sources"]] == ["S1A", "S1C"]


def test_recipe_cli_keeps_curated_merge_and_pair_and_reports_real_rules(tmp_path):
    a = source_file(tmp_path, "S1A")
    c = source_file(tmp_path, "S1C", max_rainrate_IMERG=0.1)
    recipe = tmp_path / "recipe.toml"
    recipe.write_text(
        'reference = "swot"\noutput_dir = "output"\nproduction_date = "20260930"\n'
        'version = "0.1"\ncompile = false\n'
        f'[[catalogues]]\nmission = "S1A"\npath = "{a.name}"\n'
        f'[[catalogues]]\nmission = "S1C"\npath = "{c.name}"\n'
        '[[catalogues.rules]]\nid = "imerg_rain"\n'
        'clauses = [["max_rainrate_IMERG", "le", 0.1]]\n',
        encoding="utf-8",
    )
    assert cli_main(["--recipe", str(recipe)]) == 0
    root = tmp_path / "output"
    assert (root / "curated/S1A" /
            "S1A_curated_coaligned_dataset_WV_20260930_swh_0.1.parquet").is_file()
    assert (root / "curated/S1C" /
            "S1C_curated_coaligned_dataset_WV_20260930_swh_0.1.parquet").is_file()
    assert pq.read_table(root / "merged/S1_WV_swot_curated.parquet").num_rows == 2
    test_path, = list((root / "datasets").rglob("S1_reference_test_dataset_*.parquet"))
    target_path, = list((root / "datasets").rglob("S1_target_dataset_*.parquet"))
    assert swot_test.validate_swot_test_pair(test_path, target_path)
    manifest = json.loads((root / "manifest.json").read_text())
    assert manifest["accepted_rows"] == 2
    assert len(manifest["sources"][0]["quality_filter_steps"]) == 13
    assert len(manifest["sources"][1]["quality_filter_steps"]) == 13
    assert manifest["sources"][0]["path"] == str(a)
    assert manifest["sources"][0]["curated_path"].endswith(
        "S1A_curated_coaligned_dataset_WV_20260930_swh_0.1.parquet"
    )
    tex_path, = list((root / "report").rglob("*.tex"))
    tex = tex_path.read_text()
    assert "S1 WV SWOT" in tex
    assert "never exceeds 100" not in tex
    assert "max\\_rainrate\\_IMERG" in tex
    assert str(recipe) not in tex
    assert str(a) not in tex
    assert str(tmp_path) not in tex
    assert a.name in tex
    assert Path(manifest["sources"][0]["curated_path"]).name in tex
    assert "--recipe recipe.toml" in tex
    assert "fresh output directory" in tex
    assert tex.count("max\\_rainrate\\_IMERG") == 2  # shared rule plus S1C override
    assert "Common quality filters" in tex
    assert "S1C-specific quality filters" in tex
    assert "S1A-specific quality filters" in tex
    assert "Remaining / removed" in tex
    assert len(list((root / "report").rglob("images_*/*.png"))) == 3


def test_recipe_accepts_swh_reference_date_gt_and_additive_columns(tmp_path):
    sources = {mission: source_file(tmp_path, mission) for mission in ("S1A", "S1C", "S1D")}
    frame = pd.read_parquet(sources["S1D"])
    frame["swh_swot_l3_20km"] = 2.1
    frame["polarization"] = "VV"
    frame.to_parquet(sources["S1D"], index=False)

    recipe = tmp_path / "recipe.toml"
    recipe.write_text(
        'reference = "swot"\nreference_variable = "swh"\n'
        'output_dir = "output"\nproduction_date = "20260930"\n'
        'version = "1.0"\ncompile = false\n'
        f'[[catalogues]]\nmission = "S1A"\npath = "{sources["S1A"].name}"\n'
        '[[catalogues.rules]]\nid = "sar_start"\nname = "S1A after cutoff"\n'
        'clauses = [["sar_time", "date_gt", "2024-11-25"]]\n'
        f'[[catalogues]]\nmission = "S1C"\npath = "{sources["S1C"].name}"\n'
        f'[[catalogues]]\nmission = "S1D"\npath = "{sources["S1D"].name}"\n'
        '[[catalogues.rules]]\nid = "sar_start"\nname = "S1D after cutoff"\n'
        'clauses = [["sar_time", "date_gt", "2025-06-03"]]\n',
        encoding="utf-8",
    )

    assert cli_main(["--recipe", str(recipe)]) == 0
    root = tmp_path / "output"
    merged = pq.read_table(root / "merged/S1_WV_swot_curated.parquet")
    assert merged.num_rows == 3
    assert "polarization" not in merged.column_names
    manifest = json.loads((root / "manifest.json").read_text())
    d_source = next(item for item in manifest["sources"] if item["mission"] == "S1D")
    assert d_source["curated_rows"] == 1
    d_curated = pq.read_table(d_source["curated_path"])
    assert "polarization" in d_curated.column_names
    sar_start = next(
        rule for rule in d_source["quality_filter_steps"] if rule["id"] == "sar_start"
    )
    assert sar_start["remaining"] == 1


def test_recipe_common_filters_are_not_repeated_by_mission(tmp_path):
    a = source_file(tmp_path, "S1A")
    c = source_file(tmp_path, "S1C")
    recipe = tmp_path / "recipe.toml"
    recipe.write_text(
        'reference = "swot"\noutput_dir = "output"\nproduction_date = "20260930"\n'
        'version = "0.1"\ncompile = false\n'
        f'[[catalogues]]\nmission = "S1A"\npath = "{a.name}"\n'
        f'[[catalogues]]\nmission = "S1C"\npath = "{c.name}"\n',
        encoding="utf-8",
    )
    assert cli_main(["--recipe", str(recipe)]) == 0
    tex_path, = list((tmp_path / "output/report").rglob("*.tex"))
    tex = tex_path.read_text()
    assert "Common quality filters" in tex
    assert "soba_create_test_dataset --recipe recipe.toml" in tex
    assert "--swot-catalogue" not in tex
    assert "-specific quality filters" not in tex
    assert tex.count("max\\_rainrate\\_IMERG") == 1
    assert "Remaining / removed" in tex
    assert str(tmp_path) not in tex


def test_recipe_rejects_unsafe_tex_filename_before_writing(tmp_path):
    source = source_file(tmp_path, "S1A")
    unsafe = source.with_name(source.stem + "|evil.parquet")
    source.rename(unsafe)
    recipe = tmp_path / "recipe.toml"
    recipe.write_text(
        f'reference = "swot"\noutput_dir = "output"\nproduction_date = "20260930"\n'
        f'version = "0.1"\n[[catalogues]]\nmission = "S1A"\npath = "{unsafe.name}"\n'
    )
    with pytest.raises(ValueError, match="unsafe"):
        cli_main(["--recipe", str(recipe)])
    assert not (tmp_path / "output").exists()


def test_recipe_rejects_unsafe_source_directory_before_writing(tmp_path):
    unsafe_dir = tmp_path / "un|safe"
    unsafe_dir.mkdir()
    source = source_file(unsafe_dir, "S1A")
    recipe = tmp_path / "recipe.toml"
    recipe.write_text(
        f'reference = "swot"\noutput_dir = "output"\nproduction_date = "20260930"\n'
        'version = "0.1"\n[[catalogues]]\nmission = "S1A"\n'
        f'path = "{source.relative_to(tmp_path)}"\n'
    )
    with pytest.raises(ValueError, match="unsafe"):
        cli_main(["--recipe", str(recipe)])
    assert not (tmp_path / "output").exists()


def test_failed_pair_retains_curated_provenance(tmp_path, monkeypatch):
    source = source_file(tmp_path, "S1A")
    recipe = tmp_path / "recipe.toml"
    recipe.write_text(
        f'reference = "swot"\noutput_dir = "output"\nproduction_date = "20260930"\n'
        f'version = "0.1"\n[[catalogues]]\nmission = "S1A"\npath = "{source.name}"\n'
    )
    def fail_pair(*args):
        raise ValueError("forced pair failure")
    monkeypatch.setattr(swot_test, "build_swot_test_from_merged", fail_pair)
    with pytest.raises(ValueError, match="forced pair failure"):
        cli_main(["--recipe", str(recipe)])
    root = tmp_path / "output"
    manifest = json.loads((root / "manifest.json").read_text())
    assert manifest["status"] == "failed"
    assert manifest["sources"][0]["path"] == str(source)
    assert manifest["sources"][0]["curated_rows"] == 1
    assert manifest["sources"][0]["steps"][-1]["id"] == "accepted_classes"
    assert manifest["curated_merge"] == str(root / "merged/S1_WV_swot_curated.parquet")
    assert (root / "merged/S1_WV_swot_curated.parquet").is_file()


def test_report_does_not_claim_disabled_overlap_or_time_threshold(tmp_path):
    source = source_file(tmp_path, "S1A", overlap_pct=80, ref_time_delta=8000)
    recipe = tmp_path / "recipe.toml"
    recipe.write_text(
        f'reference = "swot"\noutput_dir = "output"\nproduction_date = "20260930"\n'
        f'version = "0.1"\n[[catalogues]]\nmission = "S1A"\npath = "{source.name}"\n'
        '[[catalogues.rules]]\nid = "full_overlap"\nenabled = false\n'
        '[[catalogues.rules]]\nid = "time_under_two_hours"\nenabled = false\n'
    )
    assert cli_main(["--recipe", str(recipe)]) == 0
    tex_path, = list((tmp_path / "output/report").rglob("*.tex"))
    tex = tex_path.read_text()
    assert "full footprint overlap" not in tex
    assert "no additional distance cutoff" not in tex
    assert "time co-location criterion" not in tex.lower()
    assert "full overlap" in tex
    assert "disabled" in tex


def test_recipe_key_matches_exported_float32_coordinates_at_rounding_boundary(tmp_path):
    source = source_file(tmp_path, "S1A", ref_lon=1.05)
    recipe = tmp_path / "recipe.toml"
    recipe.write_text(
        f'reference = "swot"\noutput_dir = "output"\nproduction_date = "20260930"\n'
        f'version = "0.1"\n[[catalogues]]\nmission = "S1A"\npath = "{source.name}"\n'
    )
    assert cli_main(["--recipe", str(recipe)]) == 0
    test_path, = list((tmp_path / "output/datasets").rglob("S1_reference_test_dataset_*.parquet"))
    target_path, = list((tmp_path / "output/datasets").rglob("S1_target_dataset_*.parquet"))
    assert swot_test.validate_swot_test_pair(test_path, target_path)


def test_recipe_rejects_safe_from_another_mission(tmp_path):
    path = tmp_path / "S1C_coaligned_catalogue_WV_sample.parquet"
    pd.DataFrame([source_row()]).to_parquet(path, index=False)
    recipe = tmp_path / "recipe.toml"
    recipe.write_text(
        f'reference = "swot"\noutput_dir = "output"\nproduction_date = "20260930"\n'
        f'version = "0.1"\n[[catalogues]]\nmission = "S1C"\npath = "{path.name}"\n'
    )
    with pytest.raises(ValueError, match="SAFE mission mismatch"):
        cli_main(["--recipe", str(recipe)])
    manifest = json.loads((tmp_path / "output/manifest.json").read_text())
    assert manifest["status"] == "failed"


def test_recipe_excludes_missing_safe_placeholder_without_mission_error(tmp_path):
    source = source_file(tmp_path, "S1A")
    frame = pd.read_parquet(source)
    missing = frame.iloc[0].copy()
    missing["sar_safe_slc"] = "nan"
    pd.concat([frame, pd.DataFrame([missing])], ignore_index=True).to_parquet(source, index=False)
    recipe = tmp_path / "recipe.toml"
    recipe.write_text(
        f'reference = "swot"\noutput_dir = "output"\nproduction_date = "20260930"\n'
        f'version = "0.1"\n[[catalogues]]\nmission = "S1A"\npath = "{source.name}"\n'
    )
    assert cli_main(["--recipe", str(recipe)]) == 0
    manifest = json.loads((tmp_path / "output/manifest.json").read_text())
    assert manifest["excluded_rows"]["missing_key_fields"] == 1


def test_recipe_rejects_unimplemented_reference_before_writing(tmp_path):
    recipe = tmp_path / "recipe.toml"
    recipe.write_text('reference = "alti"\noutput_dir = "output"\n'
                      'production_date = "20260930"\nversion = "0.1"\n'
                      '[[catalogues]]\nmission = "S1A"\npath = "missing.parquet"\n')
    with pytest.raises(ValueError, match="unsupported reference.*alti"):
        cli_main(["--recipe", str(recipe)])
    assert not (tmp_path / "output").exists()


def test_recipe_refuses_existing_output_directory(tmp_path):
    source = source_file(tmp_path, "S1A")
    output = tmp_path / "output"
    output.mkdir()
    (output / "keep.txt").write_text("untouched")
    recipe = tmp_path / "recipe.toml"
    recipe.write_text(
        f'reference = "swot"\noutput_dir = "output"\nproduction_date = "20260930"\n'
        f'version = "0.1"\n[[catalogues]]\nmission = "S1A"\npath = "{source.name}"\n'
    )
    with pytest.raises(FileExistsError):
        cli_main(["--recipe", str(recipe)])
    assert sorted(p.name for p in output.iterdir()) == ["keep.txt"]
