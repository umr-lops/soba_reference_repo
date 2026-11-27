"""One STAC Item -> catalogue rows (spec WV catalogue columns).

Pure: the only archive access is the injected ``slc_lookup``.
"""

from __future__ import annotations

from datetime import datetime

from .products import Reference, product_name
from .sar import (
    PLATFORM, POLARIZATION, footprint_centroid, href_path, safe_dir_of, safe_ref, slc_measurement,
    slc_safe_dir,
)

KEY_DECIMALS = 4  # lon/lat decimals in primary_key (spec: at least 2)

# Spec order. The dataset-named reference parameters sit between the reference position and
# ref_time.
HEAD = (
    "primary_key", "sar_time", "sar_lat", "sar_lon", "sar_incidence_angle", "sar_elevation_angle",
    "sar_ground_heading", "sar_distance_to_coast", "sar_path_ocn", "sar_path_slc", "sar_safe_slc",
    "sar_safe_ocn", "ref_lon", "ref_lat",
)
TAIL = ("ref_time", "ref_flag", "ref_id", "legacy_usage", "ref_source", "class_1", "pos_1")
# Kept beyond the spec: useful for curation, not required by it.
EXTRA = ("ref_time_delta_min", "sar_ground_heading_uncertainty")


def columns(param_names, ancillary=()) -> list:
    """All catalogue columns for a reference whose parameters are ``param_names``.

    ``ancillary`` lists the columns of the ancillary sources (those not already in ``TAIL``).
    """
    return [*HEAD, *param_names, *TAIL, *EXTRA, *[c for c in ancillary if c not in TAIL]]


def _time(value: str) -> datetime:
    """STAC UTC time ('...Z', up to nanoseconds) -> naive UTC datetime, microseconds."""
    value = value.rstrip("Z")
    head, _, frac = value.partition(".")
    return datetime.fromisoformat(f"{head}.{(frac + '000000')[:6]}" if frac else head)


def _flag(value):
    """A quality flag is an integer bit mask. STAC may store it as 2048.0; 0.5 is an error."""
    if value is None:
        return None
    if float(value) != int(value):
        raise ValueError(f"ref_flag must be an integer bit mask, got {value!r}")
    return int(value)


def _float(value):
    return None if value is None else float(value)


def item_rows(item: dict, reference: Reference, slc_lookup=slc_safe_dir) -> tuple:
    """(rows, skipped) for one OCN WV Item.

    ``skipped`` is "" when the Item produced rows, else the reason: ``mode``, ``product_type``
    or ``no_slc`` (the archive holds no SLC SAFE for the OCN one). Each row carries
    ``_satellite``, ``_polarization`` and ``_product`` for grouping; the writer drops them.
    """
    props = item["properties"]
    if props["sarmatchup:sar_instrument_mode"] != "WV":
        return [], "mode"
    if props["sarmatchup:sar_product_type"] != "OCN":
        return [], "product_type"
    ocn_path = href_path(item["assets"]["sar"]["href"])
    ocn_dir = safe_dir_of(ocn_path)
    slc_dir = slc_lookup(ocn_dir)
    if slc_dir is None:
        return [], "no_slc"
    imagette = int(props["sarmatchup:imagette_index"])
    satellite = PLATFORM[props["sarmatchup:sar_platform"]]
    polarization = POLARIZATION[props["sarmatchup:polarization"]]
    sar_lon, sar_lat = footprint_centroid(item["geometry"])
    sar_time = _time(props["start_datetime"]).replace(microsecond=0)
    base = {
        "sar_time": sar_time, "sar_lat": sar_lat, "sar_lon": sar_lon,
        "sar_path_ocn": ocn_path, "sar_path_slc": slc_measurement(ocn_path, slc_dir),
        "sar_safe_slc": safe_ref(slc_dir, imagette), "sar_safe_ocn": safe_ref(ocn_dir, imagette),
        "legacy_usage": None, "class_1": None, "pos_1": None,
        "_satellite": satellite, "_polarization": polarization,
    }
    rows = []
    for group in props["sarmatchup:matchups"]:
        product = product_name(reference, group.get("reference_version"))
        for point in group["points"]:
            lon, lat = point["ref_lon"], point["ref_lat"]
            row = {
                **base,
                "primary_key": (f"{slc_dir.name}:WV_{imagette:03d}_"
                                f"{lon:.{KEY_DECIMALS}f}_{lat:.{KEY_DECIMALS}f}"),
                "sar_incidence_angle": _float(point.get("sar_incidence_angle")),
                "sar_elevation_angle": _float(point.get("sar_elevation_angle")),
                "sar_ground_heading": _float(point.get("sar_ground_heading")),
                "sar_ground_heading_uncertainty": _float(
                    point.get("sar_ground_heading_uncertainty")),
                "sar_distance_to_coast": _float(point.get("sar_distance_to_coast")),
                "ref_lon": float(lon), "ref_lat": float(lat),
                "ref_time": _time(point["ref_time_utc"]).replace(microsecond=0),
                "ref_time_delta_min": _float(point.get("delta_diff_time_minutes")),
                "ref_id": group["reference_file"], "ref_source": group["reference_source"],
                "ref_flag": _flag(None if reference.flag is None else point.get(reference.flag)),
                "_product": product,
            }
            for column, field in reference.params:
                row[column] = _float(point.get(field))
            rows.append(row)
    return rows, ""
