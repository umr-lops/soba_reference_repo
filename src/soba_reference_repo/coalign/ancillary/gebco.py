"""GEBCO bathymetry: ``bathymetry``, the water depth at the reference location (m).

Source: one global grid, ``GEBCO_<year>.nc`` in the recipe folder (e.g.
``/legacy/project/cersat/public/provider/bodc/reference/gebco/gebco_19``): ``elevation``
(m above mean sea level) at 15 arc-second spacing, latitude -90 to 90, longitude -180 to 180,
cell centres. ``bathymetry`` is ``-elevation`` at the nearest cell: positive in water (the spec's
water depth), negative on land (the land height above sea level), so a curation can tell both.

The file is compressed one full latitude row (86,400 values) per chunk, so a value costs the
decompression of its whole row: rows are read once per latitude index and the last
``ROW_CACHE`` rows are kept, which serves the many points of one scene from a few hundred rows.
"""

from __future__ import annotations

from collections import OrderedDict, defaultdict
from pathlib import Path

import numpy as np

from . import Source
from .grid import nearest_index

DEPTH = "bathymetry"
ROW_CACHE = 2048  # latitude rows kept (86,400 float32 each: about 700 MB at most)


def gebco_file(root) -> Path:
    """The one ``GEBCO_*.nc`` grid of the folder ``root``."""
    found = sorted(Path(root).glob("GEBCO_*.nc"))
    if len(found) != 1:
        raise ValueError(f"expected one GEBCO_*.nc grid in {root}, found {len(found)}")
    return found[0]


class Gebco(Source):
    name = "gebco"
    columns = (DEPTH,)

    def __init__(self, root):
        self.path = gebco_file(root)
        self.description = f"bathymetry: {self.path.stem} grid, 15 arc-second (depth = -elevation)"
        self._data = None
        self._rows: OrderedDict = OrderedDict()
        self.no_depth = 0
        self.rows_read = 0

    def _open(self):
        if self._data is None:
            import netCDF4

            self._data = netCDF4.Dataset(self.path)
            elevation = self._data["elevation"]
            if elevation.units != "m":
                raise ValueError(f"{self.path}: elevation in {elevation.units!r}, expected m")
            self._lat = self._data["lat"][:]
            self._lon = self._data["lon"][:]
        return self._data

    def _row(self, index: int) -> np.ndarray:
        if index in self._rows:
            self._rows.move_to_end(index)
            return self._rows[index]
        row = np.ma.filled(self._open()["elevation"][index, :].astype("float64"), np.nan)
        self.rows_read += 1
        self._rows[index] = row
        if len(self._rows) > ROW_CACHE:
            self._rows.popitem(last=False)
        return row

    def fill(self, rows):
        for row in rows:
            row[DEPTH] = None
        if not rows:
            return
        self._open()
        lat_i, lat_ok = nearest_index(self._lat, [r["ref_lat"] for r in rows])
        lon_i, lon_ok = nearest_index(self._lon, [r["ref_lon"] for r in rows], period=360)
        by_row = defaultdict(list)
        for k in np.flatnonzero(lat_ok & lon_ok):
            by_row[int(lat_i[k])].append(k)
        self.no_depth += int((~(lat_ok & lon_ok)).sum())
        for index in sorted(by_row):
            elevation = self._row(index)
            for k in by_row[index]:
                value = elevation[lon_i[k]]
                if np.isfinite(value):
                    rows[k][DEPTH] = float(-value)
                else:
                    self.no_depth += 1

    def counts(self):
        return {"no_bathymetry": self.no_depth, "gebco_rows_read": self.rows_read}
