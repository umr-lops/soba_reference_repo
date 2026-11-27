"""IW co-aligned catalogues from synthetic soba_matchups GRD days (no cdsodatacli, no archive)."""

import json
from datetime import datetime

import pyarrow.parquet as pq
import pytest

from soba_reference_repo.coalign import iw
from soba_reference_repo.coalign.recipe import run_recipe

GRD = "S1C_IW_GRDH_1SDV_20261122T162138_20261122T162203_010452_014E3B_D026.SAFE"
GRD_HH = "S1C_IW_GRDH_1SDH_20261122T194200_20261122T194225_010454_014E4B_501B.SAFE"
OCN = "S1C_IW_OCN__2SDV_20261122T162138_20261122T162203_010452_014E3B_0CDA.SAFE"
SLC = "S1C_IW_SLC__1SDV_20261122T162137_20261122T162204_010452_014E3B_411B.SAFE"
LABEL = "PODAAC_SWOT_KARIN_L2_WINDWAVE_D2"
PRODUCT = "PODAAC-SWOT-KARIN-L2-WINDWAVE-D2"


def point(lon, lat, **fields):
    base = {
        "ref_time_utc": "2026-11-22T17:05:01.123456789Z", "ref_lon": lon, "ref_lat": lat,
        "swh_karin": 2.5, "swh_karin_qual": 256, "swh_karin_uncert": 0.3,
        "wind_speed_karin": 9.2, "wind_speed_karin_qual": 268435712, "wind_speed_karin_2": 5.4,
        "wind_speed_karin_2_qual": 256, "dynamic_ice_flag": 0, "rain_flag": 0,
        "sar_distance_to_coast": 53.0, "sar_incidence_angle": 30.2, "sar_elevation_angle": 26.8,
        "sar_ground_heading": 347.0, "sar_ground_heading_uncertainty": 3.8,
        "sar_distance_to_along_track_edge": 41230.5, "sar_distance_to_cross_track_edge": 12874.2,
        "delta_diff_time_minutes": 42.1,
    }
    base.update(fields)
    return base


def item(safe, points, product_type="GRD", mode="IW"):
    return {
        "type": "Feature",
        "properties": {
            "sarmatchup:sar_instrument_mode": mode, "sarmatchup:sar_product_type": product_type,
            "sarmatchup:sar_platform": "sentinel-1c", "sarmatchup:sar_safe": safe,
            "sarmatchup:sar_orbit_direction": "ascending",
            "sarmatchup:matchups": [{
                "reference_file": "SWOT_L2_LR_SSH_WindWave_059_288_x_PID2_01.nc",
                "reference_source": "SWOT KaRIn", "reference_version": "PID2_01",
                "points": points,
            }],
        },
    }


def write_day(collection, day, items, label=LABEL):
    run = collection / day / "grd" / "iw"
    (run / "items").mkdir(parents=True)
    (run / "manifest.json").write_text(json.dumps({"reference_product": label}))
    for k, it in enumerate(items):
        (run / "items" / f"{k:03d}.json").write_text(json.dumps(it))
    return run


def fake_matcher(names, target):
    twin = {"OCN_": OCN, "SLC_": SLC}[target]
    return {n: (twin if n == GRD else None) for n in names}


def test_catalogue_rows_twins_paths_and_names(tmp_path):
    collection = tmp_path / "stac" / "swot" / "d2"
    write_day(collection, "2026-11-22", [
        item(GRD, [point(36.17855, 69.64214), point(36.2, 69.7, sar_incidence_angle=None)]),
        item(GRD_HH, [point(-10.0, 50.0)]),
        item(OCN, [point(1.0, 1.0)], product_type="OCN"),  # not a GRD Item: skipped
    ])
    # Only the GRD SAFE is on disk (MPC layout), its OCN twin in the provider tree.
    grd_dir = tmp_path / "l1/sentinel-1c/L1/IW/S1C_IW_GRDH_1S/2026/326" / GRD
    grd_dir.mkdir(parents=True)
    ocn_dir = tmp_path / "l2/sentinel-1c/c-band_sar/s1c_iw_ocn__2s/2026/326" / OCN
    ocn_dir.mkdir(parents=True)
    archives = iw.Archives(tmp_path / "l2", [tmp_path / "l1"])
    paths, counts = iw.build_iw_catalogues(
        [collection], "2026-11-22", "2026-11-22", tmp_path / "out", "0.1", "20261127",
        matcher=fake_matcher, archives=archives)
    assert [p.name for p in paths] == [
        f"S1C_coaligned_catalogue_IW_20261122_20261122_20261127_DH_{PRODUCT}_0.1.parquet",
        f"S1C_coaligned_catalogue_IW_20261122_20261122_20261127_DV_{PRODUCT}_0.1.parquet",
    ]
    assert counts["rows"] == 3 and counts["skipped_product_type"] == 1
    assert counts["grd_without_ocn"] == counts["grd_without_slc"] == 1
    table = pq.read_table(paths[1])
    rows = table.to_pylist()
    first = rows[0]
    assert first["primary_key"] == f"{GRD}_36.1786_69.6421"
    assert first["sar_time"] == datetime(2026, 11, 22, 16, 21, 38)
    assert first["ref_time"] == datetime(2026, 11, 22, 17, 5, 1)
    assert (first["sar_safe_grd"], first["sar_safe_ocn"]) == (GRD, OCN)
    assert first["sar_safe_slc"] == f"S1C_IW_SLC__1S/2026/326/{SLC}"
    assert first["sar_path_grd"] == str(grd_dir) and first["sar_path_ocn"] == str(ocn_dir)
    assert first["sar_path_slc"] is None  # not on disk: empty, the row is kept
    assert (first["swh_swot"], first["windspeed_swot"], first["windspeed_2_swot"]) == (
        2.5, 9.2, 5.4)
    assert first["ref_flag"] == 256 and first["wind_speed_karin_qual"] == 268435712
    assert first["ref_distance_to_coast"] == 53.0
    assert first["sar_distance_to_along_track_edge"] == 41230.5
    assert all(first[name] is None for name in iw.ANCILLARY)
    assert rows[1]["sar_incidence_angle"] is None
    assert "sar_lat" not in table.column_names and "sar_lon" not in table.column_names
    meta = {k.decode(): v.decode() for k, v in table.schema.metadata.items()}
    assert meta["sar_mode"] == "IW" and meta["ref_product"] == PRODUCT
    assert meta["ancillary columns not filled"].startswith("sea_ice_concentration")
    assert "cdsodatacli" in meta["triplets"]
    # The HH-polarized GRD has no twin: its twin columns are empty.
    other = pq.read_table(paths[0]).to_pylist()[0]
    assert other["sar_safe_ocn"] is None and other["sar_safe_slc"] is None


def test_without_matcher_or_archives_a_small_test_still_builds(tmp_path):
    collection = tmp_path / "swot" / "d0"
    write_day(collection, "2026-11-10", [item(GRD, [point(1.0, 2.0), point(1.0, 2.0)])],
              label="PODAAC_SWOT_KARIN_L2_WINDWAVE_D0")
    write_day(collection, "2026-11-11", [item(GRD, [point(3.0, 4.0)])],
              label="PODAAC_SWOT_KARIN_L2_WINDWAVE_D0")
    paths, counts = iw.build_iw_catalogues([collection], "2026-11-10", "2026-11-10",
                                           tmp_path / "out", production="20261127")
    assert len(paths) == 1 and "WINDWAVE-D0" in paths[0].name
    assert counts["rows"] == 1 and counts["dropped_duplicate_key"] == 1  # same cell twice
    row = pq.read_table(paths[0]).to_pylist()[0]
    assert row["sar_safe_ocn"] is None and row["sar_path_grd"] is None
    meta = pq.read_schema(paths[0]).metadata
    assert b"not associated" in meta[b"triplets"]
    # An existing catalogue is never overwritten.
    with pytest.raises(FileExistsError):
        iw.build_iw_catalogues([collection], "2026-11-10", "2026-11-10", tmp_path / "out",
                               production="20261127")


def test_unknown_reference_product_fails_loudly(tmp_path):
    collection = tmp_path / "scat" / "hscat"
    write_day(collection, "2026-11-10", [item(GRD, [point(1.0, 2.0)])],
              label="KNMI_HSCAT_HY2B_25KM")
    with pytest.raises(ValueError, match="no IW catalogue mapping"):
        iw.build_iw_catalogues([collection], "2026-11-10", "2026-11-10", tmp_path / "out")


def test_iw_recipe_through_the_coalign_entry_point(tmp_path):
    write_day(tmp_path / "stac" / "swot" / "d2", "2026-11-22", [item(GRD, [point(5.0, 6.0)])])
    recipe = tmp_path / "iw.toml"
    recipe.write_text("""sar_mode = "IW"
stac_root = "stac"
output_dir = "out"
production_date = "20261127"
version = "0.1"
start = "2026-11-22"
end = "2026-11-22"
products = ["swot/d2"]
[archives]
l1 = ["l1"]
""")
    paths, counts = run_recipe(recipe)
    assert counts["rows"] == 1 and paths[0].parent == tmp_path / "out"
    recipe.write_text(recipe.read_text().replace('version = "0.1"', 'version = "1"'))
    with pytest.raises(ValueError, match="X.Y"):
        run_recipe(recipe)


def test_safe_names_and_polarizations():
    assert iw.safe_slc_ref(SLC) == f"S1C_IW_SLC__1S/2026/326/{SLC}"
    assert iw.reference_for(LABEL)[1] == PRODUCT
    partial = GRD.replace("_1SDV_", "_1SVV_")
    rows, _ = iw.item_rows(item(partial, [point(0, 0)]), iw.SWOT, {}, iw.Archives())
    assert rows[0]["_polarization"] == "SV"
