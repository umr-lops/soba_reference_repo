"""ERA5 reanalysis 10 m wind: ``ERA5_Wind_Speed`` and ``ERA5_Wind_Direction``.

Files: ``<root>/<yyyy>/<mm>/era5_single-levels_inst_<yyyymmdd>.nc``, one per day with its 24
hourly fields (``valid_time``, seconds since 1970-01-01). Variables ``u10`` and ``v10`` (m/s,
dims valid_time, latitude, longitude), 0.25 degree grid, latitude 90 to -90, longitude 0 to
359.75, fill value NaN. The same day folder holds other ERA5 files (pressure levels, waves,
accumulated and averaged fields); only the instantaneous single-level one is read.

Rule, as for the ECMWF forecast (``ecmwf.py``): the field of the hour nearest to ``ref_time``
(``hh:30`` rounds up, so 23:30 reads 00:00 of the next day's file) and the nearest grid cell to
the reference point. Speed is ``hypot(u, v)``; direction follows the spec's "To" convention,
degrees clockwise from north, null when the speed is exactly zero.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from . import Source
from .ecmwf import hour_of, speed_direction
from .grid import nearest_index

SPEED, DIRECTION = "ERA5_Wind_Speed", "ERA5_Wind_Direction"
UNITS = ("m s**-1", "m s-1", "m/s")


def era5_file(root, day) -> Path:
    """The instantaneous single-level file of one day."""
    return Path(root) / f"{day:%Y}" / f"{day:%m}" / f"era5_single-levels_inst_{day:%Y%m%d}.nc"


class Era5(Source):
    name = "era5"
    columns = (SPEED, DIRECTION)
    description = "wind: ERA5 reanalysis 10 m wind, 0.25 degree, hourly"

    def __init__(self, root):
        self.root = Path(root)
        self.missing: set = set()
        self.no_wind = 0

    def fill(self, rows):
        by_day = defaultdict(lambda: defaultdict(list))
        for row in rows:
            row[SPEED] = row[DIRECTION] = None
            hour = hour_of(row["ref_time"])
            by_day[hour.date()][hour].append(row)
        for day in sorted(by_day):
            self._fill_day(day, by_day[day])

    def _fill_day(self, day, by_hour):
        import netCDF4

        path = era5_file(self.root, day)
        if not path.is_file():
            self.missing.add(path)
            self.no_wind += sum(len(rows) for rows in by_hour.values())
            return
        with netCDF4.Dataset(path) as data:
            for name in ("u10", "v10"):
                if data[name].units not in UNITS:
                    raise ValueError(f"{path}: {name} unit {data[name].units!r}, expected m/s")
            times = [datetime.fromtimestamp(int(s), timezone.utc).replace(tzinfo=None)
                     for s in data["valid_time"][:]]
            latitude, longitude = data["latitude"][:], data["longitude"][:]
            for hour in sorted(by_hour):
                rows = by_hour[hour]
                if hour not in times:
                    raise ValueError(f"{path} has no field valid at {hour}")
                step = times.index(hour)
                lat_i, lat_ok = nearest_index(latitude, [r["ref_lat"] for r in rows])
                lon_i, lon_ok = nearest_index(longitude, [r["ref_lon"] for r in rows], period=360)
                u = np.ma.filled(data["u10"][step].astype("float64"), np.nan)[lat_i, lon_i]
                v = np.ma.filled(data["v10"][step].astype("float64"), np.nan)[lat_i, lon_i]
                speed, direction = speed_direction(u, v)
                good = lat_ok & lon_ok & np.isfinite(speed)
                for k, row in enumerate(rows):
                    if good[k]:
                        row[SPEED] = float(speed[k])
                        row[DIRECTION] = None if np.isnan(direction[k]) else float(direction[k])
                self.no_wind += int((~good).sum())

    def counts(self):
        return {"no_era5_wind": self.no_wind, "missing_era5_files": len(self.missing)}
