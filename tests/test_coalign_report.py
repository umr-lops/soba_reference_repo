"""Documentation of the coalign catalogues: one document per reference kind."""

import pytest
from coalign_fixtures import make_archive, make_item, point, write_day
from test_coalign_ancillary_ecmwf import write_wind
from test_coalign_ancillary_tengeop import write_summary

from soba_reference_repo.coalign.catalogue import build_catalogues
from soba_reference_repo.catalogue_report import (
    build_coalign_report as build_report,
)
from soba_reference_repo.catalogue_report import (
    coalign_completeness_table as completeness_table,
)
from soba_reference_repo.catalogue_report import (
    read_coalign_catalogues as read_catalogues,
)

DAYS = ["2026-11-19"]


def _build(tmp_path, ancillary=True):
    """HSCAT and altimeter catalogues from one synthetic STAC tree."""
    ocn_dir = make_archive(tmp_path / "arch")
    scat = tmp_path / "stac" / "scat" / "hscat_hy-2b_25km"
    alti = tmp_path / "stac" / "alti" / "cmems"
    write_day(scat, DAYS[0], [make_item(ocn_dir, 1, [
        point(-72.33, -66.2, wind_speed=11.0, wind_direction_to=170.0, quality_flag=0.0),
        point(-72.30, -66.1, wind_speed=9.0, wind_direction_to=160.0, quality_flag=0.0)])])
    write_day(alti, DAYS[0], [make_item(ocn_dir, 2, [point(-72.2, -66.2, VAVH=2.5)])],
              reference_product="CMEMS_WAV_L3_ALTI_VAVH")
    paths = {}
    if ancillary:
        write_wind(tmp_path / "wind")
        write_summary(tmp_path / "dl")
        paths = {"tengeop": tmp_path / "dl", "ecmwf": tmp_path / "wind"}
    files, _ = build_catalogues([scat, alti], DAYS[0], DAYS[0], tmp_path / "cat",
                                production="20261126", ancillary_paths=paths)
    return tmp_path / "cat", files


def test_one_document_per_reference_kind_with_the_expected_sections(tmp_path):
    folder, files = _build(tmp_path)
    results = build_report(folder, tmp_path / "out", compile_report=False)
    assert [r["kind"] for r in results] == ["HSCAT", "ALTI"]
    scat = (tmp_path / "out" / "S1_coalign_catalogue_WV_HSCAT.tex").read_text()
    for text in ("Wind-Speed and Wind-Direction Distributions", "windspeed\\_scat",
                 "Share of empty values", "KNMI-HSCAT-HY2-25km catalogue columns",
                 "ECMWF\\_Wind\\_Speed", "Position in the Dataflow", "Global Coverage Map",
                 "Monthly Distribution of Matchups"):
        assert text in scat, text
    for removed in ("Time Offset Between SAR and Reference", "Ancillary Values",
                    "How the Catalogue Is Produced", "Agreement of the ECMWF"):
        assert removed not in scat, removed
    alti = (tmp_path / "out" / "S1_coalign_catalogue_WV_ALTI.tex").read_text()
    assert "swh\\_alti" in alti and "windspeed\\_scat" not in alti
    assert "@@" not in scat and "@@" not in alti
    assert (tmp_path / "out" / "images_S1_coalign_catalogue_WV_HSCAT" / "coverage.png").is_file()


def test_the_report_needs_no_ancillary_columns(tmp_path):
    folder, _ = _build(tmp_path, ancillary=False)
    results = build_report(folder, tmp_path / "out", compile_report=False)
    tex = (tmp_path / "out" / "S1_coalign_catalogue_WV_HSCAT.tex").read_text()
    assert len(results) == 2 and "ECMWF\\_Wind\\_Speed" not in tex


def test_file_table_lists_each_file_with_its_rows(tmp_path):
    folder, files = _build(tmp_path)
    build_report(folder, tmp_path / "out", compile_report=False)
    tex = (tmp_path / "out" / "S1_coalign_catalogue_WV_HSCAT.tex").read_text()
    assert "Contains 2 matches." in tex and "20261119" in tex


def test_read_catalogues_rejects_a_folder_without_catalogues_and_a_foreign_name(tmp_path):
    with pytest.raises(FileNotFoundError):
        read_catalogues(tmp_path)
    (tmp_path / "S1A_coaligned_catalogue_WV_bad.parquet").write_bytes(b"")
    with pytest.raises(ValueError, match="not a coalign catalogue name"):
        read_catalogues(tmp_path)


def test_completeness_table(tmp_path):
    folder, _ = _build(tmp_path)
    entries = read_catalogues(folder)
    scat = [e for e in entries if e["kind"] == "HSCAT"]
    table = completeness_table(scat)
    assert "legacy\\_usage" in table and "100.0\\%" in table


def test_the_report_compiles_to_a_pdf(tmp_path):
    import shutil
    if not (shutil.which("pdflatex") or shutil.which("tectonic")):
        pytest.skip("no LaTeX engine")
    folder, _ = _build(tmp_path)
    results = build_report(folder, tmp_path / "out", compile_report=True)
    assert all(r["pdf"] and (tmp_path / "out").joinpath(
        r["pdf"].split("/")[-1]).stat().st_size > 20_000 for r in results)


def test_the_cli_writes_the_documents(tmp_path):
    from soba_reference_repo.catalogue_report import main
    folder, _ = _build(tmp_path)
    assert main(["--coalign", str(folder), "--output", str(tmp_path / "o"), "--no-compile"]) == 0
    assert (tmp_path / "o" / "S1_coalign_catalogue_WV_ALTI.tex").is_file()


def test_monthly_figure_has_a_bar_for_every_month_that_has_rows(tmp_path):
    """A month is plotted only when it holds matchups, with its count above the bar."""
    import pandas as pd

    from soba_reference_repo.plotting import figure_monthly
    frame = pd.DataFrame({
        "sar_time": pd.to_datetime(["2026-06-30 23:00", "2026-06-30 23:10", "2026-07-01 00:10"]),
        "satellite": ["S1C", "S1D", "S1D"], "product": ["P"] * 3})
    figure_monthly(frame, tmp_path / "monthly.png")
    assert (tmp_path / "monthly.png").stat().st_size > 5_000


def _frame():
    import pandas as pd
    times = pd.to_datetime(["2026-06-10", "2026-06-11", "2026-07-01", "2026-06-12", "2026-06-13"])
    return pd.DataFrame({
        "sar_time": times, "sar_lon": [10.0, 20.0, 30.0, 40.0, 50.0],
        "sar_lat": [0.0, 1.0, 2.0, 3.0, 4.0],
        "satellite": ["S1C", "S1D", "S1D", "S1C", "S1D"], "product": ["P"] * 5,
        "mission": ["HY-2B HSCAT", "HY-2B HSCAT", "HY-2C HSCAT", "HY-2B HSCAT", "HY-2B HSCAT"]})


def test_groups_list_the_values_present_most_rows_first():
    from soba_reference_repo.plotting import group_values
    frame = _frame()
    assert group_values(frame, "satellite") == ["S1C", "S1D"]
    assert group_values(frame, "mission") == ["HY-2B HSCAT", "HY-2C HSCAT"]
    assert group_values(frame, "product") == ["P"]


def test_coverage_and_monthly_blocks_follow_the_groups(tmp_path):
    from soba_reference_repo.catalogue_report import (
        coalign_coverage_blocks as coverage_blocks,
    )
    from soba_reference_repo.catalogue_report import (
        coalign_monthly_blocks as monthly_blocks,
    )
    tex = coverage_blocks(_frame(), "img", tmp_path)
    assert "Coverage per Sentinel-1 satellite: S1C, S1D." in tex
    assert "Coverage per reference mission: HY-2B HSCAT, HY-2C HSCAT." in tex
    assert "All rows have the same reference product: P" in tex      # one value: a sentence
    assert (tmp_path / "coverage_satellite_1.png").is_file()
    assert (tmp_path / "coverage_mission_1.png").is_file()
    assert not (tmp_path / "coverage_product_1.png").exists()
    monthly = monthly_blocks(_frame(), "img", tmp_path)
    assert "stacked by reference mission" in monthly
    assert "All rows have the same reference product: P" in monthly
    assert (tmp_path / "monthly_mission.png").is_file()


def test_many_values_are_split_over_figures_of_three_panels(tmp_path):
    import pandas as pd

    from soba_reference_repo.catalogue_report import (
        coalign_coverage_blocks as coverage_blocks,
    )
    frame = pd.DataFrame({
        "sar_time": pd.to_datetime(["2026-06-10"] * 5), "sar_lon": [1.0, 2, 3, 4, 5],
        "sar_lat": [0.0, 1, 2, 3, 4], "satellite": ["S1C"] * 5, "product": ["P"] * 5,
        "mission": [f"alti-{n}" for n in range(5)]})
    tex = coverage_blocks(frame, "img", tmp_path)
    assert tex.count("\\includegraphics") == 2                      # 5 values -> 3 + 2
    assert (tmp_path / "coverage_mission_2.png").is_file()
    assert not (tmp_path / "coverage_mission_3.png").exists()
