"""Tests for the SOBA catalogue-report tool."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq
import pytest

from soba_reference_repo.report import (
    ANCILLARY_COLUMNS,
    DEFAULT_LAND_MAP,
    DEFAULT_LATEX_DIR,
    LATEX_AUXILIARY_FILES,
    WV_MANDATORY_COLUMNS,
    WV_REF_PARAM_COLUMNS,
    ReportConfig,
    build_report_tex,
    build_test_filename,
    build_test_frame,
    default_test_name,
    find_pdflatex,
    insert_table_row,
    insert_version_history_row,
    make_scene_key,
    plot_figures,
    purge_latex_byproducts,
    reference_statistics_table,
    run_crossing,
    section_bounds,
    stage_latex_assets,
    write_test_parquet,
)
from soba_reference_repo.validator import SOBAParquetValidator, SOBAValidationRules

CORE = "S1D_WV_SLC__1S/2026/007/S1D_WV_SLC__1SSV_20260107T111713_20260107T114520_000927_0006BD"
OCN_CORE = "S1D_WV_OCN__2S/2026/007/S1D_WV_OCN__2SSV_20260107T111713_20260107T114520_000927_0006BD"
SCAT_REF_TIME = pd.Timestamp("2026-01-07 11:54:46", tz="UTC")
SOURCE_SCAT_NAME = (
    "S1D_coaligned_catalogue_WV_20260107_20260414_20260916_"
    "SV_KNMI-ASCAT-METOP-12.5km_0.2.parquet"
)
SOURCE_SWOT_NAME = (
    "S1D_coaligned_catalogue_WV_20260107_20260808_20260902_"
    "SV_PODAAC-SWOT-KARIN-L2-WINDWAVE-D0_0.1.parquet"
)
BOTH_PRODUCTS = "KNMI-ASCAT-METOP-12.5km_PODAAC-SWOT-KARIN-L2-WINDWAVE-D0"


def _write_pair(tmp_path, *, overlap=100.0, rain=0.0, time_delta_min=10.0,
                scat_filename="scat.parquet", swot_filename="swot.parquet", swot_primary_key=None):
    """Write a one-row SCAT/SWOT pair with a controllable collocation quality."""
    scat_rows = [{
        "primary_key": f"{CORE}_35F2.SAFE:WV_033_-135.4_-74.3",
        "sar_safe_ocn": f"{OCN_CORE}_5A74.SAFE:WV_033", "sar_safe_slc": f"{CORE}_35F2.SAFE:WV_033",
        "sar_time": pd.Timestamp("2026-01-07 11:38:27"),
        "sar_lat": -74.31, "sar_lon": -135.38,
        "sar_incidence_angle": 22.9, "sar_elevation_angle": 20.4, "sar_ground_heading": -128.2,
        "sar_path_ocn": "/archive/ocn.nc", "sar_path_slc": "/archive/slc.tiff",
        "ref_time": SCAT_REF_TIME, "ref_lat": -74.27, "ref_lon": -135.41,
        "ref_param_1": 17.8, "ref_param_2": 9.8, "ref_distance_km": 2.7, "ref_flag": 0,
        "ref_id": "ascat_20260107_103900_metopc_37200.l2.nc",
    }]
    swot_rows = [{
        # the two catalogues agree on primary_key for a shared imagette, the default match
        "primary_key": swot_primary_key or f"{CORE}_35F2.SAFE:WV_033_-135.4_-74.3",
        "sar_safe_ocn": f"{OCN_CORE}_6B12.SAFE:WV_033", "sar_safe_slc": f"{CORE}_D27C.SAFE:WV_033",
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
    scat_path = tmp_path / scat_filename
    swot_path = tmp_path / swot_filename
    pd.DataFrame(scat_rows).to_parquet(scat_path, index=False)
    pd.DataFrame(swot_rows).to_parquet(swot_path, index=False)
    return scat_path, swot_path


def _result(tmp_path, **kwargs):
    use_scene_key = kwargs.pop("use_scene_key", False)
    scat_path, swot_path = _write_pair(tmp_path, **kwargs)
    return run_crossing(
        scat_path, swot_path, "S1D", ReportConfig(), use_scene_key=use_scene_key
    )


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
        "sar_safe_ocn": [
            "S1C_WV_OCN__2SSV_20260107T111713_20260107T114520_000927_0006BD_5A74.SAFE:WV_033"
        ],
        "sar_safe_slc": [None],
    })

    with pytest.raises(ValueError, match="S1D"):
        make_scene_key(frame, "S1D")


# --- crossing and filters ----------------------------------------------------

def test_run_crossing_keeps_a_row_that_passes_every_filter(tmp_path):
    result = _result(tmp_path)

    assert len(result.crossing) == 1
    assert len(result.filtered) == 1
    assert result.crossing["match_key"].iloc[0].startswith(f"{CORE}_35F2.SAFE:WV_033")
    assert result.scat_rows == 1 and result.swot_rows == 1


def test_the_default_match_uses_the_catalogue_primary_key(tmp_path):
    result = _result(tmp_path)

    assert result.crossing["match_key"].iloc[0] == f"{CORE}_35F2.SAFE:WV_033_-135.4_-74.3"


def test_a_reference_disagreement_needs_the_scene_key(tmp_path):
    """Each catalogue keys a row on its own reference, so the two can disagree on it."""
    other = f"{CORE}_35F2.SAFE:WV_033_-140.0_-70.0"
    result = _result(tmp_path, swot_primary_key=other)

    assert len(result.crossing) == 0  # no shared primary_key
    bridged = _result(tmp_path, swot_primary_key=other, use_scene_key=True)
    assert len(bridged.crossing) == 1  # the imagette key still pairs them
    assert bridged.crossing["match_key"].iloc[0].endswith(":WV_033")


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


# --- reference-distribution sections -----------------------------------------

def test_section_bounds_uses_the_requested_reference_bins():
    inf = float("inf")

    # wind speed: 0-5, 5-10, 10-15, >15 m/s
    assert list(section_bounds("scat_wind_speed_ms", 0.0, 12.0)) == [-inf, 5.0, 10.0, 15.0, inf]
    # wave height: 0-1, 1-3, 3-5, 5-10, >10 m
    assert list(section_bounds("swot_wave_height_m", 0.0, 5.0)) == [
        -inf, 1.0, 3.0, 5.0, 10.0, inf]
    # wind direction has no fixed bins: equal thirds of the axis
    assert list(section_bounds("scat_wind_direction_deg", 0.0, 360.0)) == [-inf, 120.0, 240.0, inf]


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
    assert {"sar_path_ocn", "sar_path_slc", "sar_safe_slc", "sar_safe_ocn"} <= set(frame.columns)


def test_build_test_frame_respects_the_value_conventions(tmp_path):
    frame = build_test_frame(_result(tmp_path))

    assert frame["primary_key"].is_unique
    # <SAFE>:WV_<imagette>_<ref_lon>_<ref_lat>, the position at one decimal
    assert frame["primary_key"].str.match(r".*\.SAFE:WV_\d+_-?\d+\.\d_-?\d+\.\d$").all()
    # and it is the catalogue's own key, carried through rather than recomposed
    assert frame["primary_key"].iloc[0] == f"{CORE}_35F2.SAFE:WV_033_-135.4_-74.3"
    # timestamps are typed, whole-second, UTC — the validator asks for datetime64[ns]
    for column in ("sar_time", "scat_time", "swot_time"):
        assert "datetime64" in str(frame[column].dtype), column
        assert (frame[column].dt.microsecond == 0).all(), column
    for column in ("sar_lon", "scat_lon", "swot_lon"):
        assert frame[column].between(-180, 180).all()
    # the fixture's heading is -128.2; the export is clockwise from north, in [0, 360)
    assert frame["sar_ground_heading"].iloc[0] == pytest.approx(231.8)
    assert frame["sar_ground_heading"].between(0, 360, inclusive="left").all()


def test_write_test_parquet_records_the_global_attributes(tmp_path):
    frame = build_test_frame(_result(tmp_path))
    target = tmp_path / "s1d_swot_ascat_test.parquet"

    write_test_parquet(frame, target, "KNMI-ASCAT-METOP-12.5km")

    metadata = pq.ParquetFile(target).schema_arrow.metadata or {}
    assert metadata[b"source scat"] == b"KNMI-ASCAT-METOP-12.5km"
    assert metadata[b"source ancillary datasets"] == b"rain: IMERG HHL v7 NASA"
    assert metadata[b"library used to produce the parquet"] == b"soba_reference_repo"
    assert metadata[b"library version"].startswith(b"commit ")
    assert metadata[b"creation date"] == pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d").encode()
    assert len(pd.read_parquet(target)) == len(frame)


# --- LaTeX -------------------------------------------------------------------

def test_stage_latex_assets_copies_everything_the_template_needs(tmp_path):
    build = tmp_path / "build"

    staged = stage_latex_assets(DEFAULT_LATEX_DIR, build)

    for name in LATEX_AUXILIARY_FILES:
        assert (DEFAULT_LATEX_DIR / name).is_file(), f"missing source asset: {name}"
        assert (build / name).is_file(), f"not staged: {name}"
    assert len(staged) == len(LATEX_AUXILIARY_FILES)
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
        test_name="S1D_reference_test_dataset_WV_20260107_20260107_20260916_SV_X_Y_0.1.parquet",
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
    assert "soba_reference_repo" in tex  # the generated command names the tool
    assert "TEST dataset reference TEST dataset" not in tex  # no doubled wording
    assert "for S1D / SWOT / ASCAT reference TEST dataset" in tex
    assert r"\label{tab:reference_stats}" in tex  # reference statistics table
    assert r"\label{tab:columns}" in tex  # catalogue columns table
    # versioning tables: documentation rows kept, dataset file row added
    assert r"\caption{Versioning of the documentation}" in tex
    assert r"\caption{Versioning of test catalogue files}" in tex
    assert (
        r"\path|S1D_reference_test_dataset_WV_20260107_20260107_20260916_SV_X_Y_0.1.parquet|"
        in tex
    )
    assert r"\hypersetup{hidelinks}" in tex  # no red link boxes
    # both reference files are listed on their own line
    assert (
        r"\path|scat.parquet|\\" in tex and r"\path|swot.parquet| (KaRIn" in tex
    )


def test_reference_statistics_table_reports_min_max_mean_median(tmp_path):
    table = reference_statistics_table(_result(tmp_path))

    assert r"\label{tab:reference_stats}" in table
    assert r"\textbf{Min} & \textbf{Max} & \textbf{Mean} & \textbf{Median}" in table
    assert "ASCAT wind speed (m/s) & 9.80 & 9.80 & 9.80 & 9.80" in table
    assert "ASCAT wind direction (°) & 17.80 & 17.80 & 17.80 & 17.80" in table
    assert "SWOT KaRIn wave height (m) & 1.90 & 1.90 & 1.90 & 1.90" in table


def test_build_report_tex_points_includegraphics_at_the_given_figures_folder(tmp_path):
    folder = "images_S1D_reference_test_dataset_WV_20260107_SV_X_Y_0.1"
    tex = build_report_tex(
        DEFAULT_LATEX_DIR / "template.tex",
        _result(tmp_path),
        label="s1d_swot_ascat",
        figure_names=["a.png", "b.png", "c.png"],
        figures_dir=folder,
    )

    assert rf"\includegraphics[width=\textwidth]{{{folder}/a.png}}" in tex
    assert "{figures/" not in tex


def test_insert_version_history_row_survives_template_row_edits():
    template = (
        r"\section*{Version History}" "\n"
        r"\begin{table}[H]" "\n"
        r"    \caption{Versioning of the documentation}" "\n"
        r"    \begin{tabular}{@{}p{2cm}@{}}" "\n"
        r"        \toprule" "\n"
        r"        1.0.0 & heavily edited by hand \\" "\n"
        r"        1.0.1 & and again \\" "\n"
        r"        \bottomrule" "\n"
        r"    \end{tabular}" "\n"
        r"\end{table}" "\n"
    )

    filled = insert_version_history_row(template, "9.9.9 & generated")

    assert "        9.9.9 & generated \\\\" in filled
    assert (
        filled.index("1.0.1 & and again")
        < filled.index("9.9.9 & generated")
        < filled.index(r"\bottomrule")
    )


def test_insert_table_row_targets_the_table_that_carries_the_caption():
    template = (
        r"\begin{table}[H]" "\n"
        r"    \caption{Versioning of the documentation}" "\n"
        r"        1.0.0 & doc row \\" "\n"
        r"        \bottomrule" "\n"
        r"\end{table}" "\n"
        r"\begin{table}[H]" "\n"
        r"    \caption{Versioning of test catalogue files}" "\n"
        r"        \midrule" "\n"
        r"        \bottomrule" "\n"
        r"\end{table}" "\n"
    )

    filled = insert_table_row(
        template, "Versioning of test catalogue files", "the_file.parquet & 2026-09-16 & generated"
    )

    documentation, catalogue_files = filled.split(r"\end{table}", 1)
    assert "the_file.parquet & 2026-09-16 & generated \\\\" in catalogue_files
    assert "the_file.parquet" not in documentation


def test_build_report_tex_fails_loudly_when_an_anchor_is_missing(tmp_path):
    broken = tmp_path / "broken.tex"
    broken.write_text(
        (DEFAULT_LATEX_DIR / "template.tex").read_text().replace(r"\hl{dataset name}", "gone")
    )

    with pytest.raises(ValueError, match="anchor"):
        _fill(tmp_path, template=broken)


def test_build_report_tex_wraps_every_file_name_in_path(tmp_path):
    # underscores are everywhere (SAFE names, catalogue filenames, the TEST name)
    # and ``_`` switches TeX into math mode, which aborts the compile; the url
    # package's \path|...| sets them verbatim, and lets long names wrap.
    tex = build_report_tex(
        DEFAULT_LATEX_DIR / "template.tex",
        _result(tmp_path),
        label="s1d_swot_ascat",
        figure_names=["a.png", "b.png", "c.png"],
        scat_name="S1D_coaligned_catalogue.parquet",
        swot_name="S1D_swot_catalogue.parquet",
        test_name="S1D_reference_test_dataset_WV_20260107_SV_X_Y_0.1.parquet",
    )

    for name in (
        "S1D_coaligned_catalogue.parquet",
        "S1D_swot_catalogue.parquet",
        "S1D_reference_test_dataset_WV_20260107_SV_X_Y_0.1.parquet",
    ):
        assert rf"\path|{name}|" in tex


# --- validator ---------------------------------------------------------------

def test_exported_primary_key_matches_the_validator_pattern(tmp_path):
    frame = build_test_frame(_result(tmp_path))
    pattern = SOBAValidationRules.COMMON_MANDATORY["primary_key"]["pattern"]

    assert frame["primary_key"].str.match(pattern).all(), frame["primary_key"].tolist()


def test_exported_frame_passes_the_bundled_validator(tmp_path):
    """The export is clean against the validator once it is told which reference family to check.

    The bundled copy carries a marked local change: its mandatory reference columns follow the
    source (`scat_lon`/`scat_lat`/`scat_time`) instead of the retired `ref_lon`/`ref_lat`/
    `ref_time`. The gist itself still asks for `ref_*`; see the header of `validator.py`.
    """
    path = write_test_parquet(build_test_frame(_result(tmp_path)), tmp_path / "test.parquet")

    result = SOBAParquetValidator(
        mode="WV", dataset_type="test", reference="scat"
    ).validate_file(str(path))

    assert result["valid"] is True, result["errors"]
    warnings = " ".join(result["warnings"])
    for column in ("sar_time", "scat_time", "sar_ground_heading", "sar_safe_slc", "sar_safe_ocn"):
        assert column not in warnings, warnings


def test_the_legacy_fallback_covers_one_reference_family(tmp_path):
    """The retired trio was family-agnostic, so it stands for one family only: asking for a
    second reference the file does not carry still fails."""
    frame = build_test_frame(_result(tmp_path)).rename(
        columns={"scat_lon": "ref_lon", "scat_lat": "ref_lat", "scat_time": "ref_time"}
    ).drop(columns=["swot_lon", "swot_lat", "swot_time"])
    path = write_test_parquet(frame, tmp_path / "legacy_one_family.parquet")

    result = SOBAParquetValidator(
        mode="WV", dataset_type="test", reference="scat,swot"
    ).validate_file(str(path))

    assert result["valid"] is False
    assert any(
        "swot_lon, swot_lat, swot_time" in error for error in result["errors"]
    ), result["errors"]


def test_the_validator_falls_back_to_the_legacy_ref_names(tmp_path):
    """A file written before the <ref>_ rename carries ref_lon/ref_lat/ref_time. It validates,
    and the substitution is reported rather than hidden."""
    frame = build_test_frame(_result(tmp_path)).rename(
        columns={"scat_lon": "ref_lon", "scat_lat": "ref_lat", "scat_time": "ref_time"}
    )
    path = write_test_parquet(frame, tmp_path / "legacy.parquet")

    result = SOBAParquetValidator(
        mode="WV", dataset_type="test", reference="scat"
    ).validate_file(str(path))

    assert result["valid"] is True, result["errors"]
    assert any("legacy name 'ref_lon'" in note for note in result["info"]), result["info"]


def test_the_validator_checks_the_reference_families_it_is_given(tmp_path):
    path = write_test_parquet(build_test_frame(_result(tmp_path)), tmp_path / "test.parquet")

    for reference in ("scat", "swot", "scat,swot", ("scat", "swot")):
        result = SOBAParquetValidator(
            mode="WV", dataset_type="test", reference=reference
        ).validate_file(str(path))
        assert result["valid"] is True, (reference, result["errors"])


def test_the_validator_rejects_a_reference_source_outside_scat_and_swot():
    # the spec names alti_* too, but this tool has no ALTI crossing, so it is not offered
    for source in ("gnss", "alti"):
        with pytest.raises(ValueError, match="unsupported reference source"):
            SOBAValidationRules.get_reference_columns(source)


def test_bundled_validator_rejects_a_frame_missing_a_mandatory_column(tmp_path):
    path = tmp_path / "incomplete.parquet"
    pd.DataFrame({"primary_key": ["a"], "sar_time": ["2026-01-01 00:00:00"]}).to_parquet(path)

    result = SOBAParquetValidator(mode="WV", dataset_type="test").validate_file(str(path))

    assert result["valid"] is False
    assert any("Missing mandatory variables" in error for error in result["errors"])


# --- LaTeX discovery ---------------------------------------------------------

def test_find_pdflatex_prefers_the_named_directory(tmp_path):
    (tmp_path / "pdflatex.exe").write_bytes(b"x")

    # str and Path both work, so the CLI value can be passed straight through
    assert find_pdflatex(tmp_path) == str(tmp_path / "pdflatex.exe")
    assert find_pdflatex(str(tmp_path)) == str(tmp_path / "pdflatex.exe")


def test_find_pdflatex_fails_loudly_on_a_directory_without_it(tmp_path):
    with pytest.raises(FileNotFoundError, match="no pdflatex in"):
        find_pdflatex(tmp_path / "not-a-tex-install")


def test_find_pdflatex_falls_back_to_path(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/pdflatex")

    assert find_pdflatex() == "/usr/bin/pdflatex"


def test_find_pdflatex_explains_a_missing_toolchain(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)

    with pytest.raises(FileNotFoundError, match="not on PATH"):
        find_pdflatex()


# --- TEST dataset naming -----------------------------------------------------

def test_build_test_filename_applies_the_naming_convention():
    name = build_test_filename(
        satellite="S1D",
        sar_mode="WV",
        polarization="SV",
        ref_product=BOTH_PRODUCTS,
        sar_times=pd.to_datetime(["2026-01-12 13:04:11", "2026-02-16 09:30:56"], utc=True),
        version="0.1",
        production_date="2026-09-16",
    )

    assert name == (
        "S1D_reference_test_dataset_WV_20260112_20260216_20260916_"
        "SV_KNMI-ASCAT-METOP-12.5km_PODAAC-SWOT-KARIN-L2-WINDWAVE-D0_0.1.parquet"
    )


def test_default_test_name_reads_both_reference_catalogues(tmp_path):
    scat_path, swot_path = _write_pair(tmp_path, scat_filename=SOURCE_SCAT_NAME)
    result = run_crossing(scat_path, swot_path, "S1D", ReportConfig())

    name = default_test_name(result, SOURCE_SCAT_NAME, SOURCE_SWOT_NAME, "S1D", version="0.1")

    assert re.fullmatch(
        r"S1D_reference_test_dataset_WV_20260107_20260107_\d{8}_SV_"
        r"KNMI-ASCAT-METOP-12\.5km_PODAAC-SWOT-KARIN-L2-WINDWAVE-D0_0\.1\.parquet",
        name,
    ), name


def test_default_test_name_rejects_a_mismatched_satellite(tmp_path):
    scat_path, swot_path = _write_pair(tmp_path, scat_filename=SOURCE_SCAT_NAME)
    result = run_crossing(scat_path, swot_path, "S1D", ReportConfig())

    with pytest.raises(ValueError, match="S1C"):
        default_test_name(result, SOURCE_SCAT_NAME, SOURCE_SWOT_NAME, "S1C")


# --- build directory hygiene -------------------------------------------------

def test_purge_latex_byproducts_keeps_the_tex_and_the_pdf(tmp_path):
    build = tmp_path / "build"
    (build / "figures").mkdir(parents=True)
    for name in ("report.pdf", "report.tex", "soba.sty", "logo_soba.png"):
        (build / name).write_bytes(b"x")
    (build / "figures" / "map.png").write_bytes(b"x")
    for suffix in (".aux", ".log", ".out", ".toc"):
        (build / f"report{suffix}").write_text("x")

    purge_latex_byproducts(build, "report")

    assert sorted(path.name for path in build.iterdir()) == [
        "figures", "logo_soba.png", "report.pdf", "report.tex", "soba.sty",
    ]


# --- template ----------------------------------------------------------------

def test_the_vendored_template_describes_a_test_dataset():
    text = (DEFAULT_LATEX_DIR / "template.tex").read_text(encoding="utf-8")

    assert "TEST dataset" in text
    assert "Co-aligned Parquet Catalogue description" not in text
    assert r"\lfoot{Reference TEST Dataset Description - Ifremer}" in text


def test_exported_safe_columns_carry_no_archive_prefix(tmp_path):
    """The catalogues sometimes prefix the SAFE with its archive path; the spec's examples in
    the validator anchor the collection tag right after the satellite prefix."""
    frame = build_test_frame(_result(tmp_path))

    for column in ("sar_safe_slc", "sar_safe_ocn"):
        assert "/" not in frame[column].iloc[0], column
    assert frame["sar_safe_slc"].str.match(r"^S1[ABCD]_WV_SLC__1S[SVH]{2}_.*\.SAFE:WV_\d+$").all()
    assert frame["sar_safe_ocn"].str.match(r"^S1[ABCD]_WV_OCN__2S[SVH]{2}_.*\.SAFE:WV_\d+$").all()


def test_the_parquet_carries_the_global_attributes(tmp_path):
    import pyarrow.parquet as pq

    path = write_test_parquet(
        build_test_frame(_result(tmp_path)), tmp_path / "test.parquet", "KNMI-ASCAT-METOP-12.5km"
    )
    metadata = pq.read_schema(path).metadata

    assert metadata[b"source scat"] == b"KNMI-ASCAT-METOP-12.5km"
    assert metadata[b"source ancillary datasets"] == b"rain: IMERG HHL v7 NASA"
    assert metadata[b"library used to produce the parquet"] == b"soba_reference_repo"
    assert metadata[b"library version"].startswith(b"commit ")
    assert metadata[b"creation date"] == pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d").encode()


def test_the_template_column_table_documents_every_exported_column(tmp_path):
    columns = build_test_frame(_result(tmp_path)).columns
    text = (DEFAULT_LATEX_DIR / "template.tex").read_text(encoding="utf-8")
    section = text.split(r"\subsection{Catalogue Columns}")[1].split(r"\section{")[0]

    missing = [column for column in columns if column.replace("_", r"\_") not in section]
    assert not missing, f"columns missing from the template tables: {missing}"


def test_the_ancillary_columns_sit_under_the_second_reference(tmp_path):
    text = (DEFAULT_LATEX_DIR / "template.tex").read_text(encoding="utf-8")
    table = text.split(r"\subsection{Catalogue Columns}")[1].split(r"\section{")[0]
    group = table.split(r"\textbf{Ancillary}")[1]

    missing = [column for column in ANCILLARY_COLUMNS if column.replace("_", r"\_") not in group]
    assert not missing, f"ancillary columns missing from the Ancillary group: {missing}"


# --- CLI ---------------------------------------------------------------------

def test_cli_validate_flag_reports_the_bundled_validator(tmp_path):
    """`--validate` runs the bundled validator against the file just written and gates the
    exit code on its verdict; the tool's crossing is scatterometer-referenced, so it passes
    `scat` as the reference family."""
    scat_path, swot_path = _write_pair(
        tmp_path, scat_filename=SOURCE_SCAT_NAME, swot_filename=SOURCE_SWOT_NAME
    )

    completed = subprocess.run(
        [sys.executable, "-m", "soba_reference_repo.cli",
         "--scat", str(scat_path), "--swot", str(swot_path),
         "--satellite", "S1D", "--scatterometer", "ASCAT",
         "--label", "validation", "--no-compile", "--validate",
         "--output-dir", str(tmp_path / "report"), "--test-dir", str(tmp_path / "deliverables")],
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "PYTHONPATH": "src"},
        capture_output=True, text=True, check=False,
    )

    assert "SOBA PARQUET VALIDATION REPORT" in completed.stdout
    assert "Status: ✅ PASSED" in completed.stdout
    assert completed.returncode == 0


def test_cli_reference_flag_overrides_the_validated_families(tmp_path):
    """The validated families default to the catalogues given — scat,swot here — and
    `--reference` overrides them."""
    scat_path, swot_path = _write_pair(
        tmp_path, scat_filename=SOURCE_SCAT_NAME, swot_filename=SOURCE_SWOT_NAME
    )

    completed = subprocess.run(
        [sys.executable, "-m", "soba_reference_repo.cli",
         "--scat", str(scat_path), "--swot", str(swot_path),
         "--satellite", "S1D", "--scatterometer", "ASCAT",
         "--label", "reference", "--no-compile", "--validate", "--reference", "swot",
         "--output-dir", str(tmp_path / "report"), "--test-dir", str(tmp_path / "deliverables")],
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "PYTHONPATH": "src"},
        capture_output=True, text=True, check=False,
    )

    assert "Status: ✅ PASSED" in completed.stdout
    assert completed.returncode == 0


def test_cli_runs_end_to_end_without_compiling(tmp_path):
    scat_path, swot_path = _write_pair(
        tmp_path, scat_filename=SOURCE_SCAT_NAME, swot_filename=SOURCE_SWOT_NAME
    )
    output_dir = tmp_path / "report"
    test_dir = tmp_path / "deliverables"

    completed = subprocess.run(
        [sys.executable, "-m", "soba_reference_repo.cli",
         "--scat", str(scat_path), "--swot", str(swot_path),
         "--satellite", "S1D", "--scatterometer", "ASCAT",
         "--label", "unit", "--no-compile",
         "--output-dir", str(output_dir), "--test-dir", str(test_dir)],
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "PYTHONPATH": "src"},
        capture_output=True, text=True, check=False,
    )

    assert completed.returncode == 0, completed.stderr
    # --no-compile keeps the intermediates; the PDF step is what purges them
    produced = list(test_dir.glob("S1D_reference_test_dataset_*.parquet"))
    assert len(produced) == 1, sorted(p.name for p in test_dir.iterdir())
    # the report carries the same name as the dataset file it documents
    assert (output_dir / f"{produced[0].stem}.tex").is_file()
    # the figures live in a folder named after it too
    assert len(list((output_dir / f"images_{produced[0].stem}").glob("*.png"))) == 3
    assert re.fullmatch(
        r"S1D_reference_test_dataset_WV_20260107_20260107_\d{8}_SV_"
        r"KNMI-ASCAT-METOP-12\.5km_PODAAC-SWOT-KARIN-L2-WINDWAVE-D0_0\.1\.parquet",
        produced[0].name,
    ), produced[0].name
    # the manifest is named after the dataset file it describes
    assert (test_dir / f"{produced[0].stem}_manifest.json").is_file(), (
        sorted(p.name for p in test_dir.iterdir())
    )


def test_cli_requires_satellite_and_scatterometer(tmp_path):
    scat_path, swot_path = _write_pair(tmp_path, scat_filename=SOURCE_SCAT_NAME)

    completed = subprocess.run(
        [sys.executable, "-m", "soba_reference_repo.cli",
         "--scat", str(scat_path), "--swot", str(swot_path), "--no-compile"],
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "PYTHONPATH": "src"},
        capture_output=True, text=True, check=False,
    )

    assert completed.returncode != 0
    assert "--satellite" in completed.stderr
    assert "--scatterometer" in completed.stderr
