"""WV specifics of the co-aligned catalogues: one row per reference point and WV imagette.

The rows come from soba_matchups OCN WV Items; the SLC twin of each OCN SAFE is looked up in the
archive (same name but product type and CRC), and the ``primary_key`` is built on it
(``<SLC SAFE>:WV_<NNN>_<lon>_<lat>``). TenGeoP classes (``class_1``, ``pos_1``) are WV only.
The writer, file naming and ancillary sources are shared (``catalogue.py``).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import pandas as pd
import pyarrow as pa

from .catalogue import Mode
from .products import Reference, product_name, reference_for
from .rows import KEY_DECIMALS, as_flag, as_float, stac_time
from .sar import PLATFORM, footprint_centroid, href_path, safe_dir_of
from .stac import find_days, iter_items, read_manifest

SAR_MODE = "WV"
POLARIZATION = {"VV": "SV", "HH": "SH"}  # WV is single polarization

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
# Null outside the footprint, or when the SLC SAFE is missing: not an error.
NULLABLE = {
    "sar_lat", "sar_lon", "sar_incidence_angle", "sar_elevation_angle", "sar_ground_heading",
    "sar_ground_heading_uncertainty", "sar_distance_to_coast", "legacy_usage", "class_1",
    "pos_1", "ref_flag", "ref_time_delta_min",
}
STRINGS = {"primary_key", "sar_path_ocn", "sar_path_slc", "sar_safe_slc", "sar_safe_ocn",
           "ref_id", "ref_source", "legacy_usage", "class_1"}
TIMES = {"sar_time", "ref_time"}


def columns(param_names, ancillary=()) -> list:
    """All catalogue columns for a reference whose parameters are ``param_names``.

    ``ancillary`` lists the columns of the ancillary sources (those not already in ``TAIL``).
    """
    return [*HEAD, *param_names, *TAIL, *EXTRA, *[c for c in ancillary if c not in TAIL]]


def catalogue_schema(param_names, ancillary=()) -> pa.Schema:
    """The Arrow schema of a catalogue: explicit, so no column depends on the data it holds."""
    def kind(name):
        if name in STRINGS:
            return pa.string()
        if name in TIMES:
            return pa.timestamp("us")
        return pa.int64() if name == "ref_flag" else pa.float64()

    return pa.schema([(name, kind(name)) for name in columns(param_names, ancillary)])


def check_catalogue(frame: pd.DataFrame, param_names, ancillary=()) -> dict:
    """Fail on duplicate keys or nulls in mandatory columns; return counts for the summary."""
    duplicated = frame["primary_key"][frame["primary_key"].duplicated(keep=False)]
    if len(duplicated):
        raise ValueError(f"{duplicated.nunique()} duplicated primary_key, first: "
                         f"{sorted(duplicated.unique())[:3]}")
    nulls = {}
    for name in columns(param_names, ancillary):
        if name in NULLABLE or name in param_names or name in ancillary:
            continue
        count = int(frame[name].isna().sum())
        if count:
            nulls[name] = count
    if nulls:
        raise ValueError(f"nulls in mandatory columns: {nulls}")
    if not frame["sar_lon"].between(-180, 180).all():
        raise ValueError("sar_lon outside [-180, 180]")
    return {
        "rows": len(frame),
        "null_angles": int(frame["sar_incidence_angle"].isna().sum()),
        "null_distance_to_coast": int(frame["sar_distance_to_coast"].isna().sum()),
    }


# --------------------------------------------------------------------------------------------
# SAFE references and the SLC twin
# --------------------------------------------------------------------------------------------
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


# --------------------------------------------------------------------------------------------
# Rows
# --------------------------------------------------------------------------------------------
def item_rows(item: dict, reference: Reference, slc_lookup=slc_safe_dir) -> tuple:
    """(rows, skipped) for one OCN WV Item.

    ``skipped`` is "" when the Item produced rows, else the reason: ``mode``, ``product_type``
    or ``no_slc`` (the archive holds no SLC SAFE for the OCN one). Each row carries
    ``_satellite``, ``_polarization`` and ``_product`` for grouping; the writer drops them.
    """
    props = item["properties"]
    if props["sarmatchup:sar_instrument_mode"] != SAR_MODE:
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
    sar_time = stac_time(props["start_datetime"]).replace(microsecond=0)
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
                "sar_incidence_angle": as_float(point.get("sar_incidence_angle")),
                "sar_elevation_angle": as_float(point.get("sar_elevation_angle")),
                "sar_ground_heading": as_float(point.get("sar_ground_heading")),
                "sar_ground_heading_uncertainty": as_float(
                    point.get("sar_ground_heading_uncertainty")),
                "sar_distance_to_coast": as_float(point.get("sar_distance_to_coast")),
                "ref_lon": float(lon), "ref_lat": float(lat),
                "ref_time": stac_time(point["ref_time_utc"]).replace(microsecond=0),
                "ref_time_delta_min": as_float(point.get("delta_diff_time_minutes")),
                "ref_id": group["reference_file"], "ref_source": group["reference_source"],
                "ref_flag": as_flag(None if reference.flag is None else point.get(reference.flag)),
                "_product": product,
            }
            for column, field in reference.params:
                row[column] = as_float(point.get(field))
            rows.append(row)
    return rows, ""


class WV(Mode):
    """WV imagettes from soba_matchups OCN WV Items."""

    name = SAR_MODE
    # Ancillary sources that only make sense per WV imagette.
    sources = ("tengeop", "ecmwf", "era5", "ww3", "gebco", "seaice", "imerg")

    def __init__(self, slc_lookup=slc_safe_dir):
        self.slc_lookup = slc_lookup

    def schema(self, param_names, ancillary):
        return catalogue_schema(param_names, ancillary)

    def check(self, rows, param_names, ancillary):
        return check_catalogue(pd.DataFrame(rows), param_names, ancillary)

    def rows(self, product_dirs, start, end, counts, satellites=None):
        for product_dir in product_dirs:
            for day in find_days(product_dir, start, end):
                reference = reference_for(read_manifest(day)["reference_product"])
                names = [column for column, _ in reference.params]
                for item in iter_items(day):
                    counts["items"] += 1
                    rows, skipped = item_rows(item, reference, self.slc_lookup)
                    if skipped:
                        counts[f"skipped_{skipped}"] += 1
                        continue
                    for row in rows:
                        key = (row["_satellite"], row["_polarization"], row["_product"])
                        if satellites and key[0] not in satellites:
                            continue
                        yield key, names, row
