"""WW3 CCI sea state run: monthly file, nearest hour, nearest cell, Hs and Tp = 1 / fp."""

from datetime import datetime, timedelta

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
from soba_reference_repo.coalign.ancillary.ww3 import HS, TP, WW3, ww3_file
from soba_reference_repo.coalign.catalogue import build_catalogues
from soba_reference_repo.dataset_validator import validate_dataset

MONTH = datetime(2026, 11, 1)
LATS = np.arange(-70.0, -60.01, 0.5)        # ascending like the real files
LONS = np.arange(-80.0, -65.01, 0.5)        # -180..180 axis


def write_ww3(root, month=MONTH, fields=None, hours=None, hs_unit="m", fill=None):
    """One monthly file; ``fields`` maps an hour index to (hs, fp) (default (2.5, 0.1))."""
    path = ww3_file(root, month)
    path.parent.mkdir(parents=True, exist_ok=True)
    nxt = (month.replace(day=28) + timedelta(days=4)).replace(day=1)
    hours = hours if hours is not None else int((nxt - month).total_seconds() // 3600)
    with Dataset(path, "w") as data:
        data.createDimension("time", None)
        data.createDimension("latitude", len(LATS))
        data.createDimension("longitude", len(LONS))
        t = data.createVariable("time", "f8", ("time",))
        t.units = "days since 1990-01-01T00:00:00"
        start = (month - datetime(1990, 1, 1)).total_seconds() / 86400
        t[:] = start + np.arange(hours) / 24.0
        data.createVariable("latitude", "f4", ("latitude",))[:] = LATS
        data.createVariable("longitude", "f4", ("longitude",))[:] = LONS
        for name, unit, scale in (("hs", hs_unit, 0.002), ("fp", "s-1", 0.001)):
            var = data.createVariable(name, "i2", ("time", "latitude", "longitude"),
                                      fill_value=np.int16(-32767))
            var.units, var.scale_factor, var.add_offset = unit, np.float32(scale), np.float32(0)
        hs = np.full((hours, len(LATS), len(LONS)), 2.5)
        fp = np.full((hours, len(LATS), len(LONS)), 0.1)
        for k, (h, f) in (fields or {}).items():
            hs[k], fp[k] = h, f
        hs, fp = np.ma.masked_array(hs), np.ma.masked_array(fp)
        if fill is not None:
            hs[:, fill[0], fill[1]] = np.ma.masked
            fp[:, fill[0], fill[1]] = np.ma.masked
        data["hs"][:], data["fp"][:] = hs, fp
    return path


def rows_at(*times_and_places):
    return [{"ref_time": t, "ref_lon": lon, "ref_lat": lat} for t, lon, lat in times_and_places]


def test_file_name():
    assert ww3_file("/r", datetime(2026, 2, 3)).as_posix() == (
        "/r/2026/FIELD_NC/CCI_WW3-GLOB-30M_202602.nc")


def test_nearest_hour_hs_and_tp_including_the_next_month_file(tmp_path):
    write_ww3(tmp_path, fields={0: (2.5, 0.1), 1: (4.0, 0.05)})   # packed exactly (0.001)
    write_ww3(tmp_path, datetime(2026, 12, 1), fields={0: (1.0, 0.125)})
    rows = rows_at((datetime(2026, 11, 1, 0, 20), -72.3, -66.2),
                   (datetime(2026, 11, 1, 0, 40), -72.3, -66.2),
                   (datetime(2026, 11, 30, 23, 45), -72.3, -66.2))   # 00:00 on 1 December
    source = WW3(tmp_path)
    source.fill(rows)
    assert (rows[0][HS], rows[0][TP]) == (pytest.approx(2.5), pytest.approx(10.0))
    assert (rows[1][HS], rows[1][TP]) == (pytest.approx(4.0), pytest.approx(20.0))
    assert (rows[2][HS], rows[2][TP]) == (pytest.approx(1.0), pytest.approx(8.0))
    assert source.counts() == {"no_ww3": 0, "missing_ww3_files": 0}


def test_each_month_file_is_opened_once_per_batch(tmp_path, monkeypatch):
    import netCDF4
    write_ww3(tmp_path, hours=48)
    opened, real = [], netCDF4.Dataset
    monkeypatch.setattr(netCDF4, "Dataset", lambda p, *a, **k: opened.append(p) or real(p, *a, **k))
    WW3(tmp_path).fill(rows_at(*[(MONTH + timedelta(hours=h, minutes=5), -72.3, -66.2)
                                 for h in range(30)]))
    assert len(opened) == 1


def test_nearest_cell_not_interpolation(tmp_path):
    path = write_ww3(tmp_path, hours=2)
    with Dataset(path, "a") as data:
        data["hs"][0, 8, 10] = 6.0
    lat, lon = float(LATS[8]), float(LONS[10])
    rows = rows_at((MONTH, lon + 0.2, lat - 0.2), (MONTH, lon + 0.3, lat))
    WW3(tmp_path).fill(rows)
    assert rows[0][HS] == pytest.approx(6.0)
    assert rows[1][HS] == pytest.approx(2.5)           # 0.3 east is the next cell over


def test_land_missing_file_and_off_grid_give_counted_nulls(tmp_path):
    write_ww3(tmp_path, hours=2, fill=(8, 10))
    lat, lon = float(LATS[8]), float(LONS[10])
    rows = rows_at((MONTH, lon, lat), (MONTH, lon, 10.0), (MONTH, 100.0, lat),
                   (datetime(2026, 9, 3), -72.3, -66.2))            # no September file
    source = WW3(tmp_path)
    source.fill(rows)
    assert [(r[HS], r[TP]) for r in rows] == [(None, None)] * 4
    assert source.counts() == {"no_ww3": 4, "missing_ww3_files": 1}


def test_a_missing_hour_or_other_units_raise(tmp_path):
    write_ww3(tmp_path, hours=12)
    with pytest.raises(ValueError, match="no field valid at"):
        WW3(tmp_path).fill(rows_at((datetime(2026, 11, 1, 15), -72.3, -66.2)))
    other = tmp_path / "cm"
    write_ww3(other, hours=2, hs_unit="cm")
    with pytest.raises(ValueError, match="cm"):
        WW3(other).fill(rows_at((MONTH, -72.3, -66.2)))


def test_make_builds_the_reader_named_in_the_recipe(tmp_path):
    assert isinstance(make("ww3", tmp_path), WW3)


def test_wv_catalogue_with_ww3_validates(tmp_path):
    ocn_dir = make_archive(tmp_path / "arch")
    product = tmp_path / "stac" / "scat" / "hscat_hy-2b_25km"
    write_day(product, "2026-11-19", [make_item(ocn_dir, 1, [point(-72.33, -66.2)])])
    write_ww3(tmp_path / "ww3")
    (path,), counts = build_catalogues(
        [product], "2026-11-19", "2026-11-19", tmp_path / "out", production="20261127",
        ancillary_paths={"ww3": tmp_path / "ww3"})
    table = pq.read_table(path)
    assert table.column_names[-2:] == [HS, TP]
    assert table[HS][0].as_py() == pytest.approx(2.5)
    meta = {k.decode(): v.decode() for k, v in table.schema.metadata.items()}
    assert "WAVEWATCH III" in meta["source ancillary datasets"]
    assert counts["no_ww3"] == 0
    assert validate_dataset(path, "catalogue").ok


def test_iw_catalogue_fills_the_spec_ww3_column(tmp_path):
    collection = tmp_path / "swot" / "d2"
    write_iw_day(collection, "2026-11-19", [item(GRD, [iw_point(-72.33, -66.2) | {
        "ref_time_utc": "2026-11-19T00:10:00Z"}])])
    write_ww3(tmp_path / "ww3")
    (path,), _ = build_catalogues(
        [collection], "2026-11-19", "2026-11-19", tmp_path / "out", production="20261127",
        ancillary_paths={"ww3": tmp_path / "ww3"}, mode=iw.IW())
    table = pq.read_table(path)
    assert table.column_names.count(HS) == 1 and TP in table.column_names
    assert table[HS][0].as_py() == pytest.approx(2.5)
    meta = {k.decode(): v.decode() for k, v in table.schema.metadata.items()}
    assert HS not in meta["ancillary columns not filled"]
