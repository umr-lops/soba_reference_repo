"""SAR side of a catalogue row: SAFE names, archive paths, SLC twin lookup, footprint centroid."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

PLATFORM = {"sentinel-1a": "S1A", "sentinel-1b": "S1B", "sentinel-1c": "S1C", "sentinel-1d": "S1D"}
POLARIZATION = {"VV": "SV", "HH": "SH"}  # WV is single polarization


def href_path(href: str) -> str:
    """``file:///a/b`` -> ``/a/b``."""
    return href[len("file://"):] if href.startswith("file://") else href


def safe_dir_of(measurement_path: str) -> Path:
    """The ``*.SAFE`` folder that holds a measurement file."""
    for parent in Path(measurement_path).parents:
        if parent.name.endswith(".SAFE"):
            return parent
    raise ValueError(f"no .SAFE folder in {measurement_path}")


def safe_ref(safe_dir: Path, imagette: int) -> str:
    """``<product folder>/<year>/<doy>/<SAFE>:WV_<NNN>``, the spec's ``sar_safe_*`` form."""
    return "/".join([*safe_dir.parts[-4:]]) + f":WV_{imagette:03d}"


@lru_cache(maxsize=4096)
def _listing(directory: str) -> tuple:
    path = Path(directory)
    return tuple(sorted(p.name for p in path.iterdir())) if path.is_dir() else ()


def slc_safe_dir(ocn_safe_dir: Path) -> Path | None:
    """The SLC SAFE acquired with an OCN SAFE, or None when the archive has none.

    Both products share mission, mode, polarization, start and stop times, orbit and data take;
    only the product type and the trailing CRC differ.
    """
    if "/L2/WV/" not in str(ocn_safe_dir) or "_OCN__2S" not in ocn_safe_dir.name:
        raise ValueError(f"not an OCN WV SAFE path: {ocn_safe_dir}")
    swap = {"/L2/WV/": "/L1/WV/", "_OCN__2S": "_SLC__1S"}
    day = str(ocn_safe_dir.parent)
    name = ocn_safe_dir.name
    for old, new in swap.items():
        day, name = day.replace(old, new), name.replace(old, new)
    day = Path(day)
    prefix = name.removesuffix(".SAFE").rsplit("_", 1)[0]
    hits = [n for n in _listing(str(day)) if n.startswith(prefix + "_") and n.endswith(".SAFE")]
    return day / hits[0] if len(hits) == 1 else None


def slc_measurement(ocn_measurement: str, slc_dir: Path) -> str:
    """The SLC measurement named like the OCN one (``-ocn-`` to ``-slc-``, ``.nc`` to ``.tiff``)."""
    name = Path(ocn_measurement).name.replace("-ocn-", "-slc-")
    return str(slc_dir / "measurement" / (name.removesuffix(".nc") + ".tiff"))


def centroid(ring) -> tuple:
    """(lon, lat) mean of the footprint corners, safe across the antimeridian."""
    corners = list(ring[:-1]) if ring[0] == ring[-1] else list(ring)
    lon0 = corners[0][0]
    lons = [lon0 + ((lon - lon0 + 180.0) % 360.0) - 180.0 for lon, _ in corners]
    lon = (sum(lons) / len(lons) + 180.0) % 360.0 - 180.0
    return lon, sum(lat for _, lat in corners) / len(corners)


def footprint_centroid(geometry: dict) -> tuple:
    """(lon, lat) of a Polygon or MultiPolygon footprint.

    A footprint crossing the antimeridian is stored as two polygons, one on each side; all their
    corners are averaged together.
    """
    if geometry["type"] == "Polygon":
        rings = [geometry["coordinates"][0]]
    elif geometry["type"] == "MultiPolygon":
        rings = [polygon[0] for polygon in geometry["coordinates"]]
    else:
        raise ValueError(f"unsupported footprint geometry: {geometry['type']}")
    return centroid([corner for ring in rings for corner in ring])
