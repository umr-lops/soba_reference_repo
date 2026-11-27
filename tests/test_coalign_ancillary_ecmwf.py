"""ECMWF operational 10 m wind: nearest hour, nearest cell, speed and 'to' direction."""

from datetime import datetime

import numpy as np
import pyarrow.parquet as pq
import pytest
from coalign_fixtures import make_archive, make_item, point, write_day
from netCDF4 import Dataset

from soba_reference_repo.coalign.ancillary import make
from soba_reference_repo.coalign.ancillary.ecmwf import (
    DIRECTION, SPEED, Ecmwf, hour_of, speed_direction, wind_file,
)
from soba_reference_repo.coalign.catalogue import build_catalogues, collect
from soba_reference_repo.coalign.sar import slc_safe_dir
from soba_reference_repo.dataset_validator import validate_dataset

HOUR = datetime(2026, 11, 19, 0, 0)
LATS = np.round(np.arange(-65.0, -67.01, -0.1), 1)     # descending like the real files
LONS = np.round(np.arange(286.0, 288.01, 0.1), 1)      # 0..360 axis: -74 .. -72


def write_wind(root, hour=HOUR, u=3.0, v=4.0, fill=None, unit="m s-1", valid=None):
    """One hourly file with a constant wind (u, v), optionally a fill value at one cell."""
    path = wind_file(root, hour)
    path.parent.mkdir(parents=True, exist_ok=True)
    valid = valid or hour
    with Dataset(path, "w") as data:
        data.createDimension("time", 1)
        data.createDimension("Latitude", len(LATS))
        data.createDimension("Longitude", len(LONS))
        t = data.createVariable("time", "i4", ("time",))
        t.units = "hours since 1990-01-01 00:00:00"
        t[:] = int((valid - datetime(1990, 1, 1)).total_seconds() // 3600)
        data.createVariable("Latitude", "f4", ("Latitude",))[:] = LATS
        data.createVariable("Longitude", "f4", ("Longitude",))[:] = LONS
        for name, value in (("10U", u), ("10V", v)):
            var = data.createVariable(name, "f4", ("time", "Latitude", "Longitude"),
                                      fill_value=9999.0)
            var.units = unit
            grid = np.full((1, len(LATS), len(LONS)), value, dtype="f4")
            if fill is not None:
                grid[0, fill[0], fill[1]] = 9999.0
            var[:] = grid
    return path


def rows_at(*times_and_places):
    return [{"ref_time": t, "ref_lon": lon, "ref_lat": lat} for t, lon, lat in times_and_places]


def test_file_name_and_hour_rounding():
    assert hour_of(datetime(2026, 11, 19, 0, 29, 59)) == datetime(2026, 11, 19, 0)
    assert hour_of(datetime(2026, 11, 19, 0, 30)) == datetime(2026, 11, 19, 1)
    assert hour_of(datetime(2026, 11, 19, 23, 40)) == datetime(2026, 11, 20, 0)
    assert wind_file("/r", datetime(2026, 11, 19, 1)).as_posix() == (
        "/r/2026/323/ECMWF_FORECAST_0100_202611190100_10U_10V.nc")


@pytest.mark.parametrize("u,v,speed,direction", [
    (0.0, 5.0, 5.0, 0.0),       # blowing to the north
    (5.0, 0.0, 5.0, 90.0),      # to the east
    (0.0, -5.0, 5.0, 180.0),    # to the south
    (-5.0, 0.0, 5.0, 270.0),    # to the west
    (3.0, 4.0, 5.0, 36.86989765),
])
def test_speed_and_to_direction(u, v, speed, direction):
    s, d = speed_direction(np.array([u]), np.array([v]))
    assert s[0] == pytest.approx(speed) and d[0] == pytest.approx(direction)


def test_calm_air_has_a_speed_and_no_direction():
    s, d = speed_direction(np.array([0.0]), np.array([0.0]))
    assert s[0] == 0.0 and np.isnan(d[0])


def test_values_come_from_the_nearest_hour_and_cell(tmp_path):
    write_wind(tmp_path, HOUR, u=3.0, v=4.0)
    write_wind(tmp_path, datetime(2026, 11, 19, 1), u=-6.0, v=8.0)
    rows = rows_at((datetime(2026, 11, 19, 0, 20), -72.33, -66.2),
                   (datetime(2026, 11, 19, 0, 40), -72.33, -66.2))
    source = Ecmwf(tmp_path)
    source.fill(rows)
    assert (rows[0][SPEED], rows[0][DIRECTION]) == (pytest.approx(5.0), pytest.approx(36.869898))
    assert (rows[1][SPEED], rows[1][DIRECTION]) == (pytest.approx(10.0), pytest.approx(323.130102))
    assert source.counts() == {"no_ecmwf_wind": 0, "missing_ecmwf_files": 0}


def test_each_file_is_opened_once_per_batch(tmp_path, monkeypatch):
    import netCDF4
    write_wind(tmp_path)
    opened, real = [], netCDF4.Dataset
    monkeypatch.setattr(netCDF4, "Dataset", lambda p, *a, **k: opened.append(p) or real(p, *a, **k))
    Ecmwf(tmp_path).fill(rows_at(*[(datetime(2026, 11, 19, 0, m), -72.3, -66.2) for m in range(9)]))
    assert len(opened) == 1


def test_nearest_cell_not_interpolation(tmp_path):
    path = write_wind(tmp_path)
    with Dataset(path, "a") as data:                     # one cell blows to the north at 9 m/s
        data["10U"][0, 12, 10] = 0.0
        data["10V"][0, 12, 10] = 9.0
    lat, lon = float(LATS[12]), float(LONS[10]) - 360.0
    rows = rows_at((HOUR, lon + 0.04, lat - 0.04), (HOUR, lon + 0.06, lat))
    Ecmwf(tmp_path).fill(rows)
    assert rows[0][SPEED] == pytest.approx(9.0) and rows[0][DIRECTION] == pytest.approx(0.0)
    assert rows[1][SPEED] == pytest.approx(5.0)          # 0.06 east is the next cell over


def test_missing_file_gives_nulls_and_is_counted_not_raised(tmp_path):
    write_wind(tmp_path)
    rows = rows_at((HOUR, -72.3, -66.2), (datetime(2026, 11, 19, 5), -72.3, -66.2))
    source = Ecmwf(tmp_path)
    source.fill(rows)
    assert rows[0][SPEED] is not None and rows[1][SPEED] is None and rows[1][DIRECTION] is None
    assert source.counts() == {"no_ecmwf_wind": 1, "missing_ecmwf_files": 1}


def test_fill_value_and_points_off_the_grid_give_nulls(tmp_path):
    write_wind(tmp_path, fill=(12, 20))
    lat, lon = float(LATS[12]), float(LONS[20]) - 360.0
    rows = rows_at((HOUR, lon, lat), (HOUR, lon, 10.0), (HOUR, 100.0, lat))
    source = Ecmwf(tmp_path)
    source.fill(rows)
    assert [r[SPEED] for r in rows] == [None, None, None]
    assert source.counts()["no_ecmwf_wind"] == 3


def test_a_file_valid_at_another_time_or_in_other_units_raises(tmp_path):
    write_wind(tmp_path, valid=datetime(2026, 11, 19, 6))
    with pytest.raises(ValueError, match="valid at"):
        Ecmwf(tmp_path).fill(rows_at((HOUR, -72.3, -66.2)))
    other = tmp_path / "knots"
    write_wind(other, unit="knots")
    with pytest.raises(ValueError, match="knots"):
        Ecmwf(other).fill(rows_at((HOUR, -72.3, -66.2)))


def test_make_builds_the_reader_named_in_the_recipe(tmp_path):
    assert isinstance(make("ecmwf", tmp_path), Ecmwf)


def _stac(tmp_path):
    ocn_dir = make_archive(tmp_path / "arch")
    product = tmp_path / "stac" / "scat" / "hscat_hy-2b_25km"
    write_day(product, "2026-11-19", [
        make_item(ocn_dir, 1, [point(-72.33, -66.2, wind_speed=11.0, ref_time_utc=None) | {
            "ref_time_utc": "2026-11-19T00:28:01.000000000Z"}]),
        make_item(ocn_dir, 2, [point(-72.31, -66.19, wind_speed=9.0) | {
            "ref_time_utc": "2026-11-19T05:10:00.000000000Z"}]),
    ])
    return product


def test_collect_adds_the_two_wind_columns_and_counts_the_gap(tmp_path):
    write_wind(tmp_path / "wind")
    frames, counts, _ = collect([_stac(tmp_path)], "2026-11-19", "2026-11-19", slc_safe_dir,
                                sources=[Ecmwf(tmp_path / "wind")])
    frame = frames[("S1C", "SV", "KNMI-HSCAT-HY2-25km")].sort_values("ref_time")
    assert frame[SPEED].round(3).tolist()[0] == 5.0 and np.isnan(frame[SPEED].iloc[1])
    assert counts["no_ecmwf_wind"] == 1 and counts["missing_ecmwf_files"] == 1


def test_catalogue_file_has_the_columns_attribute_and_validates(tmp_path):
    write_wind(tmp_path / "wind")
    (path,), counts = build_catalogues(
        [_stac(tmp_path)], "2026-11-19", "2026-11-19", tmp_path / "out", production="20261126",
        ancillary_paths={"ecmwf": tmp_path / "wind"})
    table = pq.read_table(path)
    assert table.column_names[-2:] == [SPEED, DIRECTION]
    assert table.schema.field(SPEED).type == "double"
    meta = {k.decode(): v.decode() for k, v in table.schema.metadata.items()}
    assert "ECMWF" in meta["source ancillary datasets"]
    assert meta["ancillary path ecmwf"] == str(tmp_path / "wind")
    assert counts["no_ecmwf_wind"] == 1
    assert validate_dataset(path, "catalogue").ok


def test_two_sources_together_keep_every_column_and_attribute(tmp_path):
    from test_coalign_ancillary_tengeop import write_summary
    write_wind(tmp_path / "wind")
    write_summary(tmp_path / "dl")
    (path,), _ = build_catalogues(
        [_stac(tmp_path)], "2026-11-19", "2026-11-19", tmp_path / "out", production="20261126",
        ancillary_paths={"tengeop": tmp_path / "dl", "ecmwf": tmp_path / "wind"})
    table = pq.read_table(path)
    assert {"class_1", "pos_1", SPEED, DIRECTION} <= set(table.column_names)
    meta = {k.decode(): v.decode() for k, v in table.schema.metadata.items()}
    assert "TenGeoP" in meta["source ancillary datasets"] and "ECMWF" in meta[
        "source ancillary datasets"]
    assert validate_dataset(path, "catalogue").ok


def test_without_ecmwf_the_catalogue_has_no_wind_columns(tmp_path):
    (path,), _ = build_catalogues([_stac(tmp_path)], "2026-11-19", "2026-11-19",
                                  tmp_path / "out", production="20261126")
    assert SPEED not in pq.read_schema(path).names
