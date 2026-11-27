"""coalign: STAC reading, SAR helpers, row building, catalogue writing."""

import pyarrow.parquet as pq
import pytest
from coalign_fixtures import OCN, SLC, make_archive, make_item, point, write_day

from soba_reference_repo.coalign.catalogue import (
    build_catalogues, catalogue_name, collect,
)
from soba_reference_repo.coalign.products import REFERENCES, product_name, reference_for
from soba_reference_repo.coalign.wv import check_catalogue, columns, item_rows, slc_safe_dir
from soba_reference_repo.coalign.sar import centroid
from soba_reference_repo.coalign.stac import find_days
from soba_reference_repo.dataset_validator import validate_dataset


def test_find_days_reads_both_layouts_and_skips_replaced(tmp_path):
    write_day(tmp_path / "alti" / "cmems", "2026-08-30", [])
    write_day(tmp_path / "alti" / "cmems", "2026-08-31", [], layout="ocn/wv")
    write_day(tmp_path / "alti" / "cmems", "2026-08-30.replaced-20261120", [])
    write_day(tmp_path / "alti" / "cmems", "2026-09-05", [])
    days = find_days(tmp_path / "alti" / "cmems", "2026-08-30", "2026-08-31")
    assert [d.name for d in days] == ["2026-08-30", "2026-08-31"]


def test_find_days_reads_the_common_layout_and_prefers_it(tmp_path):
    """<collection>/<day>/<product_type>/<mode>/ (soba_matchups production layout)."""
    swot = tmp_path / "swot" / "d2"
    write_day(swot / "2026-11-19" / "ocn", "wv", [])          # -> swot/d2/2026-11-19/ocn/wv
    write_day(swot / "2026-11-19" / "ocn", "iw", [])          # another mode: not a WV run
    write_day(swot / "2026-11-20" / "slc", "wv", [])          # another product type
    write_day(swot / "2026-11-21" / "ocn", "wv", [])
    write_day(swot, "2026-11-21", [])                         # old layout, same day: common wins
    write_day(swot, "2026-11-22", [])                         # old layout only
    days = find_days(swot, "2026-11-19", "2026-11-22")
    assert [d.relative_to(swot).as_posix() for d in days] == [
        "2026-11-19/ocn/wv", "2026-11-21/ocn/wv", "2026-11-22"]
    assert [d.relative_to(swot).as_posix()
            for d in find_days(swot, "2026-11-19", "2026-11-22", run=("ocn", "iw"),
                               legacy=False)] == ["2026-11-19/ocn/iw"]


def test_slc_twin_found_by_acquisition_not_crc(tmp_path):
    ocn_dir = make_archive(tmp_path / "arch")
    assert slc_safe_dir(ocn_dir).name == f"{SLC}.SAFE"


def test_slc_twin_missing_is_none(tmp_path):
    assert slc_safe_dir(make_archive(tmp_path / "arch", with_slc=False)) is None


def test_centroid_across_antimeridian():
    ring = [[179.9, 10.0], [-179.9, 10.0], [-179.9, 11.0], [179.9, 11.0], [179.9, 10.0]]
    lon, lat = centroid(ring)
    assert abs(abs(lon) - 180.0) < 0.2 and lat == 10.5


def test_centroid_of_a_multipolygon_split_at_the_antimeridian():
    from soba_reference_repo.coalign.sar import footprint_centroid
    west = [[[-179.9, -55.0], [-180.0, -55.0], [-180.0, -55.2], [-179.9, -55.2], [-179.9, -55.0]]]
    east = [[[179.6, -55.0], [180.0, -55.0], [180.0, -55.2], [179.6, -55.2], [179.6, -55.0]]]
    lon, lat = footprint_centroid({"type": "MultiPolygon", "coordinates": [west, east]})
    assert 179.0 < abs(lon) <= 180.0 and abs(lat + 55.1) < 0.05
    polygon = {"type": "Polygon", "coordinates": east}
    lon, lat = footprint_centroid(polygon)
    assert abs(lon - 179.8) < 0.1 and abs(lat + 55.1) < 0.05


def test_item_rows_spec_values(tmp_path):
    ocn_dir = make_archive(tmp_path / "arch")
    rows, skipped = item_rows(make_item(ocn_dir), reference_for("KNMI_HSCAT_HY2B_25KM"))
    (row,) = rows
    assert skipped == ""
    assert row["primary_key"] == f"{SLC}.SAFE:WV_001_-72.3300_-66.2000"
    assert row["sar_safe_slc"] == f"S1C_WV_SLC__1S/2026/323/{SLC}.SAFE:WV_001"
    assert row["sar_safe_ocn"] == f"S1C_WV_OCN__2S/2026/323/{OCN}.SAFE:WV_001"
    stem = "20261119t003214-20261119t003217-010398-014c82-001"
    assert row["sar_path_ocn"].endswith(f"wv1-ocn-vv-{stem}.nc")
    assert row["sar_path_slc"].endswith(f"{SLC}.SAFE/measurement/s1c-wv1-slc-vv-{stem}.tiff")
    assert (row["windspeed_scat"], row["winddirection_scat"]) == (11.94, 167.5)
    assert not [c for c in row if c.startswith("ref_param")]
    assert row["ref_flag"] == 0.0 and row["ref_id"].endswith("_l2.nc")
    assert str(row["sar_time"]) == "2026-11-19 00:32:14"
    assert str(row["ref_time"]) == "2026-11-19 00:28:01"
    assert (row["_satellite"], row["_polarization"], row["_product"]) == (
        "S1C", "SV", "KNMI-HSCAT-HY2-25km")


def test_item_without_slc_is_dropped_and_counted(tmp_path):
    ocn_dir = make_archive(tmp_path / "arch", with_slc=False)
    rows, skipped = item_rows(make_item(ocn_dir), reference_for("KNMI_HSCAT_HY2B_25KM"))
    assert rows == [] and skipped == "no_slc"


@pytest.mark.parametrize("field,value,reason", [
    ("mode", "IW", "mode"), ("product_type", "SLC", "product_type")])
def test_other_modes_and_product_types_are_skipped(tmp_path, field, value, reason):
    ocn_dir = make_archive(tmp_path / "arch")
    item = make_item(ocn_dir, **{field: value})
    assert item_rows(item, reference_for("KNMI_HSCAT_HY2B_25KM")) == ([], reason)


def test_ref_params_are_named_per_dataset_and_only_what_is_needed():
    """Spec: ref_param[_1..N] is named after the dataset, e.g. windspeed_scat."""
    names = {k: [c for c, _ in r.params] for k, r in REFERENCES.items()}
    for product, columns_ in names.items():
        if "ASCAT" in product or "HSCAT" in product:
            assert columns_ == ["windspeed_scat", "winddirection_scat"], product
    assert names["CMEMS_WAV_L3_ALTI_VAVH"] == ["swh_alti"]
    assert names["ESACCI_SEASTATE_L2P_V5"] == ["swh_alti"]
    assert names["PODAAC_SWOT_KARIN_L2_WINDWAVE_D0"] == ["swh_swot"]
    assert reference_for("CMEMS_WAV_L3_ALTI_VAVH").params == (("swh_alti", "VAVH"),)


def test_altimeter_has_no_flag_and_swot_is_named_by_release():
    """Spec 1.5.0 (#53): PODAAC-SWOT-KARIN-L2-WINDWAVE-<release>, whatever the processing."""
    assert reference_for("CMEMS_WAV_L3_ALTI_VAVH").flag is None
    d0 = reference_for("PODAAC_SWOT_KARIN_L2_WINDWAVE_D0")
    assert product_name(d0, "PID0_01") == product_name(d0, "PGD0_02") == (
        "PODAAC-SWOT-KARIN-L2-WINDWAVE-D0")
    d2 = reference_for("PODAAC_SWOT_KARIN_L2_WINDWAVE_D2")
    assert product_name(d2, "PID2_01") == "PODAAC-SWOT-KARIN-L2-WINDWAVE-D2"
    with pytest.raises(ValueError, match="no catalogue mapping"):
        reference_for("NOT_A_PRODUCT")


def test_every_mapped_product_fits_the_column_builder():
    for reference in REFERENCES.values():
        names = [column for column, _ in reference.params]
        assert len(columns(names)) == len(set(columns(names)))


def test_check_catalogue_fails_on_duplicate_keys_and_mandatory_nulls(tmp_path):
    ocn_dir = make_archive(tmp_path / "arch")
    rows, _ = item_rows(make_item(ocn_dir, points=[point(-72.33, -66.2, wind_speed=1.0)]),
                        reference_for("KNMI_HSCAT_HY2B_25KM"))
    import pandas as pd
    frame = pd.DataFrame(rows)
    names = ["windspeed_scat", "winddirection_scat"]
    assert check_catalogue(frame, names)["rows"] == 1
    with pytest.raises(ValueError, match="duplicated primary_key"):
        check_catalogue(pd.concat([frame, frame]), names)
    broken = frame.copy()
    broken["ref_id"] = None
    with pytest.raises(ValueError, match="mandatory"):
        check_catalogue(broken, names)


def _tree(tmp_path):
    arch = tmp_path / "arch"
    ocn_c = make_archive(arch)
    ocn_d = make_archive(arch, ocn=OCN.replace("S1C", "S1D"), slc=SLC.replace("S1C", "S1D"))
    ocn_none = make_archive(arch, ocn=OCN.replace("010398", "010399"), with_slc=False)
    product = tmp_path / "stac" / "scat" / "hscat_hy-2b_25km"
    two_points = [point(-72.33, -66.2, wind_speed=1.0), point(-72.31, -66.19, wind_speed=2.0)]
    write_day(product, "2026-11-19", [
        make_item(ocn_c, 1, two_points),
        make_item(ocn_c, 2),
        make_item(ocn_d, 1, platform="sentinel-1d"),
        make_item(ocn_none, 1),
    ], layout="ocn/wv")
    return product


def test_build_catalogues_one_file_per_satellite_and_passes_the_validator(tmp_path):
    product = _tree(tmp_path)
    paths, counts = build_catalogues([product], "2026-11-19", "2026-11-19", tmp_path / "out",
                                     production="20261126", slc_lookup=slc_safe_dir)
    names = sorted(p.name for p in paths)
    assert names == [
        "S1C_coaligned_catalogue_WV_20261119_20261119_20261126_SV_KNMI-HSCAT-HY2-25km_0.1.parquet",
        "S1D_coaligned_catalogue_WV_20261119_20261119_20261126_SV_KNMI-HSCAT-HY2-25km_0.1.parquet",
    ]
    assert counts == {"items": 4, "skipped_no_slc": 1, "rows": 4}
    s1c = pq.read_table(paths[0])
    assert s1c.num_rows == 3
    assert s1c.column_names == columns(["windspeed_scat", "winddirection_scat"])
    meta = {k.decode(): v.decode() for k, v in s1c.schema.metadata.items()}
    assert meta["satellite"] == "S1C" and meta["sar_mode"] == "WV" and meta["version"] == "0.1"
    for path in paths:
        report = validate_dataset(path, "catalogue")
        assert not [c for c in report.checks if c[0] == "FAIL"], report.checks


def test_build_catalogues_refuses_to_overwrite(tmp_path):
    product = _tree(tmp_path)
    args = ([product], "2026-11-19", "2026-11-19", tmp_path / "out")
    build_catalogues(*args, production="20261126")
    with pytest.raises(FileExistsError):
        build_catalogues(*args, production="20261126")


def test_satellite_filter_and_empty_range(tmp_path):
    product = _tree(tmp_path)
    paths, _ = build_catalogues([product], "2026-11-19", "2026-11-19", tmp_path / "o1",
                                satellites=["S1D"], production="20261126")
    assert [p.name[:3] for p in paths] == ["S1D"]
    frames, counts, _ = collect([product], "2030-01-01", "2030-01-02")
    assert frames == {} and not counts


def test_catalogue_name_matches_spec_pattern():
    assert catalogue_name("S1C", "SV", "CMEMS-WAV-L3-ALTI-TAC", "20260830", "20260831",
                          "20261126", "0.1") == (
        "S1C_coaligned_catalogue_WV_20260830_20260831_20261126_SV_CMEMS-WAV-L3-ALTI-TAC_0.1.parquet")


def test_column_types_are_stable_across_products(tmp_path):
    """ref_flag is an integer bit mask everywhere; no column gets Arrow's null type."""
    import pyarrow as pa

    ocn_dir = make_archive(tmp_path / "arch")
    product = tmp_path / "stac" / "scat" / "hscat_hy-2b_25km"
    write_day(product, "2026-11-19",
              [make_item(ocn_dir, 1, [point(-72.33, -66.2, wind_speed=1.0, quality_flag=2048.0)])])
    paths, _ = build_catalogues([product], "2026-11-19", "2026-11-19", tmp_path / "out",
                                production="20261126")
    schema = pq.read_schema(paths[0])
    assert schema.field("ref_flag").type == pa.int64()
    assert pq.read_table(paths[0]).column("ref_flag").to_pylist() == [2048]
    assert not [f.name for f in schema if pa.types.is_null(f.type)]
    assert schema.field("legacy_usage").type == pa.string()
    assert schema.field("class_1").type == pa.string()
    assert schema.field("pos_1").type == pa.float64()


def test_a_non_integer_flag_is_rejected(tmp_path):
    ocn_dir = make_archive(tmp_path / "arch")
    item = make_item(ocn_dir, 1, [point(-72.33, -66.2, wind_speed=1.0, quality_flag=0.5)])
    with pytest.raises(ValueError, match="ref_flag"):
        item_rows(item, reference_for("KNMI_HSCAT_HY2B_25KM"))


def test_altimeter_ref_flag_is_null_and_typed(tmp_path):
    import pyarrow as pa

    ocn_dir = make_archive(tmp_path / "arch")
    product = tmp_path / "stac" / "alti" / "cmems"
    write_day(product, "2026-11-19", [make_item(ocn_dir, 1, [point(-72.33, -66.2, VAVH=2.5)])],
              reference_product="CMEMS_WAV_L3_ALTI_VAVH")
    paths, _ = build_catalogues([product], "2026-11-19", "2026-11-19", tmp_path / "out",
                                production="20261126")
    table = pq.read_table(paths[0])
    assert table.column("ref_flag").to_pylist() == [None]
    assert table.schema.field("ref_flag").type == pa.int64()
    assert table.column("swh_alti").to_pylist() == [2.5]
