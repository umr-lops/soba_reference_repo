"""IMERG rain rate: the half-hour file containing ref_time, nearest cell, mm/h."""

from datetime import datetime

import numpy as np
import pyarrow.parquet as pq
import pytest
from coalign_fixtures import make_archive, make_item, point, write_day
from netCDF4 import Dataset
from test_coalign_iw import GRD, item
from test_coalign_iw import point as iw_point
from test_coalign_iw import write_day as write_iw_day

from soba_reference_repo.coalign import iw
from soba_reference_repo.coalign.ancillary import make
from soba_reference_repo.coalign.ancillary.imerg import (
    COLUMN, Imerg, half_hour_of, imerg_file,
)
from soba_reference_repo.coalign.catalogue import build_catalogues
from soba_reference_repo.dataset_validator import validate_dataset

START = datetime(2026, 11, 19, 0, 0)
LONS = np.round(np.arange(-79.95, -64.9, 0.1), 2)
LATS = np.round(np.arange(-69.95, -59.9, 0.1), 2)


def write_imerg(root, start=START, rain=1.5, cells=None, unit="mm/hr"):
    """One half-hourly file, ``rain`` everywhere (``cells``: (lon index, lat index) -> value)."""
    end = start.replace(minute=start.minute + 29, second=59)
    minute = start.hour * 60 + start.minute
    folder = root / f"{start:%Y}" / f"{start.timetuple().tm_yday:03d}"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (f"3B-HHR-L.MS.MRG.3IMERG.{start:%Y%m%d}-S{start:%H%M%S}-E{end:%H%M%S}."
                     f"{minute:04d}.V07B.RT-H5")
    field = np.full((1, len(LONS), len(LATS)), rain, dtype="f4")
    for (j, i), value in (cells or {}).items():
        field[0, j, i] = value
    with Dataset(path, "w") as data:
        grid = data.createGroup("Grid")
        grid.createDimension("time", 1)
        grid.createDimension("lon", len(LONS))
        grid.createDimension("lat", len(LATS))
        grid.createVariable("lon", "f4", ("lon",))[:] = LONS
        grid.createVariable("lat", "f4", ("lat",))[:] = LATS
        var = grid.createVariable("precipitation", "f4", ("time", "lon", "lat"),
                                  fill_value=np.float32(-9999.9))
        var.units = unit
        var[:] = field
    return path


def rows_at(*times_and_places):
    return [{"ref_time": t, "ref_lon": lon, "ref_lat": lat} for t, lon, lat in times_and_places]


def test_half_hour_and_file_name(tmp_path):
    assert half_hour_of(datetime(2026, 11, 19, 10, 29, 59)) == datetime(2026, 11, 19, 10, 0)
    assert half_hour_of(datetime(2026, 11, 19, 10, 40)) == datetime(2026, 11, 19, 10, 30)
    assert imerg_file(tmp_path, START) is None
    path = write_imerg(tmp_path, datetime(2026, 11, 19, 10, 30))
    assert imerg_file(tmp_path, datetime(2026, 11, 19, 10, 30)) == path
    assert path.name == "3B-HHR-L.MS.MRG.3IMERG.20261119-S103000-E105959.0630.V07B.RT-H5"
    assert path.parent.name == "323"


def test_the_file_containing_ref_time_not_the_nearest(tmp_path):
    write_imerg(tmp_path, START, rain=1.5)
    write_imerg(tmp_path, datetime(2026, 11, 19, 0, 30), rain=7.0)
    rows = rows_at((datetime(2026, 11, 19, 0, 25), -72.3, -66.2),     # nearer 00:30, reads 00:00
                   (datetime(2026, 11, 19, 0, 35), -72.3, -66.2))
    source = Imerg(tmp_path)
    source.fill(rows)
    assert [r[COLUMN] for r in rows] == [pytest.approx(1.5), pytest.approx(7.0)]
    assert source.counts() == {"no_imerg_rainrate": 0, "missing_imerg_files": 0}


def test_nearest_cell_zero_rain_and_counted_nulls(tmp_path):
    write_imerg(tmp_path, cells={(20, 30): 0.0, (21, 30): -9999.9, (22, 30): -1.0})
    lat = float(LATS[30])
    rows = rows_at((START, float(LONS[20]) + 0.04, lat - 0.04),   # nearest cell: zero rain
                   (START, float(LONS[21]), lat),                  # fill value
                   (START, float(LONS[22]), lat),                  # negative
                   (START, 10.0, lat),                             # off the grid
                   (datetime(2026, 11, 19, 5, 10), -72.3, -66.2))  # no file
    source = Imerg(tmp_path)
    source.fill(rows)
    assert [r[COLUMN] for r in rows] == [0.0, None, None, None, None]
    assert source.counts() == {"no_imerg_rainrate": 4, "missing_imerg_files": 1}


def test_other_units_raise(tmp_path):
    write_imerg(tmp_path, unit="kg m-2 s-1")
    with pytest.raises(ValueError, match="kg m-2 s-1"):
        Imerg(tmp_path).fill(rows_at((START, -72.3, -66.2)))


def test_make_builds_the_reader_named_in_the_recipe(tmp_path):
    assert isinstance(make("imerg", tmp_path), Imerg)


def test_wv_catalogue_with_imerg_validates(tmp_path):
    write_imerg(tmp_path / "imerg")
    ocn_dir = make_archive(tmp_path / "arch")
    product = tmp_path / "stac" / "scat" / "hscat_hy-2b_25km"
    write_day(product, "2026-11-19", [make_item(ocn_dir, 1, [point(-72.33, -66.2) | {
        "ref_time_utc": "2026-11-19T00:10:00Z"}])])
    (path,), counts = build_catalogues(
        [product], "2026-11-19", "2026-11-19", tmp_path / "out", production="20261127",
        ancillary_paths={"imerg": tmp_path / "imerg"})
    table = pq.read_table(path)
    assert table.column_names[-1] == COLUMN and table[COLUMN][0].as_py() == pytest.approx(1.5)
    meta = {k.decode(): v.decode() for k, v in table.schema.metadata.items()}
    assert "IMERG" in meta["source ancillary datasets"]
    assert counts["no_imerg_rainrate"] == 0
    assert validate_dataset(path, "catalogue").ok


def test_iw_catalogue_gets_the_rain_column(tmp_path):
    write_imerg(tmp_path / "imerg")
    collection = tmp_path / "swot" / "d2"
    write_iw_day(collection, "2026-11-19", [item(GRD, [iw_point(-72.33, -66.2) | {
        "ref_time_utc": "2026-11-19T00:10:00Z"}])])
    (path,), _ = build_catalogues(
        [collection], "2026-11-19", "2026-11-19", tmp_path / "out", production="20261127",
        ancillary_paths={"imerg": tmp_path / "imerg"}, mode=iw.IW())
    assert pq.read_table(path)[COLUMN][0].as_py() == pytest.approx(1.5)
