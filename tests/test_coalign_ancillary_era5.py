"""ERA5 reanalysis 10 m wind: daily file, nearest hour, nearest cell, speed and 'to' direction."""

from datetime import datetime, timezone

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
from soba_reference_repo.coalign.ancillary.ecmwf import SPEED as ECMWF_SPEED
from soba_reference_repo.coalign.ancillary.era5 import DIRECTION, SPEED, Era5, era5_file
from soba_reference_repo.coalign.catalogue import build_catalogues
from soba_reference_repo.dataset_validator import validate_dataset

DAY = datetime(2026, 11, 19)
LATS = np.arange(-60.0, -70.01, -0.25)       # descending like the real files
LONS = np.arange(280.0, 295.01, 0.25)       # 0..360 axis: -80 .. -65


def write_era5(root, day=DAY, winds=None, unit="m s**-1", hours=range(24)):
    """One daily file; ``winds`` maps an hour to a constant (u, v) (default (3, 4) every hour)."""
    path = era5_file(root, day)
    path.parent.mkdir(parents=True, exist_ok=True)
    hours = list(hours)
    with Dataset(path, "w") as data:
        data.createDimension("valid_time", len(hours))
        data.createDimension("latitude", len(LATS))
        data.createDimension("longitude", len(LONS))
        t = data.createVariable("valid_time", "i8", ("valid_time",))
        t.units = "seconds since 1970-01-01"
        t[:] = [int(day.replace(hour=h, tzinfo=timezone.utc).timestamp()) for h in hours]
        data.createVariable("latitude", "f8", ("latitude",))[:] = LATS
        data.createVariable("longitude", "f8", ("longitude",))[:] = LONS
        for k, name in enumerate(("u10", "v10")):
            var = data.createVariable(name, "f4", ("valid_time", "latitude", "longitude"),
                                      fill_value=np.float32(np.nan))
            var.units = unit
            grid = np.empty((len(hours), len(LATS), len(LONS)), dtype="f4")
            for i, hour in enumerate(hours):
                grid[i] = (winds or {}).get(hour, (3.0, 4.0))[k]
            var[:] = grid
    return path


def rows_at(*times_and_places):
    return [{"ref_time": t, "ref_lon": lon, "ref_lat": lat} for t, lon, lat in times_and_places]


def test_file_name():
    assert era5_file("/r", DAY).as_posix() == "/r/2026/11/era5_single-levels_inst_20261119.nc"


def test_nearest_hour_including_the_next_day_file(tmp_path):
    write_era5(tmp_path, winds={0: (3.0, 4.0), 1: (-6.0, 8.0)})
    write_era5(tmp_path, datetime(2026, 11, 20), winds={0: (0.0, 2.0)})
    rows = rows_at((datetime(2026, 11, 19, 0, 20), -72.3, -66.2),
                   (datetime(2026, 11, 19, 0, 40), -72.3, -66.2),
                   (datetime(2026, 11, 19, 23, 45), -72.3, -66.2))   # 00:00 the next day
    source = Era5(tmp_path)
    source.fill(rows)
    assert (rows[0][SPEED], rows[0][DIRECTION]) == (pytest.approx(5.0), pytest.approx(36.869898))
    assert (rows[1][SPEED], rows[1][DIRECTION]) == (pytest.approx(10.0), pytest.approx(323.130102))
    assert (rows[2][SPEED], rows[2][DIRECTION]) == (pytest.approx(2.0), pytest.approx(0.0))
    assert source.counts() == {"no_era5_wind": 0, "missing_era5_files": 0}


def test_each_day_file_is_opened_once_per_batch(tmp_path, monkeypatch):
    import netCDF4
    write_era5(tmp_path)
    opened, real = [], netCDF4.Dataset
    monkeypatch.setattr(netCDF4, "Dataset", lambda p, *a, **k: opened.append(p) or real(p, *a, **k))
    Era5(tmp_path).fill(rows_at(*[(datetime(2026, 11, 19, h, 5), -72.3, -66.2) for h in range(9)]))
    assert len(opened) == 1


def test_nearest_cell_not_interpolation(tmp_path):
    path = write_era5(tmp_path)
    with Dataset(path, "a") as data:                     # one cell blows to the north at 9 m/s
        data["u10"][0, 12, 10] = 0.0
        data["v10"][0, 12, 10] = 9.0
    lat, lon = float(LATS[12]), float(LONS[10]) - 360.0
    rows = rows_at((DAY, lon + 0.1, lat - 0.1), (DAY, lon + 0.15, lat))
    Era5(tmp_path).fill(rows)
    assert rows[0][SPEED] == pytest.approx(9.0) and rows[0][DIRECTION] == pytest.approx(0.0)
    assert rows[1][SPEED] == pytest.approx(5.0)          # 0.15 east is the next cell over


def test_missing_file_fill_value_and_off_grid_give_counted_nulls(tmp_path):
    path = write_era5(tmp_path)
    with Dataset(path, "a") as data:
        data["u10"][0, 12, 20] = np.nan
    lat, lon = float(LATS[12]), float(LONS[20]) - 360.0
    rows = rows_at((DAY, lon, lat), (DAY, lon, 10.0), (DAY, 100.0, lat),
                   (datetime(2026, 11, 21, 3), -72.3, -66.2))   # no file that day
    source = Era5(tmp_path)
    source.fill(rows)
    assert [r[SPEED] for r in rows] == [None, None, None, None]
    assert source.counts() == {"no_era5_wind": 4, "missing_era5_files": 1}


def test_a_missing_hour_or_other_units_raise(tmp_path):
    write_era5(tmp_path, hours=range(12))
    with pytest.raises(ValueError, match="no field valid at"):
        Era5(tmp_path).fill(rows_at((datetime(2026, 11, 19, 15), -72.3, -66.2)))
    other = tmp_path / "knots"
    write_era5(other, unit="knots")
    with pytest.raises(ValueError, match="knots"):
        Era5(other).fill(rows_at((DAY, -72.3, -66.2)))


def test_make_builds_the_reader_named_in_the_recipe(tmp_path):
    assert isinstance(make("era5", tmp_path), Era5)


def test_wv_catalogue_with_era5_and_ecmwf_validates(tmp_path):
    from test_coalign_ancillary_ecmwf import write_wind
    ocn_dir = make_archive(tmp_path / "arch")
    product = tmp_path / "stac" / "scat" / "hscat_hy-2b_25km"
    write_day(product, "2026-11-19", [make_item(ocn_dir, 1, [point(-72.33, -66.2)])])
    write_era5(tmp_path / "era5")
    write_wind(tmp_path / "ecmwf")
    (path,), counts = build_catalogues(
        [product], "2026-11-19", "2026-11-19", tmp_path / "out", production="20261127",
        ancillary_paths={"ecmwf": tmp_path / "ecmwf", "era5": tmp_path / "era5"})
    table = pq.read_table(path)
    assert {ECMWF_SPEED, SPEED, DIRECTION} <= set(table.column_names)
    assert table[SPEED][0].as_py() == pytest.approx(5.0)
    meta = {k.decode(): v.decode() for k, v in table.schema.metadata.items()}
    assert "ERA5" in meta["source ancillary datasets"]
    assert meta["ancillary path era5"] == str(tmp_path / "era5")
    assert counts["no_era5_wind"] == 0
    assert validate_dataset(path, "catalogue").ok


def test_iw_catalogue_gets_the_era5_columns(tmp_path):
    collection = tmp_path / "swot" / "d2"
    write_iw_day(collection, "2026-11-19", [item(GRD, [iw_point(-72.33, -66.2) | {
        "ref_time_utc": "2026-11-19T00:10:00Z"}])])
    write_era5(tmp_path / "era5")
    (path,), _ = build_catalogues(
        [collection], "2026-11-19", "2026-11-19", tmp_path / "out", production="20261127",
        ancillary_paths={"era5": tmp_path / "era5"}, mode=iw.IW())
    table = pq.read_table(path)
    assert table[SPEED][0].as_py() == pytest.approx(5.0)
    meta = {k.decode(): v.decode() for k, v in table.schema.metadata.items()}
    assert "ERA5" in meta["source ancillary datasets"]
    assert "ERA5" not in meta["ancillary columns not filled"]
