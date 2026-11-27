"""IMERG rain rate: ``imerg_rainrate`` (mm/h), the column the curation recipes already filter on.

Files: NASA GPM IMERG Late V07B, half-hourly, HDF5,
``<root>/<yyyy>/<doy>/3B-HHR-L.MS.MRG.3IMERG.<yyyymmdd>-S<hhmmss>-E<hhmmss>.<minute>.V07B.RT-H5``
(root ``/reference/remote-sensing/public/precipitation/precipitation-rate/global/nasa/multisensor/
gpm_3imerghhl/v7b``). Group ``Grid``: ``precipitation`` (mm/hr, dims time, lon, lat = 1 x 3600
x 1800), 0.1 degree, lon -179.95 to 179.95, lat -89.95 to 89.95, fill value -9999.9.

Rule, the one the WV altimeter catalogues used for ``imerg_rainrate`` (``add_imerg_rainrate``):
the half-hour file **that contains** ``ref_time`` (a time at hh:40 reads the hh:30 file) and the
nearest grid cell. Negative or missing values, a missing file, or a point off the grid give
counted nulls. Only the box of cells the rows of one file need is read.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

from . import Source
from .grid import nearest_index

COLUMN = "imerg_rainrate"
UNITS = ("mm/hr", "mm/h", "mm h-1")


def half_hour_of(time: datetime) -> datetime:
    """The start of the half-hour that contains ``time``."""
    return time.replace(minute=30 * (time.minute // 30), second=0, microsecond=0)


def imerg_file(root, start: datetime) -> Path | None:
    """The half-hourly file starting at ``start`` (any V07 release), or None."""
    end = start + timedelta(minutes=29, seconds=59)
    minute = start.hour * 60 + start.minute
    folder = Path(root) / f"{start:%Y}" / f"{start.timetuple().tm_yday:03d}"
    found = sorted(folder.glob(f"3B-HHR-L.MS.MRG.3IMERG.{start:%Y%m%d}-S{start:%H%M%S}-"
                               f"E{end:%H%M%S}.{minute:04d}.V07*.RT-H5"))
    return found[-1] if found else None


class Imerg(Source):
    name = "imerg"
    columns = (COLUMN,)
    description = "rain: IMERG Late V07B NASA GPM rain rate, 0.1 degree, half-hourly"

    def __init__(self, root):
        self.root = Path(root)
        self.missing: set = set()
        self.no_rain_value = 0

    def fill(self, rows):
        by_file = defaultdict(list)
        for row in rows:
            row[COLUMN] = None
            by_file[half_hour_of(row["ref_time"])].append(row)
        for start in sorted(by_file):
            self._fill_half_hour(start, by_file[start])

    def _fill_half_hour(self, start, rows):
        import netCDF4

        path = imerg_file(self.root, start)
        if path is None:
            self.missing.add(start)
            self.no_rain_value += len(rows)
            return
        with netCDF4.Dataset(path) as data:
            grid = data.groups["Grid"]
            rain = grid["precipitation"]
            if rain.units not in UNITS:
                raise ValueError(f"{path}: precipitation in {rain.units!r}, expected mm/hr")
            if rain.dimensions != ("time", "lon", "lat"):
                raise ValueError(f"{path}: precipitation dims {rain.dimensions}")
            lon_i, lon_ok = nearest_index(grid["lon"][:], [r["ref_lon"] for r in rows],
                                          period=360)
            lat_i, lat_ok = nearest_index(grid["lat"][:], [r["ref_lat"] for r in rows])
            ok = lon_ok & lat_ok
            values = np.full(len(rows), np.nan)
            if ok.any():
                j0, j1 = int(lon_i[ok].min()), int(lon_i[ok].max())
                i0, i1 = int(lat_i[ok].min()), int(lat_i[ok].max())
                box = np.ma.filled(rain[0, j0:j1 + 1, i0:i1 + 1].astype("float64"), np.nan)
                values[ok] = box[lon_i[ok] - j0, lat_i[ok] - i0]
        good = ok & np.isfinite(values) & (values >= 0)
        for k, row in enumerate(rows):
            if good[k]:
                row[COLUMN] = float(values[k])
        self.no_rain_value += int((~good).sum())

    def counts(self):
        return {"no_imerg_rainrate": self.no_rain_value,
                "missing_imerg_files": len(self.missing)}
