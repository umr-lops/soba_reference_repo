"""Tests for the SOBA catalogue-report tool."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pandas as pd
import pyarrow.parquet as pq
import pytest

from soba_catalogue_report.report import (
    DEFAULT_LAND_MAP,
    DEFAULT_LATEX_DIR,
    ReportConfig,
    WV_MANDATORY_COLUMNS,
    WV_REF_PARAM_COLUMNS,
    build_report_tex,
    build_test_frame,
    make_scene_key,
    plot_figures,
    run_crossing,
    stage_latex_assets,
    write_test_parquet,
)

CORE = "S1D_WV_SLC__1S/2026/007/S1D_WV_SLC__1SSV_20260107T111713_20260107T114520_000927_0006BD"
SCAT_REF_TIME = pd.Timestamp("2026-01-07 11:54:46", tz="UTC")


def _write_pair(tmp_path, *, overlap=100.0, rain=0.0, time_delta_min=10.0):
    """Write a one-row SCAT/SWOT pair with a controllable collocation quality."""
    scat_rows = [{
        "sar_safe_ocn": None, "sar_safe_slc": f"{CORE}_35F2.SAFE:WV_033",
        "sar_time": pd.Timestamp("2026-01-07 11:38:27"),
        "sar_lat": -74.31, "sar_lon": -135.38,
        "sar_incidence_angle": 22.9, "sar_elevation_angle": 20.4, "sar_ground_heading": -128.2,
        "sar_path_ocn": "/archive/ocn.nc", "sar_path_slc": "/archive/slc.tiff",
        "ref_time": SCAT_REF_TIME, "ref_lat": -74.27, "ref_lon": -135.41,
        "ref_param_1": 17.8, "ref_param_2": 9.8, "ref_distance_km": 2.7, "ref_flag": 0,
        "ref_id": "ascat_20260107_103900_metopc_37200.l2.nc",
    }]
    swot_rows = [{
        "sar_safe_ocn": None, "sar_safe_slc": f"{CORE}_D27C.SAFE:WV_033",
        "sar_time": pd.Timestamp("2026-01-07 11:33:34+00:00"),
        "sar_lat": -74.31, "sar_lon": -135.38,
        "sar_incidence_angle": 22.9, "sar_elevation_angle": 20.4,
        "sar_distance_to_coast": 1238.0,
        "sar_path_ocn": "/archive/ocn.nc", "sar_path_slc": "/archive/slc.tiff",
        "ref_time": SCAT_REF_TIME + pd.Timedelta(minutes=time_delta_min),
        "ref_lat": -74.28, "ref_lon": -135.40, "ref_mean_hs_karin": 1.90,
        "ref_time_delta": 600.0, "ref_distance_delta": 3.0,
        "overlap_pct": overlap, "mean_rainrate_IMERG": rain,
        "swot_dynamic_ice_flag": 0.0, "swot_rain_flag": 0.0, "ref_flag": "clear",
        "swot_cycle": 1.0, "swot_pass": 1.0, "legacy_usage": None,
    }]
    scat_path = tmp_path / "scat.parquet"
    swot_path = tmp_path / "swot.parquet"
    pd.DataFrame(scat_rows).to_parquet(scat_path, index=False)
    pd.DataFrame(swot_rows).to_parquet(swot_path, index=False)
    return scat_path, swot_path


def _result(tmp_path, **kwargs):
    scat_path, swot_path = _write_pair(tmp_path, **kwargs)
    return run_crossing(scat_path, swot_path, "S1D", ReportConfig())


# --- scene key ---------------------------------------------------------------

def test_scene_key_falls_back_to_slc_when_ocn_is_missing():
    frame = pd.DataFrame({
        "sar_safe_ocn": [
            "S1D_WV_OCN__2S/2026/007/S1D_WV_OCN__2SSV_20260107T111713_20260107T114520_000927_0006BD_5A74.SAFE:WV_033",
            None,
        ],
        "sar_safe_slc": [
            "S1D_WV_SLC__1S/2026/007/S1D_WV_SLC__1SSV_20260107T111713_20260107T114520_000927_0006BD_35F2.SAFE:WV_033",
            "S1D_WV_SLC__1S/2026/109/S1D_WV_SLC__1SSV_20260419T155600_20260419T160436_002417_003F71_2FA6.SAFE:WV_001",
        ],
    })

    keys = make_scene_key(frame, "S1D")

    assert keys.tolist() == [
        "20260107T111713_20260107T114520_000927_0006BD:WV_033",
        "20260419T155600_20260419T160436_002417_003F71:WV_001",
    ]


def test_scene_key_rejects_a_foreign_mission():
    frame = pd.DataFrame({
        "sar_safe_ocn": ["S1C_WV_OCN__2SSV_20260107T111713_20260107T114520_000927_0006BD_5A74.SAFE:WV_033"],
        "sar_safe_slc": [None],
    })

    with pytest.raises(ValueError, match="S1D"):
        make_scene_key(frame, "S1D")


# --- crossing and filters ----------------------------------------------------

def test_run_crossing_keeps_a_row_that_passes_every_filter(tmp_path):
    result = _result(tmp_path)

    assert len(result.crossing) == 1
    assert len(result.filtered) == 1
    assert result.crossing["scene_key"].iloc[0].endswith(":WV_033")
    assert result.scat_rows == 1 and result.swot_rows == 1


def test_run_crossing_drops_a_row_that_fails_one_filter(tmp_path):
    for kwargs, failed in (
        ({"overlap": 99.8}, "overlap"),
        ({"rain": 0.4}, "rain"),
        ({"time_delta_min": 180.0}, "time"),
    ):
        assert len(_result(tmp_path, **kwargs).filtered) == 0, failed


def test_run_crossing_rejects_a_catalogue_missing_a_column(tmp_path):
    scat_path, swot_path = _write_pair(tmp_path)
    pd.read_parquet(swot_path).drop(columns=["legacy_usage"]).to_parquet(swot_path, index=False)

    with pytest.raises(ValueError, match="legacy_usage"):
        run_crossing(scat_path, swot_path, "S1D", ReportConfig())


# --- figures -----------------------------------------------------------------

def test_plot_figures_writes_three_non_empty_pngs(tmp_path):
    paths = plot_figures(_result(tmp_path), tmp_path / "figures")

    assert [p.name for p in paths] == [
        "geographical_distribution.png",
        "monthly_distribution.png",
        "reference_distributions.png",
    ]
    for path in paths:
        assert path.is_file() and path.stat().st_size > 5_000


def test_plot_figures_survives_an_empty_cohort(tmp_path):
    paths = plot_figures(_result(tmp_path, time_delta_min=600.0), tmp_path / "figures")

    assert all(p.is_file() for p in paths)


def test_the_bundled_land_map_exists():
    assert DEFAULT_LAND_MAP.is_file(), "assets/ne_110m_land.geojson must ship with the project"


# --- TEST parquet ------------------------------------------------------------

def test_build_test_frame_has_every_mandatory_wv_column(tmp_path):
    frame = build_test_frame(_result(tmp_path))

    assert set(WV_MANDATORY_COLUMNS) <= set(frame.columns)
    assert set(WV_REF_PARAM_COLUMNS) <= set(frame.columns)
    assert {"sar-path-ocn", "sar-path-slc", "sar-safe-slc", "sar-safe-ocn"} <= set(frame.columns)


def test_build_test_frame_respects_the_value_conventions(tmp_path):
    frame = build_test_frame(_result(tmp_path))

    assert frame["primary_key"].is_unique
    assert frame["primary_key"].str.match(r".*:WV_\d+$").all()
    assert frame["sar_time"].str.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$").all()
    assert frame["ref_time"].str.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$").all()
    for column in ("sar_lon", "ref_lon", "swot_lon"):
        assert frame[column].between(-180, 180).all()


def test_write_test_parquet_records_the_global_attributes(tmp_path):
    frame = build_test_frame(_result(tmp_path))
    target = tmp_path / "s1d_swot_ascat_test.parquet"

    write_test_parquet(frame, target)

    metadata = pq.ParquetFile(target).schema_arrow.metadata or {}
    assert b"source_ref" in metadata and b"creation_date" in metadata
    assert len(pd.read_parquet(target)) == len(frame)


# --- LaTeX -------------------------------------------------------------------

def test_stage_latex_assets_copies_everything_the_template_needs(tmp_path):
    build = tmp_path / "build"

    staged = stage_latex_assets(DEFAULT_LATEX_DIR, build)

    for name in ("soba.sty", "logo_soba.png", "schema_dataflow.tex", "cpcd_definition.tex"):
        assert (build / name).is_file(), name
    assert len(staged) == 4
    assert (build / "logo_soba.png").stat().st_size > 100_000


def _fill(tmp_path, template=None):
    return build_report_tex(
        template or (DEFAULT_LATEX_DIR / "template.tex"),
        _result(tmp_path),
        label="s1d_swot_ascat",
        figure_names=[
            "geographical_distribution.png",
            "monthly_distribution.png",
            "reference_distributions.png",
        ],
        scat_name="scat.parquet",
        swot_name="swot.parquet",
    )


def test_build_report_tex_replaces_every_placeholder(tmp_path):
    tex = _fill(tmp_path)

    assert r"\hl{" not in tex, "an unfilled placeholder survived"
    assert "s1d_swot_ascat" in tex
    assert "scat.parquet" in tex and "swot.parquet" in tex
    for name in ("geographical_distribution.png", "monthly_distribution.png",
                 "reference_distributions.png"):
        assert name in tex
    assert "120" in tex  # the time filter value must be stated
    assert "soba-catalogue-report" in tex  # the run command replaces the template stub


def test_build_report_tex_fails_loudly_when_an_anchor_is_missing(tmp_path):
    broken = tmp_path / "broken.tex"
    broken.write_text(
        (DEFAULT_LATEX_DIR / "template.tex").read_text().replace(r"\hl{dataset name}", "gone")
    )

    with pytest.raises(ValueError, match="anchor"):
        _fill(tmp_path, template=broken)


def test_build_report_tex_escapes_latex_specials(tmp_path):
    # underscores are everywhere (SAFE names, labels, catalogue filenames) and
    # ``_`` switches TeX into math mode, which aborts the compile.
    tex = build_report_tex(
        DEFAULT_LATEX_DIR / "template.tex",
        _result(tmp_path),
        label="s1d_swot_ascat",
        figure_names=["a.png", "b.png", "c.png"],
        scat_name="S1D_coaligned_catalogue.parquet",
        swot_name="S1D_swot_catalogue.parquet",
    )

    assert r"\path|S1D_coaligned_catalogue.parquet|" in tex
    assert r"s1d\_swot\_ascat" in tex


# --- CLI ---------------------------------------------------------------------

def test_cli_runs_end_to_end_without_compiling(tmp_path):
    scat_path, swot_path = _write_pair(tmp_path)
    output_dir = tmp_path / "report"

    completed = subprocess.run(
        [sys.executable, "-m", "soba_catalogue_report.cli",
         "--scat", str(scat_path), "--swot", str(swot_path),
         "--label", "unit", "--no-compile", "--output-dir", str(output_dir)],
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "PYTHONPATH": "src"},
        capture_output=True, text=True, check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert (output_dir / "unit_catalogue_report.tex").is_file()
    assert (output_dir / "manifest.json").is_file()
    assert (output_dir / "unit_test.parquet").is_file()
    assert len(list((output_dir / "figures").glob("*.png"))) == 3
