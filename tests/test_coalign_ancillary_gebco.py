"""GEBCO bathymetry: nearest cell, depth = -elevation, rows read once and cached."""

import numpy as np
import pyarrow.parquet as pq
import pytest
from coalign_fixtures import make_archive, make_item, point, write_day
from netCDF4 import Dataset
from test_coalign_iw import GRD, item
from test_coalign_iw import point as iw_point
from test_coalign_iw import write_day as write_iw_day

from soba_reference_repo.coalign import iw
from soba_reference_repo.coalign.ancillary import gebco, make
from soba_reference_repo.coalign.ancillary.gebco import DEPTH, Gebco, gebco_file
from soba_reference_repo.coalign.catalogue import build_catalogues
from soba_reference_repo.dataset_validator import validate_dataset

STEP = 0.25
LATS = np.arange(-70.0 + STEP / 2, -60.0, STEP)     # cell centres, ascending like GEBCO
LONS = np.arange(-80.0 + STEP / 2, -65.0, STEP)


def write_gebco(root, elevation=-1000.0, name="GEBCO_2019.nc", unit="m"):
    """A small grid, chunked one latitude row per chunk like the real file."""
    root.mkdir(parents=True, exist_ok=True)
    path = root / name
    with Dataset(path, "w") as data:
        data.createDimension("lat", len(LATS))
        data.createDimension("lon", len(LONS))
        data.createVariable("lat", "f8", ("lat",))[:] = LATS
        data.createVariable("lon", "f8", ("lon",))[:] = LONS
        var = data.createVariable("elevation", "f4", ("lat", "lon"), zlib=True,
                                  chunksizes=(1, len(LONS)))
        var.units = unit
        var[:] = np.full((len(LATS), len(LONS)), elevation, dtype="f4")
    return path


def rows_at(*places):
    return [{"ref_lon": lon, "ref_lat": lat} for lon, lat in places]


def test_depth_is_minus_elevation_at_the_nearest_cell(tmp_path):
    path = write_gebco(tmp_path)
    with Dataset(path, "a") as data:
        data["elevation"][10, 20] = -3500.0      # deep cell
        data["elevation"][10, 21] = 12.0         # land
    lat, lon = float(LATS[10]), float(LONS[20])
    rows = rows_at((lon + 0.1, lat - 0.1), (lon + 0.2, lat), (lon + 0.3, lat + 0.3))
    source = Gebco(tmp_path)
    source.fill(rows)
    assert [r[DEPTH] for r in rows] == [pytest.approx(3500.0), pytest.approx(-12.0),
                                        pytest.approx(1000.0)]


def test_rows_are_read_once_and_cached_across_batches(tmp_path, monkeypatch):
    write_gebco(tmp_path)
    source = Gebco(tmp_path)
    lat = float(LATS[5])
    source.fill(rows_at(*[(float(lon), lat) for lon in LONS[:30]]))     # one latitude row
    assert source.counts()["gebco_rows_read"] == 1
    source.fill(rows_at((float(LONS[3]), lat), (float(LONS[4]), float(LATS[6]))))
    assert source.counts()["gebco_rows_read"] == 2                      # only the new row
    monkeypatch.setattr(gebco, "ROW_CACHE", 1)
    source.fill(rows_at((float(LONS[3]), float(LATS[7])), (float(LONS[3]), lat)))
    assert source.counts()["gebco_rows_read"] == 3                      # row 5 still cached


def test_off_grid_points_give_counted_nulls(tmp_path):
    write_gebco(tmp_path)
    rows = rows_at((-72.0, 10.0), (100.0, -65.0), (-72.0, -65.0))
    source = Gebco(tmp_path)
    source.fill(rows)
    assert [r[DEPTH] for r in rows] == [None, None, pytest.approx(1000.0)]
    assert source.counts()["no_bathymetry"] == 2


def test_the_folder_must_hold_one_grid_in_metres(tmp_path):
    with pytest.raises(ValueError, match="found 0"):
        Gebco(tmp_path)
    write_gebco(tmp_path)
    write_gebco(tmp_path, name="GEBCO_2024.nc")
    with pytest.raises(ValueError, match="found 2"):
        gebco_file(tmp_path)
    feet = tmp_path / "feet"
    write_gebco(feet, unit="ft")
    with pytest.raises(ValueError, match="ft"):
        Gebco(feet).fill(rows_at((-72.0, -65.0)))


def test_make_builds_the_reader_named_in_the_recipe(tmp_path):
    write_gebco(tmp_path)
    assert isinstance(make("gebco", tmp_path), Gebco)


def test_wv_catalogue_with_gebco_validates(tmp_path):
    ocn_dir = make_archive(tmp_path / "arch")
    product = tmp_path / "stac" / "scat" / "hscat_hy-2b_25km"
    write_day(product, "2026-11-19", [make_item(ocn_dir, 1, [point(-72.33, -66.2)])])
    write_gebco(tmp_path / "gebco")
    (path,), counts = build_catalogues(
        [product], "2026-11-19", "2026-11-19", tmp_path / "out", production="20261127",
        ancillary_paths={"gebco": tmp_path / "gebco"})
    table = pq.read_table(path)
    assert table.column_names[-1] == DEPTH and table[DEPTH][0].as_py() == pytest.approx(1000.0)
    meta = {k.decode(): v.decode() for k, v in table.schema.metadata.items()}
    assert "GEBCO_2019" in meta["source ancillary datasets"]
    assert counts["no_bathymetry"] == 0
    assert validate_dataset(path, "catalogue").ok


def test_iw_catalogue_fills_the_spec_bathymetry_column(tmp_path):
    collection = tmp_path / "swot" / "d2"
    write_iw_day(collection, "2026-11-19", [item(GRD, [iw_point(-72.33, -66.2)])])
    write_gebco(tmp_path / "gebco")
    (path,), _ = build_catalogues(
        [collection], "2026-11-19", "2026-11-19", tmp_path / "out", production="20261127",
        ancillary_paths={"gebco": tmp_path / "gebco"}, mode=iw.IW())
    table = pq.read_table(path)
    assert table.column_names.count(DEPTH) == 1
    assert table[DEPTH][0].as_py() == pytest.approx(1000.0)
    meta = {k.decode(): v.decode() for k, v in table.schema.metadata.items()}
    assert DEPTH not in meta["ancillary columns not filled"]
