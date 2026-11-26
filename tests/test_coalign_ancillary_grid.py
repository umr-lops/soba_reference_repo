"""Nearest-cell lookup on regular axes, with longitude wrap."""

import numpy as np

from soba_reference_repo.coalign.ancillary.grid import nearest_index


def test_ascending_axis():
    index, inside = nearest_index([0.0, 1.0, 2.0], [0.4, 0.6, 2.0])
    assert index.tolist() == [0, 1, 2] and inside.all()


def test_descending_axis_like_ecmwf_latitude():
    axis = np.arange(90, -90.01, -0.1)        # +90 first, 1801 cells
    index, inside = nearest_index(axis, [90.0, -44.65, -90.0])
    assert [round(axis[i], 1) for i in index] == [90.0, -44.7, -90.0] and inside.all()


def test_values_beyond_half_a_cell_outside_the_axis_are_not_inside():
    axis = np.arange(-40.5, -45.75, -0.1)     # a cropped grid
    index, inside = nearest_index(axis, [-40.5, -40.54, -40.6, -39.0, -50.0])
    assert inside.tolist() == [True, True, True, False, False]


def test_longitude_zero_to_360_axis_takes_negative_queries():
    axis = np.arange(0, 359.95, 0.1)          # 0 .. 359.9, 3600 cells
    index, inside = nearest_index(axis, [-131.166, 228.8, 359.96, 360.0, -0.04], period=360)
    assert [round(axis[i], 1) for i in index] == [228.8, 228.8, 0.0, 0.0, 0.0]
    assert inside.all()


def test_longitude_minus180_to_180_axis_takes_values_over_180():
    axis = np.arange(-180, 179.9, 0.5)
    index, _ = nearest_index(axis, [190.2, -179.9, 179.9], period=360)
    assert [float(axis[i]) for i in index] == [-170.0, -180.0, -180.0]


def test_a_point_exactly_between_two_cells_is_inside_on_a_float32_axis():
    """HSCAT cells sit on a 0.25 degree grid: 4.75 is halfway between 0.1 degree cells."""
    lat = np.arange(90, -90.01, -0.1, dtype="float32")        # the dtype of the ECMWF files
    lon = np.arange(0, 359.95, 0.1, dtype="float32")
    index, inside = nearest_index(lat, [4.75, -22.55, 60.35])
    assert inside.all()
    index, inside = nearest_index(lon, [-175.25, 120.85, 4.75], period=360)
    assert inside.all()


def test_a_point_a_full_cell_outside_a_float32_crop_is_still_outside():
    axis = np.arange(-40.5, -45.75, -0.1, dtype="float32")
    _, inside = nearest_index(axis, [-40.5 + 0.1, -40.5 + 0.06, -40.5 + 0.049])
    assert inside.tolist() == [False, False, True]
