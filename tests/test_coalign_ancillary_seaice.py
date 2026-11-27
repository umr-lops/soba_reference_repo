"""Bremen ASI sea ice concentration: daily file per hemisphere, nearest cell by projection."""

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
from soba_reference_repo.coalign.ancillary.seaice import (
    COLUMN, E, HEMISPHERES, SeaIce, asi_file, stereographic,
)
from soba_reference_repo.coalign.catalogue import build_catalogues
from soba_reference_repo.dataset_validator import validate_dataset

DAY = datetime(2026, 11, 19)
STEP = 6250.0
SHAPE = (24, 20)
# Grid corners around these points: north (Labrador Sea) and south (Weddell Sea).
CENTRES = {"n": (62.0, -55.0), "s": (-66.2, -72.33)}


def inverse(x, y, true_lat, lon0):
    """Latitude, longitude of polar-stereographic (x, y) (iterative inverse, Snyder 1987)."""
    sign = 1.0 if true_lat > 0 else -1.0
    x, y = sign * np.asarray(x), sign * np.asarray(y)
    phi_c = np.radians(sign * true_lat)
    t_c = np.tan(np.pi / 4 - phi_c / 2) / (
        (1 - E * np.sin(phi_c)) / (1 + E * np.sin(phi_c))) ** (E / 2)
    m_c = np.cos(phi_c) / np.sqrt(1 - E**2 * np.sin(phi_c) ** 2)
    t = np.hypot(x, y) * t_c / (6378273.0 * m_c)
    phi = np.pi / 2 - 2 * np.arctan(t)
    for _ in range(20):
        phi = np.pi / 2 - 2 * np.arctan(t * ((1 - E * np.sin(phi)) / (1 + E * np.sin(phi)))
                                        ** (E / 2))
    lam = np.arctan2(x, -y)
    return sign * np.degrees(phi), (sign * np.degrees(lam) + lon0) % 360.0


def grid_latlon(hemisphere):
    true_lat, lon0, _ = HEMISPHERES[hemisphere]
    xc, yc = stereographic(*CENTRES[hemisphere], true_lat, lon0)
    x0 = np.floor(xc / STEP) * STEP - 10 * STEP + STEP / 2
    y0 = np.floor(yc / STEP) * STEP - 12 * STEP + STEP / 2
    rows, cols = np.indices(SHAPE)
    return inverse(x0 + cols * STEP, y0 + rows * STEP, true_lat, lon0)


def write_grid(root, hemisphere):
    lat, lon = grid_latlon(hemisphere)
    path = root / f"{hemisphere}6250" / f"LongitudeLatitudeGrid-{HEMISPHERES[hemisphere][2]}.hdf"
    path.parent.mkdir(parents=True, exist_ok=True)
    with Dataset(path, "w") as data:
        data.createDimension("fakeDim0", SHAPE[0])
        data.createDimension("fakeDim1", SHAPE[1])
        data.createVariable("Longitudes", "f4", ("fakeDim0", "fakeDim1"))[:] = lon
        data.createVariable("Latitudes", "f4", ("fakeDim0", "fakeDim1"))[:] = lat
    return lat, lon


def write_asi(root, hemisphere="n", day=DAY, percent=40.0, version="v5.4", cells=None):
    """One daily ASI file (percent everywhere, ``cells`` maps (row, col) to other values)."""
    folder = root / f"{hemisphere}6250" / f"{day:%Y}" / f"{day:%m}"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"asi-AMSR2-{hemisphere}6250-{day:%Y%m%d}-{version}.hdf"
    field = np.full(SHAPE, percent, dtype="f4")
    for (row, col), value in (cells or {}).items():
        field[row, col] = value
    with Dataset(path, "w") as data:
        data.createDimension("fakeDim0", SHAPE[0])
        data.createDimension("fakeDim1", SHAPE[1])
        var = data.createVariable("ASI Ice Concentration", "f4", ("fakeDim0", "fakeDim1"))
        var.valid_range = np.array([0.0, 100.0], dtype="f4")
        var[:] = field
    return path


def rows_at(*places, time=DAY):
    return [{"ref_time": time, "ref_lon": lon, "ref_lat": lat} for lon, lat in places]


def test_file_name_prefers_v54_over_the_v5_duplicate(tmp_path):
    assert asi_file(tmp_path, "n", DAY) is None
    write_asi(tmp_path, version="v5")
    assert asi_file(tmp_path, "n", DAY).name.endswith("-v5.hdf")
    write_asi(tmp_path)
    assert asi_file(tmp_path, "n", DAY).name == "asi-AMSR2-n6250-20261119-v5.4.hdf"


@pytest.mark.parametrize("hemisphere", ["n", "s"])
def test_each_cell_centre_finds_its_own_cell(tmp_path, hemisphere):
    lat, lon = write_grid(tmp_path, hemisphere)
    values = {(r, c): float(r * SHAPE[1] + c) / 10 for r in range(SHAPE[0]) for c in range(SHAPE[1])}
    write_asi(tmp_path, hemisphere, cells=values)
    rows = rows_at(*[(float(lon[r, c]) - 360, float(lat[r, c])) for r, c in values])
    SeaIce(tmp_path).fill(rows)
    assert [row[COLUMN] for row in rows] == pytest.approx([v / 100 for v in values.values()])


def test_land_missing_day_and_points_off_the_grid_give_counted_nulls(tmp_path):
    lat, lon = write_grid(tmp_path, "n")
    write_asi(tmp_path, cells={(5, 5): 120.0})               # land: outside valid_range
    rows = rows_at((float(lon[5, 5]), float(lat[5, 5])), (float(lon[6, 6]), float(lat[6, 6])),
                   (10.0, 20.0), (-55.0, -80.0))              # far off the grid, south pole
    rows += rows_at((float(lon[6, 6]), float(lat[6, 6])), time=datetime(2026, 11, 20))
    source = SeaIce(tmp_path)
    source.fill(rows)
    assert [r[COLUMN] for r in rows] == [None, pytest.approx(0.4), None, None, None]
    assert source.counts() == {"no_sea_ice_value": 4, "missing_sea_ice_days": 2}


def test_a_grid_that_is_not_the_nsidc_projection_raises(tmp_path):
    lat, lon = write_grid(tmp_path, "n")
    path = tmp_path / "n6250" / "LongitudeLatitudeGrid-n6250-Arctic.hdf"
    with Dataset(path, "a") as data:
        data["Longitudes"][:] = lon + 3.0                     # rotated by 3 degrees
    write_asi(tmp_path)
    with pytest.raises(ValueError, match="does not follow"):
        SeaIce(tmp_path).fill(rows_at((float(lon[3, 3]), float(lat[3, 3]))))


def test_make_builds_the_reader_named_in_the_recipe(tmp_path):
    assert isinstance(make("seaice", tmp_path), SeaIce)


def test_wv_catalogue_with_sea_ice_validates(tmp_path):
    write_grid(tmp_path / "ice", "s")
    write_asi(tmp_path / "ice", "s", percent=24.0)
    ocn_dir = make_archive(tmp_path / "arch")
    product = tmp_path / "stac" / "scat" / "hscat_hy-2b_25km"
    write_day(product, "2026-11-19", [make_item(ocn_dir, 1, [point(-72.33, -66.2)])])
    (path,), counts = build_catalogues(
        [product], "2026-11-19", "2026-11-19", tmp_path / "out", production="20261127",
        ancillary_paths={"seaice": tmp_path / "ice"})
    table = pq.read_table(path)
    assert table.column_names[-1] == COLUMN and table[COLUMN][0].as_py() == pytest.approx(0.24)
    meta = {k.decode(): v.decode() for k, v in table.schema.metadata.items()}
    assert "Bremen ASI" in meta["source ancillary datasets"]
    assert counts["no_sea_ice_value"] == 0
    assert validate_dataset(path, "catalogue").ok


def test_iw_catalogue_fills_the_spec_sea_ice_column(tmp_path):
    write_grid(tmp_path / "ice", "s")
    write_asi(tmp_path / "ice", "s", percent=24.0)
    collection = tmp_path / "swot" / "d2"
    write_iw_day(collection, "2026-11-19", [item(GRD, [iw_point(-72.33, -66.2) | {
        "ref_time_utc": "2026-11-19T10:00:00Z"}])])
    (path,), _ = build_catalogues(
        [collection], "2026-11-19", "2026-11-19", tmp_path / "out", production="20261127",
        ancillary_paths={"seaice": tmp_path / "ice"}, mode=iw.IW())
    table = pq.read_table(path)
    assert table.column_names.count(COLUMN) == 1
    assert table[COLUMN][0].as_py() == pytest.approx(0.24)
    meta = {k.decode(): v.decode() for k, v in table.schema.metadata.items()}
    assert COLUMN not in meta["ancillary columns not filled"]
