"""Values shared by the catalogue rows of every mode: STAC point fields -> column values.

The rows themselves are built per mode (``wv.py``, ``iw.py``).
"""

from __future__ import annotations

from datetime import datetime

KEY_DECIMALS = 4  # lon/lat decimals in primary_key (spec: at least 2)


def stac_time(value: str) -> datetime:
    """STAC UTC time ('...Z', up to nanoseconds) -> naive UTC datetime, microseconds."""
    value = value.rstrip("Z")
    head, _, frac = value.partition(".")
    return datetime.fromisoformat(f"{head}.{(frac + '000000')[:6]}" if frac else head)


def as_flag(value):
    """A quality flag is an integer bit mask. STAC may store it as 2048.0; 0.5 is an error."""
    if value is None:
        return None
    if float(value) != int(value):
        raise ValueError(f"ref_flag must be an integer bit mask, got {value!r}")
    return int(value)


def as_float(value):
    return None if value is None else float(value)
