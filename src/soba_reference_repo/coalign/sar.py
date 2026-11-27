"""SAR side of a catalogue row, shared by every mode: platforms, SAFE folders, footprints.

The mode-specific parts (WV SLC twin lookup and ``:WV_NNN`` references; IW archives and
OCN / SLC twins) live in ``wv.py`` and ``iw.py``.
"""

from __future__ import annotations

from pathlib import Path

PLATFORM = {"sentinel-1a": "S1A", "sentinel-1b": "S1B", "sentinel-1c": "S1C", "sentinel-1d": "S1D"}


def href_path(href: str) -> str:
    """``file:///a/b`` -> ``/a/b``."""
    return href[len("file://"):] if href.startswith("file://") else href


def safe_dir_of(measurement_path: str) -> Path:
    """The ``*.SAFE`` folder that holds a measurement file."""
    for parent in Path(measurement_path).parents:
        if parent.name.endswith(".SAFE"):
            return parent
    raise ValueError(f"no .SAFE folder in {measurement_path}")


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
