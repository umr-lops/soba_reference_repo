"""Row positions without sar_lat / sar_lon (spec 1.5.0), older files still readable."""

import pytest

from soba_reference_repo.positions import position_columns


def test_reference_position_first_then_test_names_then_old_sar_columns():
    assert position_columns(["sar_lon", "sar_lat", "ref_lon", "ref_lat"]) == ("ref_lon", "ref_lat")
    assert position_columns(["swot_lon", "swot_lat", "sar_lat", "sar_lon"]) == (
        "swot_lon", "swot_lat")
    assert position_columns(["scat_lat", "scat_lon"]) == ("scat_lon", "scat_lat")
    assert position_columns(["sar_lon", "sar_lat"]) == ("sar_lon", "sar_lat")   # before 1.5.0
    with pytest.raises(ValueError, match="no position columns"):
        position_columns(["ref_lon", "sar_lat"])
