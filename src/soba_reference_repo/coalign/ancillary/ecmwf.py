"""ECMWF operational 10 m wind: ``ECMWF_Wind_Speed`` and ``ECMWF_Wind_Direction``.

Files: ``<root>/<yyyy>/<doy>/ECMWF_FORECAST_0100_<yyyymmddHHMM>_10U_10V.nc``, one per hour.
Variables ``10U`` and ``10V`` (m/s, dims time, Latitude, Longitude), 0.1 degree grid, latitude
90 to -90, longitude 0 to 359.9, fill value 9999.

Rule (agreed): the file of the hour nearest to ``ref_time`` (``hh:30`` rounds up) and the
nearest grid cell to the reference point. Speed is ``hypot(u, v)``. Direction follows the
spec's "To" convention, degrees clockwise from north: ``atan2(u, v)``, so a wind blowing to the
east is 90. The direction is null when the speed is exactly zero.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

from . import Source
from .grid import nearest_index

EPOCH = datetime(1990, 1, 1)  # the files count hours since 1990-01-01
SPEED, DIRECTION = "ECMWF_Wind_Speed", "ECMWF_Wind_Direction"


def hour_of(time: datetime) -> datetime:
    """The hour nearest to ``time``."""
    return (time + timedelta(minutes=30)).replace(minute=0, second=0, microsecond=0)


def wind_file(root, hour: datetime) -> Path:
    return (Path(root) / f"{hour:%Y}" / f"{hour.timetuple().tm_yday:03d}"
            / f"ECMWF_FORECAST_0100_{hour:%Y%m%d%H%M}_10U_10V.nc")


def speed_direction(u, v) -> tuple:
    """Wind speed (m/s) and the direction it blows towards (degrees clockwise from north)."""
    speed = np.hypot(u, v)
    direction = np.degrees(np.arctan2(u, v)) % 360.0
    return speed, np.where(speed > 0, direction, np.nan)


class Ecmwf(Source):
    name = "ecmwf"
    columns = (SPEED, DIRECTION)
    description = "wind: ECMWF operational forecast 10 m wind, 0.1 degree, hourly"

    def __init__(self, root):
        self.root = Path(root)
        self.missing: set = set()
        self.no_wind = 0

    def fill(self, rows):
        by_hour = defaultdict(list)
        for row in rows:
            row[SPEED] = row[DIRECTION] = None
            by_hour[hour_of(row["ref_time"])].append(row)
        for hour in sorted(by_hour):
            self._fill_hour(hour, by_hour[hour])

    def _fill_hour(self, hour, rows):
        import netCDF4

        path = wind_file(self.root, hour)
        if not path.is_file():
            self.missing.add(path)
            self.no_wind += len(rows)
            return
        with netCDF4.Dataset(path) as data:
            valid = EPOCH + timedelta(hours=int(data["time"][0]))
            if valid != hour:
                raise ValueError(f"{path} is valid at {valid}, expected {hour}")
            for name in ("10U", "10V"):
                if data[name].units not in ("m s-1", "m/s"):
                    raise ValueError(f"{path}: {name} unit {data[name].units!r}, expected m/s")
            lat_i, lat_ok = nearest_index(data["Latitude"][:], [r["ref_lat"] for r in rows])
            lon_i, lon_ok = nearest_index(data["Longitude"][:], [r["ref_lon"] for r in rows],
                                          period=360)
            u = np.ma.filled(data["10U"][0].astype("float64"), np.nan)[lat_i, lon_i]
            v = np.ma.filled(data["10V"][0].astype("float64"), np.nan)[lat_i, lon_i]
        speed, direction = speed_direction(u, v)
        good = lat_ok & lon_ok & np.isfinite(speed)
        for k, row in enumerate(rows):
            if good[k]:
                row[SPEED] = float(speed[k])
                row[DIRECTION] = None if np.isnan(direction[k]) else float(direction[k])
        self.no_wind += int((~good).sum())

    def counts(self):
        return {"no_ecmwf_wind": self.no_wind, "missing_ecmwf_files": len(self.missing)}
