"""WAVEWATCH III, CCI sea state run GLOB-30M: ``WW3_Hs`` (spec) and ``WW3_Tp``.

Files: ``<root>/<yyyy>/FIELD_NC/CCI_WW3-GLOB-30M_<yyyymm>.nc``, one per month with hourly fields
(``time``, days since 1990-01-01), 0.5 degree grid, latitude -78 to 80, longitude -180 to
179.5 (root ``/scale/project/wave/WW3/PROJECT/CCI/RUNS/GLOB-30M``). ``hs`` (m) and ``fp`` (peak
frequency, s-1) are packed int16, land cells hold the fill value. The ``_ef`` and ``_p2l``
files of the same folder (spectral parameters) are not read.

Rule, as for the winds (``ecmwf.py``, ``era5.py``): the field of the hour nearest to
``ref_time`` (``hh:30`` rounds up, so 23:30 on the last day of a month reads the next month's
file) and the nearest grid cell to the reference point. ``WW3_Tp`` is ``1 / fp``.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

from . import Source
from .ecmwf import hour_of
from .grid import nearest_index

HS, TP = "WW3_Hs", "WW3_Tp"
EPOCH = datetime(1990, 1, 1)  # the files count days since 1990-01-01


def ww3_file(root, month) -> Path:
    """The field file of the month of ``month`` (a date or datetime)."""
    return Path(root) / f"{month:%Y}" / "FIELD_NC" / f"CCI_WW3-GLOB-30M_{month:%Y%m}.nc"


class WW3(Source):
    name = "ww3"
    columns = (HS, TP)
    description = "sea state: WAVEWATCH III CCI sea state run GLOB-30M, 0.5 degree, hourly"

    def __init__(self, root):
        self.root = Path(root)
        self.missing: set = set()
        self.no_sea_state = 0

    def fill(self, rows):
        by_month = defaultdict(lambda: defaultdict(list))
        for row in rows:
            row[HS] = row[TP] = None
            hour = hour_of(row["ref_time"])
            by_month[(hour.year, hour.month)][hour].append(row)
        for month in sorted(by_month):
            self._fill_month(datetime(*month, 1), by_month[month])

    def _fill_month(self, month, by_hour):
        import netCDF4

        path = ww3_file(self.root, month)
        if not path.is_file():
            self.missing.add(path)
            self.no_sea_state += sum(len(rows) for rows in by_hour.values())
            return
        with netCDF4.Dataset(path) as data:
            if data["hs"].units != "m" or data["fp"].units != "s-1":
                raise ValueError(f"{path}: hs in {data['hs'].units!r}, fp in "
                                 f"{data['fp'].units!r}; expected m and s-1")
            # Hours since the epoch, rounded: the axis is in days, stored as float64.
            steps = {EPOCH + timedelta(hours=round(float(day) * 24)): k
                     for k, day in enumerate(data["time"][:])}
            latitude, longitude = data["latitude"][:], data["longitude"][:]
            for hour in sorted(by_hour):
                rows = by_hour[hour]
                if hour not in steps:
                    raise ValueError(f"{path} has no field valid at {hour}")
                lat_i, lat_ok = nearest_index(latitude, [r["ref_lat"] for r in rows])
                lon_i, lon_ok = nearest_index(longitude, [r["ref_lon"] for r in rows], period=360)
                hs = np.ma.filled(data["hs"][steps[hour]].astype("float64"), np.nan)[lat_i, lon_i]
                fp = np.ma.filled(data["fp"][steps[hour]].astype("float64"), np.nan)[lat_i, lon_i]
                good = lat_ok & lon_ok & np.isfinite(hs)
                for k, row in enumerate(rows):
                    if good[k]:
                        row[HS] = float(hs[k])
                        row[TP] = float(1.0 / fp[k]) if np.isfinite(fp[k]) and fp[k] > 0 else None
                self.no_sea_state += int((~good).sum())

    def counts(self):
        return {"no_ww3": self.no_sea_state, "missing_ww3_files": len(self.missing)}
