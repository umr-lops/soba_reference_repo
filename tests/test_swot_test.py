from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pyarrow.parquet as pq
import pytest
from matplotlib import pyplot as plt
from matplotlib.colors import to_hex

from soba_reference_repo import swot_test
from soba_reference_repo.create_test_dataset import main as cli_main
from soba_reference_repo.validator import main as validator_main
from soba_reference_repo.swot_test import (
    build_swot_test_frames,
    compile_swot_pdf,
    read_swot_catalogue,
    summarize_swot_test,
    plot_swot_figures,
    write_swot_test_pair,
    validate_swot_test_pair,
)


MISSIONS = ("S1A", "S1C", "S1D")


def catalogue_path(mission):
    return (
        f"/input/{mission}_coaligned_catalogue_WV_20260107_20260109_20260925_"
        "SV_PODAAC-SWOT-KARIN-L2-WINDWAVE-D0_0.1.parquet"
    )


def source_row(**updates):
    row = {
        "sar_time": pd.Timestamp("2026-01-07 11:33:34", tz="UTC"),
        "sar_lat": -74.31,
        "sar_lon": -135.38,
        "sar_incidence_angle": 22.9,
        "sar_elevation_angle": 20.4,
        "sar_distance_to_coast": 1238.0,
        "sar_safe_slc": "/archive/S1A_WV_SLC__1SSV_20260107.SAFE:WV_033",
        "sar_safe_ocn": "/archive/S1A_WV_OCN__2SSV_20260107.SAFE:WV_033",
        "ref_lon": -135.4,
        "ref_lat": -74.3,
        "ref_time": pd.Timestamp("2026-01-07 11:43:34", tz="UTC"),
        "ref_mean_hs_karin": 1.9,
        "swot_path": "/archive/swot_20260107.nc",
        "oswLandCoverage": 0.0,
        "swot_dynamic_ice_flag": 0.0,
        "ref_time_delta": 600.0,
        "oswTotalHs": 2.5,
        "ref_hs_alti_closest": 2.0,
        "ww3_hs": 2.4,
        "prob_1": 0.8,
        "overlap_pct": 100.0,
        "max_rainrate_IMERG": 0.0,
        "swot_rain_flag": 0.0,
        "class_1": "WS",
    }
    row.update(updates)
    return row


def test_read_swot_catalogue_maps_source_fields(tmp_path):
    path = tmp_path / Path(catalogue_path("S1A")).name
    pd.DataFrame([source_row()]).to_parquet(path, index=False)

    frame = read_swot_catalogue(path)

    assert frame.loc[0, "sar_safe_slc"] == "S1A_WV_SLC__1SSV_20260107.SAFE:WV_033"
    assert frame.loc[0, "sar_safe_ocn"] == "S1A_WV_OCN__2SSV_20260107.SAFE:WV_033"
    assert frame.loc[0, "swot_lon"] == -135.4
    assert frame.loc[0, "swot_lat"] == -74.3
    assert frame.loc[0, "swot_time"] == pd.Timestamp("2026-01-07 11:43:34", tz="UTC")
    assert frame.loc[0, "swot_waveheight"] == 1.9
    assert frame.loc[0, "swot_source"] == "/archive/swot_20260107.nc"
    assert pd.isna(frame.loc[0, "sar_ground_heading"])


def test_read_swot_catalogue_reports_missing_columns(tmp_path):
    path = tmp_path / Path(catalogue_path("S1A")).name
    pd.DataFrame([{"sar_time": pd.Timestamp("2026-01-07")}]).to_parquet(path, index=False)

    with pytest.raises(ValueError, match=r"S1A.*missing required source columns.*sar_safe_slc"):
        read_swot_catalogue(path)


def write_catalogue(tmp_path, mission, rows):
    path = tmp_path / Path(catalogue_path(mission)).name
    pd.DataFrame(rows).to_parquet(path, index=False)
    return path


def test_nectar_filters_apply_all_thirteen_rules_in_order(tmp_path):
    invalid = [
        {"oswLandCoverage": 1},
        {"sar_distance_to_coast": 0},
        {"swot_dynamic_ice_flag": 1},
        {"ref_time_delta": None},
        {"ref_mean_hs_karin": 0},
        {"oswTotalHs": 0},
        {"ref_hs_alti_closest": 0},
        {"ww3_hs": 0},
        {"prob_1": -1},
        {"ref_time_delta": 7200},
        {"overlap_pct": 99},
        {"max_rainrate_IMERG": 0.1},
        {"swot_rain_flag": 1},
        {"class_1": "SI"},
    ]
    path = write_catalogue(tmp_path, "S1A", [source_row(), *(source_row(**x) for x in invalid)])
    retained, steps = swot_test.apply_quality_filters(read_swot_catalogue(path))
    assert len(retained) == 1
    assert len(steps) == 13
    assert [step["removed"] for step in steps] == [2] + [1] * 12
    assert [step["remaining"] for step in steps] == list(range(13, 0, -1))
    assert "overlap_pct >= 100" in steps[9]["rule"]


def test_filter_cascade_excludes_unclassified_s1d_without_relaxing_rule(tmp_path):
    paths = [
        write_catalogue(
            tmp_path,
            mission,
            [
                source_row(
                    sar_safe_slc=f"/archive/{mission}_WV_SLC__1SSV_20260107.SAFE:WV_033",
                    **({"prob_1": -1, "class_1": " "} if mission == "S1D" else {}),
                )
            ],
        )
        for mission in MISSIONS
    ]
    test, target, manifest = build_swot_test_frames(paths)
    assert len(test) == len(target) == 2
    assert manifest["quality_filter_steps"][-1]["remaining"] == 2
    assert manifest["sources"][-1]["accepted_rows"] == 0
    assert manifest["sources"][-1]["quality_filter_steps"][7]["removed"] == 1


def test_build_frames_deduplicates_and_keeps_test_target_keys_aligned(tmp_path):
    first = source_row(
        sar_safe_slc="/archive/S1A_WV_SLC__1SSV_20260107.SAFE:WV_033",
        ref_time=pd.Timestamp("2026-01-07 11:39:34", tz="UTC"),
        ref_mean_hs_karin=1.1,
    )
    closest = source_row(
        sar_safe_slc="/archive/S1A_WV_SLC__1SSV_20260107.SAFE:WV_033",
        ref_time=pd.Timestamp("2026-01-07 11:34:34", tz="UTC"),
        ref_mean_hs_karin=2.2,
    )
    missing_key = source_row(sar_safe_slc=None)
    paths = [
        write_catalogue(tmp_path, "S1A", [first, closest, missing_key]),
        write_catalogue(
            tmp_path,
            "S1C",
            [
                source_row(
                    sar_safe_slc="/archive/S1C_WV_SLC__1SSV_20260107.SAFE:WV_033",
                )
            ],
        ),
        write_catalogue(
            tmp_path,
            "S1D",
            [
                source_row(
                    sar_safe_slc="/archive/S1D_WV_SLC__1SSV_20260107.SAFE:WV_033",
                )
            ],
        ),
    ]

    test, target, manifest = build_swot_test_frames(paths)

    assert test.columns.tolist() == [
        "primary_key",
        "sar_time",
        "sar_lat",
        "sar_lon",
        "sar_incidence_angle",
        "sar_elevation_angle",
        "sar_ground_heading",
        "sar_distance_to_coast",
        "sar_safe_slc",
        "sar_safe_ocn",
        "swot_lon",
        "swot_lat",
        "swot_waveheight",
        "swot_time",
        "swot_source",
    ]
    assert target.columns.tolist() == [
        "primary_key",
        "sar_safe_slc",
        "sar_safe_ocn",
        "swot_lon",
        "swot_lat",
        "swot_time",
    ]
    assert target.primary_key.tolist() == test.primary_key.tolist()
    assert test.primary_key.is_unique
    assert test.loc[0, "primary_key"] == ("S1A_WV_SLC__1SSV_20260107.SAFE:WV_033_-135.4_-74.3")
    assert test.loc[0, "swot_waveheight"] == 2.2
    assert test["sar_ground_heading"].isna().all()
    assert manifest["input_rows"] == 5
    assert manifest["accepted_rows"] == 3
    assert manifest["excluded_rows"]["missing_key_fields"] == 1
    assert manifest["excluded_rows"]["duplicate_keys"] == 1
    source_a = next(source for source in manifest["sources"] if source["mission"] == "S1A")
    assert source_a["excluded_rows"] == {
        "missing_key_fields": 1,
        "missing_required_fields": 0,
        "duplicate_keys": 1,
    }


def test_build_frames_rejects_cross_mission_key_collision(tmp_path):
    row = source_row()
    paths = [
        write_catalogue(tmp_path, "S1A", [row]),
        write_catalogue(tmp_path, "S1C", [row]),
        write_catalogue(
            tmp_path,
            "S1D",
            [
                source_row(
                    sar_safe_slc="/archive/S1D_WV_SLC__1SSV_20260107.SAFE:WV_033",
                )
            ],
        ),
    ]

    with pytest.raises(ValueError, match="primary_key collision across missions"):
        build_swot_test_frames(paths)


def test_write_pair_has_exact_filenames_schema_metadata_and_keys(tmp_path):
    paths = [
        write_catalogue(
            tmp_path,
            mission,
            [
                source_row(
                    sar_safe_slc=f"/archive/{mission}_WV_SLC__1SSV_20260107.SAFE:WV_033",
                )
            ],
        )
        for mission in MISSIONS
    ]
    test, target, _ = build_swot_test_frames(paths)

    test_path, target_path = write_swot_test_pair(
        test, target, tmp_path / "datasets", "20260110", "0.1"
    )

    assert test_path.name == "S1_reference_test_dataset_WV_20260110_swh_0.1.parquet"
    assert target_path.name == "S1_target_dataset_WV_20260110_swh_0.1.parquet"
    assert test_path.parent == target_path.parent
    assert {path.name for path in test_path.parent.iterdir()} == {test_path.name, target_path.name}
    assert validate_swot_test_pair(test_path, target_path) is True
    assert validator_main(["--type", "test", "--file", str(test_path)]) == 0
    broken = pq.read_table(test_path).drop(["swot_waveheight"])
    pq.write_table(broken, test_path)
    with pytest.raises(SystemExit) as error:
        validator_main(["--type", "test", "--file", str(test_path)])
    assert error.value.code == 1
    metadata = pq.read_schema(target_path).metadata
    assert set(metadata) == {
        b"source swot",
        b"source ancillary datasets",
        b"library used to produce the parquet",
        b"library version",
        b"creation date",
    }
    assert metadata[b"source swot"] == b"PODAAC SWOT Karin L2 WindWave PGD0 and PID0 (D0)"
    assert metadata[b"creation date"] == b"20260110"


def test_summary_reports_dataset_and_exclusion_statistics(tmp_path):
    paths = [
        write_catalogue(
            tmp_path,
            mission,
            [
                source_row(
                    sar_safe_slc=f"/archive/{mission}_WV_SLC__1SSV_20260107.SAFE:WV_033",
                    ref_mean_hs_karin=float(index + 1),
                )
            ],
        )
        for index, mission in enumerate(MISSIONS)
    ]
    test, _, manifest = build_swot_test_frames(paths)

    summary = summarize_swot_test(test, manifest)

    assert summary["row_count"] == 3
    assert summary["mission_counts"] == {"S1A": 1, "S1C": 1, "S1D": 1}
    assert summary["swh_count"] == 3
    assert summary["swh_mean"] == 2.0
    assert summary["swh_median"] == 2.0
    assert summary["swh_min"] == 1.0
    assert summary["swh_max"] == 3.0
    assert summary["missing_heading_count"] == 3
    assert summary["excluded_rows"]["missing_key_fields"] == 0


def test_summary_handles_empty_frame():
    summary = summarize_swot_test(
        pd.DataFrame(columns=["swot_waveheight", "sar_ground_heading"]), {}
    )

    assert summary["row_count"] == 0
    assert summary["swh_count"] == 0
    assert summary["swh_mean"] is None
    assert summary["mission_counts"] == {}


def test_plot_swot_figures_writes_three_pngs(tmp_path):
    paths = [
        write_catalogue(
            tmp_path,
            mission,
            [
                source_row(
                    sar_safe_slc=f"/archive/{mission}_WV_SLC__1SSV_20260107.SAFE:WV_033",
                )
            ],
        )
        for mission in MISSIONS
    ]
    test, _, _ = build_swot_test_frames(paths)

    figures = plot_swot_figures(test, tmp_path / "images")

    assert set(figures) == {"coverage", "monthly_rows", "swot_waveheight"}
    assert all(
        path.is_file() and path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
        for path in figures.values()
    )


def test_figures_include_every_row_with_distinct_mission_colors(tmp_path, monkeypatch):
    missions = ("S1A", "S1B", "S1C", "S1D")
    test = pd.DataFrame(
        [
            {
                "sar_safe_slc": f"{mission}_WV_SLC__1SSV_{month:02d}.SAFE",
                "sar_lon": float(index),
                "sar_lat": float(month),
                "sar_time": pd.Timestamp(2026, month, 7),
                "swot_waveheight": float(index + month),
            }
            for index, mission in enumerate(missions)
            for month in (1, 2)
        ]
    )
    figures = []
    original_close = plt.close
    monkeypatch.setattr(plt, "close", lambda figure: figures.append(figure))
    try:
        paths = plot_swot_figures(test, tmp_path)
        assert all(path.is_file() for path in paths.values())
        assert len(figures) == 3
        coverage = figures[0].axes[0]
        assert coverage.get_legend() is None
        assert len(coverage.collections) == 1
        assert len(coverage.collections[0].get_offsets()) == len(test)
        monthly = figures[1].axes[0]
        distribution_figure = figures[2]
        distribution = distribution_figure.axes[0]
        monthly_labels = [text.get_text() for text in monthly.get_legend().get_texts()]
        distribution_labels = [text.get_text() for text in distribution.get_legend().get_texts()]
        assert monthly_labels == list(missions)
        assert distribution_labels == [f"{mission} (n=2)" for mission in missions]
        colors = [
            to_hex(handle.get_facecolor())
            for handle in monthly.get_legend().legend_handles
        ]
        assert len(set(colors)) == len(missions)
        distribution_colors = [
            to_hex(handle.get_color())
            for handle in distribution.get_legend().legend_handles
        ]
        assert distribution_colors == colors
        assert distribution.get_ylabel() == "Mission TEST rows (%)"
        assert distribution_figure.get_size_inches()[1] == 5.0
        assert sum(patch.get_height() for patch in monthly.patches) == len(test)
    finally:
        for figure in figures:
            original_close(figure)


def test_figures_omit_missions_without_data(tmp_path, monkeypatch):
    test = pd.DataFrame(
        {
            "sar_safe_slc": ["S1A_WV.SAFE", "S1C_WV.SAFE", "S1D_WV.SAFE"],
            "sar_lon": [0.0, 1.0, 2.0],
            "sar_lat": [0.0, 1.0, 2.0],
            "sar_time": pd.to_datetime(["2026-01-07"] * 3),
            "swot_waveheight": [1.0, 2.0, 3.0],
        }
    )
    figures = []
    original_close = plt.close
    monkeypatch.setattr(plt, "close", lambda figure: figures.append(figure))
    try:
        plot_swot_figures(test, tmp_path)
        assert figures[0].axes[0].get_legend() is None
        for figure, labels in zip(figures[1:], (
            ["S1A", "S1C", "S1D"], ["S1A (n=1)", "S1C (n=1)", "S1D (n=1)"]
        ), strict=True):
            actual = [text.get_text() for text in figure.axes[0].get_legend().get_texts()]
            assert actual == labels
    finally:
        for figure in figures:
            original_close(figure)


def test_cli_rejects_direct_catalogue_and_legacy_crossing_commands(capsys):
    for args in (["swot-test", "--swot-catalogue", "x.parquet"],
                 ["--scat", "scat.parquet", "--swot", "swot.parquet"]):
        with pytest.raises(SystemExit) as error:
            cli_main(args)
        assert error.value.code == 2
    assert "--recipe" in capsys.readouterr().err


def test_compile_swot_pdf_falls_back_to_tectonic(tmp_path, monkeypatch):
    (tmp_path / "minimal.tex").write_text(
        r"\documentclass{article}\begin{document}SWOT report\end{document}",
        encoding="utf-8",
    )

    def missing_pdflatex(*args):
        raise FileNotFoundError("pdflatex is not installed")

    commands = []

    def run_tectonic(command, **options):
        commands.append(command)
        assert options["cwd"] == tmp_path
        (tmp_path / "minimal.pdf").write_bytes(b"%PDF-1.4\n")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(swot_test, "compile_pdf", missing_pdflatex)
    monkeypatch.setattr(swot_test.shutil, "which", lambda name: "/bin/tectonic")
    monkeypatch.setattr(swot_test.subprocess, "run", run_tectonic)

    pdf = compile_swot_pdf(tmp_path, "minimal")

    assert commands == [[
        "/bin/tectonic", "--keep-intermediates", "--keep-logs", "--reruns", "1",
        "minimal.tex",
    ]]
    assert pdf == tmp_path / "minimal.pdf"
    assert pdf.read_bytes().startswith(b"%PDF-")
