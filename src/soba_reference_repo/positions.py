"""Where a dataset row is, for maps and the challenger (spec 1.5.0: no ``sar_lat`` / ``sar_lon``).

The SOBA datasets no longer carry the SAR position: the reference point is the row's position
(``ref_lon`` / ``ref_lat`` in catalogue and curated datasets, ``<ref>_lon`` / ``<ref>_lat`` in
TEST and TARGET datasets). Files produced before spec 1.5.0 may still have only
``sar_lon`` / ``sar_lat``, which are then used (optional columns).
"""

from __future__ import annotations

# (longitude, latitude) pairs, first match wins.
POSITION_PAIRS = (
    ("ref_lon", "ref_lat"),
    ("swot_lon", "swot_lat"),
    ("scat_lon", "scat_lat"),
    ("alti_lon", "alti_lat"),
    ("sar_lon", "sar_lat"),       # files produced before spec 1.5.0
)


def position_columns(columns) -> tuple[str, str]:
    """The (longitude, latitude) column names of a table with these ``columns``."""
    names = set(columns)
    for pair in POSITION_PAIRS:
        if set(pair) <= names:
            return pair
    raise ValueError(f"no position columns among {sorted(names)}; expected one of "
                     f"{[f'{lon}/{lat}' for lon, lat in POSITION_PAIRS]}")
