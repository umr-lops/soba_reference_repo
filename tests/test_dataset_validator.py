"""Catalogue / curated Parquet validation (soba_validate_parquets --type catalogue|curated)."""

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from soba_reference_repo import validator
from soba_reference_repo.dataset_validator import validate_dataset

CATALOGUE = ("S1A_coaligned_catalogue_WV_20241126_20250630_20261117_SV_"
             "CMEMS-WAV-L3-ALTI-TAC_2.0.parquet")
CURATED = "S1A_curated_WV_20241126_20250630_20261117_SV_CMEMS-WAV-L3-ALTI-TAC_5.0.parquet"
IW_CATALOGUE = ("S1A_coaligned_catalogue_IW_20241126_20250630_20261117_DV_"
                "PODAAC-SWOT-KARIN-L2-WINDWAVE-D2_0.1.parquet")
GLOBAL_META = {
    "source alti": "Jason-3 altimeter",
    "source ancillary datasets": "class: TenGeoP",
    "library used to produce the parquet": "soba_reference_repo",
    "library version": "2026.11.27",
    "creation date": "20261117",
}


def frame(n=3, unit="S1A"):
    times = pd.to_datetime(["2024-11-26 01:00", "2025-03-01 12:00", "2025-06-30 23:00"][:n],
                           utc=True)
    return pd.DataFrame({
        "primary_key": [f"{unit}_WV_SLC__1SSV_2025010{i}.SAFE:WV_00{i}_1.25_-10.50"
                        for i in range(n)],
        "sar_time": times,
        "sar_lat": [10.0, -20.0, 45.0][:n],
        "sar_lon": [1.0, 179.5, -30.0][:n],
        "sar_incidence_angle": [23.0] * n,
        "sar_elevation_angle": [20.0] * n,
        "sar_ground_heading": [190.0] * n,
        "sar_distance_to_coast": [120.0] * n,
        "sar_safe_slc": [f"{unit}_WV_SLC__1SSV_2025010{i}.SAFE" for i in range(n)],
        "sar_safe_ocn": [f"{unit}_WV_OCN__2SSV_2025010{i}.SAFE" for i in range(n)],
        "sar_path_slc": ["/a"] * n,
        "sar_path_ocn": ["/b"] * n,
        "ref_time": times,
        "ref_lat": [10.0] * n,
        "ref_lon": [1.0] * n,
        "ref_flag": [0] * n,
        "ref_id": ["granule"] * n,
        "legacy_usage": [None] * n,
        "ref_source": ["Jason-3"] * n,
        "class_1": ["POS"] * n,
        "pos_1": [0.8] * n,
        "swh_alti": [2.0] * n,
    })


def iw_frame(n=3, unit="S1A"):
    grd = [f"{unit}_IW_GRDH_1SDV_2025010{i}T000000_2025010{i}T000020_000001_000001_AAAA.SAFE"
           for i in range(n)]
    times = pd.to_datetime(["2024-11-26 01:00", "2025-03-01 12:00", "2025-06-30 23:00"][:n],
                           utc=True)
    columns = {name: [1.0] * n for name in (
        "sar_incidence_angle", "sar_elevation_angle", "sar_ground_heading",
        "sar_distance_to_along_track_edge", "sar_distance_to_cross_track_edge",
        "sea_ice_concentration", "bathymetry", "ref_distance_to_coast", "WW3_Hs",
        "ECMWF_Wind_Speed", "ECMWF_Wind_Direction", "imerg_rainrate", "swh_swot")}
    return pd.DataFrame({
        "primary_key": [f"{safe}_1.25_-10.50" for safe in grd],
        "sar_time": times, **columns,
        "sar_path_ocn": [None] * n, "sar_path_slc": ["/s"] * n, "sar_path_grd": ["/g"] * n,
        "sar_safe_grd": grd, "sar_safe_slc": [""] * n, "sar_safe_ocn": [""] * n,
        "ref_lon": [1.25] * n, "ref_lat": [-10.5] * n, "ref_time": times, "ref_id": ["g"] * n,
    })


def save(tmp_path, df, name, curated=False, metadata=None):
    if curated:
        df = df.assign(_curation_mission=df["sar_safe_slc"].str[:3],
                       _curation_row=range(len(df)))
    table = pa.Table.from_pandas(df, preserve_index=False)
    metadata = {**GLOBAL_META, **(metadata or {})}
    if curated:
        metadata.setdefault("source_catalogue_dataset", CATALOGUE)
    metadata = {key: value for key, value in metadata.items() if value is not None}
    table = table.replace_schema_metadata(
        {**(table.schema.metadata or {}), **{k.encode(): v.encode()
                                             for k, v in metadata.items()}})
    path = tmp_path / name
    pq.write_table(table, path)
    return path


GOOD_META = {"dataset_version": "5.0", "production_date": "20261117"}


def statuses(report):
    return {name: status for status, name, _ in report.checks}


def test_valid_catalogue_and_curated_pass(tmp_path):
    cat = validate_dataset(save(tmp_path, frame(), CATALOGUE), "catalogue")
    cur = validate_dataset(save(tmp_path, frame(), CURATED, curated=True, metadata=GOOD_META),
                           "curated")
    assert cat.ok and cur.ok
    assert "FAIL" not in statuses(cat).values()
    assert set(statuses(cur).values()) == {"PASS"}


def test_curated_filename_dates_must_match_the_kept_sar_dates(tmp_path):
    name = CURATED.replace("20241126_20250630", "20150319_20251231")
    report = validate_dataset(save(tmp_path, frame(), name, curated=True, metadata=GOOD_META),
                              "curated")
    assert not report.ok
    assert statuses(report)["Filename dates match the data"] == "FAIL"


def test_catalogue_data_outside_filename_dates_fails_wider_name_warns(tmp_path):
    outside = CATALOGUE.replace("20250630_2026", "20250101_2026")
    assert statuses(validate_dataset(save(tmp_path, frame(), outside), "catalogue"))[
        "Data within the filename dates"] == "FAIL"
    wider = CATALOGUE.replace("20250630_2026", "20251231_2026")
    report = validate_dataset(save(tmp_path, frame(), wider), "catalogue")
    assert report.ok
    assert statuses(report)["Data within the filename dates"] == "WARN"


def test_invalid_positions_fail_curated_but_only_warn_catalogue(tmp_path):
    df = frame()
    df.loc[0, "sar_lat"] = 9.97e36
    assert statuses(validate_dataset(save(tmp_path, df, CATALOGUE), "catalogue"))[
        "SAR positions"] == "WARN"
    report = validate_dataset(save(tmp_path, df, CURATED, curated=True, metadata=GOOD_META),
                              "curated")
    assert statuses(report)["SAR positions"] == "FAIL"


def test_unknown_safe_warns_and_foreign_unit_fails(tmp_path):
    df = frame()
    df.loc[0, "sar_safe_ocn"] = "UNKNOWN"
    report = validate_dataset(save(tmp_path, df, CATALOGUE), "catalogue")
    assert report.ok and statuses(report)["SAFE identities"] == "WARN"
    report = validate_dataset(save(tmp_path, frame(unit="S1C"), CATALOGUE), "catalogue")
    assert statuses(report)["SAFE unit matches the filename"] == "FAIL"


def test_duplicated_keys_and_missing_columns_fail(tmp_path):
    df = frame()
    df.loc[1, "primary_key"] = df.loc[0, "primary_key"]
    assert statuses(validate_dataset(save(tmp_path, df, CATALOGUE), "catalogue"))[
        "Primary keys"] == "FAIL"
    report = validate_dataset(save(tmp_path, frame().drop(columns="ref_flag"), CATALOGUE),
                              "catalogue")
    assert statuses(report)["Required columns"] == "FAIL"
    meta = {**GOOD_META, "source_catalogue_dataset": CATALOGUE}
    report = validate_dataset(save(tmp_path, frame(), CURATED, metadata=meta), "curated")
    assert report.ok and statuses(report)["Curation provenance"] == "WARN"


def test_curated_metadata_must_match_the_filename(tmp_path):
    bad = {"dataset_version": "4.0", "production_date": "20261117"}
    report = validate_dataset(save(tmp_path, frame(), CURATED, curated=True, metadata=bad),
                              "curated")
    assert statuses(report)["Version metadata"] == "FAIL"
    report = validate_dataset(save(tmp_path, frame(), CURATED, curated=True), "curated")
    assert report.ok and statuses(report)["Version metadata"] == "WARN"


@pytest.mark.parametrize("name", [
    "S1A_curated_WV_20241126_20250630_20261117_SV_CMEMS-WAV-L3-ALTI-TAC.parquet",
    "S1E_curated_WV_20241126_20250630_20261117_SV_CMEMS-WAV-L3-ALTI-TAC_5.0.parquet",
    "S1A_curated_WV_20250630_20241126_20261117_SV_CMEMS-WAV-L3-ALTI-TAC_5.0.parquet",
    "S1A_curated_WV_20241126T010000_20250630T230000_20261117T175657_SV_CMEMS-WAV-L3-ALTI-TAC"
    "_5.0.parquet",
    CATALOGUE,
])
def test_filename_convention(tmp_path, name):
    report = validate_dataset(save(tmp_path, frame(), name, curated=True, metadata=GOOD_META),
                              "curated")
    assert statuses(report)["Filename convention"] == "FAIL"


def test_cli_validates_several_files_and_exits_1_on_failure(tmp_path, capsys):
    good = save(tmp_path, frame(), CURATED, curated=True, metadata=GOOD_META)
    assert validator.main(["--type", "curated", "--file", str(good)]) == 0
    assert "1/1 CURATED file(s) passed" in capsys.readouterr().out
    bad = save(tmp_path, frame(), CURATED.replace("20250630_2026", "20251231_2026"),
               curated=True, metadata=GOOD_META)
    with pytest.raises(SystemExit) as error:
        validator.main(["--type", "curated", "--file", str(good), str(bad)])
    assert error.value.code == 1
    captured = capsys.readouterr()
    assert "1/2 CURATED file(s) passed" in captured.out
    assert "[FAIL] Filename dates match the data" in captured.out
    assert "validation failed: 1 file(s)" in captured.err


def test_primary_key_coordinates_need_two_decimals(tmp_path):
    df = frame()
    df["primary_key"] = [f"S1A_X.SAFE:WV_00{i}_-150.7_-73.6" for i in range(3)]
    report = validate_dataset(save(tmp_path, df, CATALOGUE), "catalogue")
    assert statuses(report)["Primary key coordinate precision"] == "FAIL"
    swot = CATALOGUE.replace("CMEMS-WAV-L3-ALTI-TAC", "PODAAC-SWOT-KARIN-L2-WINDWAVE-D0")
    report = validate_dataset(save(tmp_path, df, swot), "catalogue")
    assert statuses(report)["Primary key coordinate precision"] == "FAIL"  # no SWOT exception


def test_sar_lat_lon_are_optional(tmp_path):
    df = frame().drop(columns=["sar_lat", "sar_lon"])
    report = validate_dataset(save(tmp_path, df, CURATED, curated=True, metadata=GOOD_META),
                              "curated")
    assert report.ok
    assert statuses(report)["Required columns"] == "PASS"
    assert "not checked" in dict((n, d) for _, n, d in report.checks)["SAR positions"]


def test_iw_catalogue_uses_the_iw_spec_columns(tmp_path):
    report = validate_dataset(save(tmp_path, iw_frame(), IW_CATALOGUE,
                                   metadata={"source alti": None, "source swot": "KaRIn"}),
                              "catalogue")
    assert report.ok, report.checks
    assert "IW columns present" in dict((n, d) for _, n, d in report.checks)["Required columns"]
    report = validate_dataset(save(tmp_path, iw_frame().drop(columns="imerg_rainrate"),
                                   IW_CATALOGUE), "catalogue")
    assert statuses(report)["Required columns"] == "FAIL"
    # an IW file is not checked against the WV table (no class_1, legacy_usage...)
    report = validate_dataset(save(tmp_path, iw_frame(unit="S1C"), IW_CATALOGUE), "catalogue")
    assert statuses(report)["SAFE unit matches the filename"] == "FAIL"


@pytest.mark.parametrize("column", ["sar_ground_heading", "sar_distance_to_coast", "ref_id",
                                    "legacy_usage", "ref_source", "class_1", "pos_1"])
def test_wv_spec_mandatory_columns(tmp_path, column):
    report = validate_dataset(save(tmp_path, frame().drop(columns=column), CATALOGUE),
                              "catalogue")
    assert statuses(report)["Required columns"] == "FAIL"


@pytest.mark.parametrize("product, ok", [
    ("KNMI-HSCAT-HY2-25km", True), ("PODAAC-SWOT-KARIN-L2-WINDWAVE-D2", True),
    ("CCI-SEA-STATE-ALTI-L2-V5", True), ("CMEMS-ALTI-L3-VAVH", True),
    ("KNMI-ASCAT-METOP-25KM", True), ("KNMI-ASCAT-METOP-5.7KM", True),
    ("CCI-SEATSTATE_ALTI_L2P-V5", False), ("CMEMS-ALTI_L3_VAVH", False),
    ("KNMI-ASCAT-METOP-25km", False),
])
def test_reference_product_must_be_in_the_spec_list(tmp_path, product, ok):
    name = CATALOGUE.replace("CMEMS-WAV-L3-ALTI-TAC", product)
    report = validate_dataset(save(tmp_path, frame(), name), "catalogue")
    assert (statuses(report)["Reference product in the spec list"] == "PASS") is ok


@pytest.mark.parametrize("drop", ["source alti", "library version", "creation date",
                                  "source ancillary datasets",
                                  "library used to produce the parquet"])
def test_global_attributes_are_mandatory(tmp_path, drop):
    report = validate_dataset(save(tmp_path, frame(), CATALOGUE, metadata={drop: None}),
                              "catalogue")
    assert statuses(report)["Global attributes"] == "FAIL"
    assert drop.replace("source alti", "source <ref>") in dict(
        (n, d) for _, n, d in report.checks)["Global attributes"]


def test_curated_needs_its_source_catalogue(tmp_path):
    meta = {**GOOD_META, "source_catalogue_dataset": None}
    path = tmp_path / CURATED
    df = frame().assign(_curation_mission="S1A", _curation_row=range(3))
    table = pa.Table.from_pandas(df, preserve_index=False)
    pq.write_table(table.replace_schema_metadata(
        {k.encode(): v.encode() for k, v in {**GLOBAL_META, **meta}.items() if v}), path)
    report = validate_dataset(path, "curated")
    assert statuses(report)["Global attributes"] == "FAIL"
    assert "source_catalogue_dataset" in dict((n, d) for _, n, d in report.checks)[
        "Global attributes"]


def test_sar_lon_must_be_within_180(tmp_path):
    df = frame()
    df.loc[1, "sar_lon"] = 200.0
    report = validate_dataset(save(tmp_path, df, CURATED, curated=True, metadata=GOOD_META),
                              "curated")
    assert statuses(report)["SAR positions"] == "FAIL"
