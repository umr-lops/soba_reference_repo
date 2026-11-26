"""Nearest-cell lookup on a regular latitude or longitude axis (numpy only)."""

from __future__ import annotations

import numpy as np


def nearest_index(axis, values, period=None) -> tuple:
    """``(index, inside)`` of the axis cell nearest to each value.

    ``axis`` is regular and either ascending or descending. ``inside`` is False for values more
    than half a cell (plus 0.1%) outside the axis: a cropped grid then gives null, not its edge
    cell. With
    ``period`` (360 for longitude) values and axis wrap, so 0..360 and -180..180 axes both
    work and 359.96 on a 0..359.9 axis lands on cell 0.
    """
    axis = np.asarray(axis, dtype=float)
    values = np.asarray(values, dtype=float)
    n = len(axis)
    step = abs(axis[-1] - axis[0]) / (n - 1)
    order = np.arange(n) if axis[0] <= axis[-1] else np.arange(n)[::-1]
    ascending = axis[order]
    if period:
        # one extra cell on each side, standing for the wrap, so the nearest search is plain
        ascending = np.concatenate([[ascending[-1] - period], ascending, [ascending[0] + period]])
        order = np.concatenate([[order[-1]], order, [order[0]]])
        values = (values - ascending[1]) % period + ascending[1]
    position = np.searchsorted(ascending, values)
    left = np.clip(position - 1, 0, len(ascending) - 1)
    right = np.clip(position, 0, len(ascending) - 1)
    pick = np.where(np.abs(values - ascending[left]) <= np.abs(ascending[right] - values),
                    left, right)
    # a point halfway between two cells is inside; the margin absorbs float32 axes (1e-5 degree)
    inside = np.abs(values - ascending[pick]) <= step * (0.5 + 1e-3)
    return order[pick], inside
