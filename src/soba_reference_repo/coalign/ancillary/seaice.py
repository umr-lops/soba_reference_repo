"""Sea ice concentration: ``sea_ice_concentration`` (fraction 0..1) from Bremen ASI v5.4 AMSR2.

Source (chosen in the SOBA sea ice product review): University of Bremen ASI v5.4, daily, NSIDC
polar-stereographic 6.25 km grids, one file per day and hemisphere,
``<root>/{n,s}6250/<yyyy>/<mm>/asi-AMSR2-{n,s}6250-<yyyymmdd>-v5.4.hdf`` (2002-2011:
``asi-{n,s}6250-...``; a ``-v5.hdf`` duplicate is read only when no ``-v5.4`` file exists).
``ASI Ice Concentration`` is in percent 0..100; land (120) is outside ``valid_range`` and
reads as missing. The cell longitudes and latitudes are in
``<root>/{n,s}6250/LongitudeLatitudeGrid-{n6250-Arctic,s6250-Antarctic}.hdf``.

Rule: the file of the UTC day of ``ref_time`` and of the point's hemisphere, and its nearest
cell. The cell is found by projecting the point with the NSIDC polar-stereographic formulas
(Hughes 1980 ellipsoid, true latitude +-70, central meridian -45 north / 0 south); the
projection is fitted to and checked against the grid file when it is loaded, so a grid that
does not follow it raises instead of giving wrong cells. Land, missing days and points outside
the grids (below about 31 N / above 39 S) give counted nulls.
"""

from __future__ import annotations

from collections import OrderedDict, defaultdict
from pathlib import Path

import numpy as np

from . import Source

COLUMN = "sea_ice_concentration"
A, E = 6378273.0, 0.081816153            # Hughes 1980 ellipsoid (NSIDC polar stereographic)
HEMISPHERES = {"n": (70.0, -45.0, "n6250-Arctic"), "s": (-70.0, 0.0, "s6250-Antarctic")}
DAY_CACHE = 8  # daily fields kept (8 MB each)


def stereographic(lat, lon, true_lat, lon0):
    """NSIDC polar-stereographic (x, y) in metres (Snyder 1987, ellipsoid, both poles)."""
    sign = 1.0 if true_lat > 0 else -1.0
    phi, phi_c = np.radians(sign * np.asarray(lat, float)), np.radians(sign * true_lat)
    lam = np.radians(sign * (np.asarray(lon, float) - lon0))

    def t(p):
        return np.tan(np.pi / 4 - p / 2) / ((1 - E * np.sin(p)) / (1 + E * np.sin(p))) ** (E / 2)

    m_c = np.cos(phi_c) / np.sqrt(1 - E**2 * np.sin(phi_c) ** 2)
    rho = A * m_c * t(phi) / t(phi_c)
    return sign * rho * np.sin(lam), -sign * rho * np.cos(lam)


def asi_file(root, hemisphere, day) -> Path | None:
    """The ASI file of one day and hemisphere (``-v5.4`` first), or None."""
    folder = Path(root) / f"{hemisphere}6250" / f"{day:%Y}" / f"{day:%m}"
    for version in ("v5.4", "v5"):
        found = sorted(folder.glob(f"asi*-{hemisphere}6250-{day:%Y%m%d}-{version}.hdf"))
        if found:
            return found[0]
    return None


class _Grid:
    """Point -> (row, column) of one hemisphere's grid, from the projection fitted on its file."""

    def __init__(self, root, hemisphere):
        import netCDF4

        self.true_lat, self.lon0, name = HEMISPHERES[hemisphere]
        path = Path(root) / f"{hemisphere}6250" / f"LongitudeLatitudeGrid-{name}.hdf"
        with netCDF4.Dataset(path) as data:
            lat = np.asarray(data["Latitudes"][:], float)
            lon = np.asarray(data["Longitudes"][:], float)
        x, y = stereographic(lat, lon, self.true_lat, self.lon0)
        self.shape = lat.shape
        self.dx = float(np.median(np.diff(x, axis=1)))
        self.dy = float(np.median(np.diff(y, axis=0)))
        self.x0, self.y0 = float(np.median(x[:, 0])), float(np.median(y[0, :]))
        rows, cols = np.indices(self.shape)
        error = np.hypot(x - (self.x0 + cols * self.dx), y - (self.y0 + rows * self.dy))
        if not (abs(abs(self.dx) - 6250) < 1 and abs(abs(self.dy) - 6250) < 1
                and np.nanmax(error) < 0.05 * 6250):
            raise ValueError(f"{path} does not follow the NSIDC 6.25 km polar-stereographic grid "
                             f"(step {self.dx:.1f} x {self.dy:.1f} m, worst cell "
                             f"{np.nanmax(error):.0f} m off)")

    def cells(self, lat, lon):
        x, y = stereographic(lat, lon, self.true_lat, self.lon0)
        row = np.rint((y - self.y0) / self.dy).astype(int)
        col = np.rint((x - self.x0) / self.dx).astype(int)
        inside = (row >= 0) & (row < self.shape[0]) & (col >= 0) & (col < self.shape[1])
        return np.where(inside, row, 0), np.where(inside, col, 0), inside


class SeaIce(Source):
    name = "seaice"
    columns = (COLUMN,)
    description = "sea ice: Bremen ASI v5.4 AMSR2 sea ice concentration, 6.25 km, daily"

    def __init__(self, root):
        self.root = Path(root)
        self._grids: dict = {}
        self._days: OrderedDict = OrderedDict()
        self.missing: set = set()
        self.no_ice_value = 0

    def _grid(self, hemisphere):
        if hemisphere not in self._grids:
            self._grids[hemisphere] = _Grid(self.root, hemisphere)
        return self._grids[hemisphere]

    def _field(self, hemisphere, day):
        key = (hemisphere, day)
        if key not in self._days:
            import netCDF4

            path = asi_file(self.root, hemisphere, day)
            if path is None:
                self.missing.add(key)
                self._days[key] = None
            else:
                with netCDF4.Dataset(path) as data:
                    field = data["ASI Ice Concentration"][:]
                self._days[key] = np.ma.filled(field.astype("float64"), np.nan)
            if len(self._days) > DAY_CACHE:
                self._days.popitem(last=False)
        return self._days[key]

    def fill(self, rows):
        groups = defaultdict(list)
        for row in rows:
            row[COLUMN] = None
            hemisphere = "n" if row["ref_lat"] >= 0 else "s"
            groups[(hemisphere, row["ref_time"].date())].append(row)
        for (hemisphere, day), group in sorted(groups.items()):
            field = self._field(hemisphere, day)
            if field is None:
                self.no_ice_value += len(group)
                continue
            if field.shape != self._grid(hemisphere).shape:
                raise ValueError(f"ASI {hemisphere} {day}: field {field.shape}, grid "
                                 f"{self._grid(hemisphere).shape}")
            row_i, col_i, inside = self._grid(hemisphere).cells(
                [r["ref_lat"] for r in group], [r["ref_lon"] for r in group])
            values = field[row_i, col_i]
            good = inside & np.isfinite(values)
            for k, row in enumerate(group):
                if good[k]:
                    row[COLUMN] = float(values[k]) / 100.0
            self.no_ice_value += int((~good).sum())

    def counts(self):
        return {"no_sea_ice_value": self.no_ice_value, "missing_sea_ice_days": len(self.missing)}
