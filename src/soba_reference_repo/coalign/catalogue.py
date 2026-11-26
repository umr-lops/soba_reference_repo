"""Build WV co-aligned catalogue Parquets from soba_matchups day folders.

One file per (satellite, polarization, reference product name), named as in the SOBA spec
section 1.1. Writes catalogues only: curation (``curation.py``) reads them like any other.
"""

from __future__ import annotations

from collections import Counter
from datetime import date
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from ..pdf_support import library_version
from .products import reference_for
from .rows import columns, item_rows
from .sar import slc_safe_dir
from .stac import find_days, iter_items, read_manifest
from .tengeop import TenGeoP

SAR_MODE = "WV"
# Null outside the footprint, or when the SLC SAFE is missing: not an error.
NULLABLE = {
    "sar_lat", "sar_lon", "sar_incidence_angle", "sar_elevation_angle", "sar_ground_heading",
    "sar_ground_heading_uncertainty", "sar_distance_to_coast", "legacy_usage", "class_1",
    "pos_1", "ref_flag", "ref_time_delta_min",
}
INT_TYPE_FLAG = "ref_flag"


def catalogue_name(satellite, polarization, product, start, stop, production, version) -> str:
    """Spec 1.1 name: S1X_coaligned_catalogue_<mode>_<start>_<stop>_<production>_<pol>_
    <product>_<X.Y>.parquet."""
    return (f"{satellite}_coaligned_catalogue_{SAR_MODE}_{start}_{stop}_{production}_"
            f"{polarization}_{product}_{version}.parquet")


def check_catalogue(frame: pd.DataFrame, param_names) -> dict:
    """Fail on duplicate keys or nulls in mandatory columns; return counts for the summary."""
    duplicated = frame["primary_key"][frame["primary_key"].duplicated(keep=False)]
    if len(duplicated):
        raise ValueError(f"{duplicated.nunique()} duplicated primary_key, first: "
                         f"{sorted(duplicated.unique())[:3]}")
    nulls = {}
    for name in columns(param_names):
        if name in NULLABLE or name in param_names:
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


def collect(product_dirs, start, end, slc_lookup=slc_safe_dir, satellites=None,
            tengeop=None) -> tuple:
    """Read every day of every product into one frame per output file.

    Returns ``({(satellite, polarization, product): DataFrame}, counts, param_names)`` where
    ``counts`` tallies Items read and the reasons Items produced no row. With ``tengeop`` (a
    ``TenGeoP`` reader) ``class_1`` and ``pos_1`` are filled and the rows left without a class
    are counted as ``no_class``.
    """
    by_file, counts, params = {}, Counter(), {}
    for product_dir in product_dirs:
        for day in find_days(product_dir, start, end):
            reference = reference_for(read_manifest(day)["reference_product"])
            names = [column for column, _ in reference.params]
            for item in iter_items(day):
                counts["items"] += 1
                rows, skipped = item_rows(item, reference, slc_lookup)
                if skipped:
                    counts[f"skipped_{skipped}"] += 1
                    continue
                for row in rows:
                    key = (row["_satellite"], row["_polarization"], row["_product"])
                    if satellites and key[0] not in satellites:
                        continue
                    if tengeop is not None:
                        row["class_1"], row["pos_1"] = tengeop.lookup(
                            row["sar_safe_slc"].split("/")[-1].split(":")[0],
                            int(row["sar_safe_slc"].rsplit(":WV_", 1)[1]))
                        counts["no_class"] += row["class_1"] is None
                    by_file.setdefault(key, []).append(row)
                    params[key] = names
    frames = {key: pd.DataFrame(rows) for key, rows in by_file.items()}
    return frames, counts, params


# Columns that may be entirely null in a file still get a real type, not Arrow's null type.
COLUMN_TYPES = {"legacy_usage": pa.string(), "class_1": pa.string(), "pos_1": pa.float64(),
                "ref_flag": pa.int64()}


def _typed(table: pa.Table, param_names=()) -> pa.Table:
    """Give all-null columns their declared type (pandas writes them as the Arrow null type).

    Reference parameters are doubles, whatever a given file happens to contain.
    """
    kinds = {**COLUMN_TYPES, **{name: pa.float64() for name in param_names}}
    for name, kind in kinds.items():
        if name in table.column_names and pa.types.is_null(table.schema.field(name).type):
            table = table.set_column(table.column_names.index(name), name,
                                     pa.nulls(table.num_rows, kind))
    return table


def write_catalogue(frame, key, param_names, out_dir, production, version,
                    extra_meta=None) -> Path:
    """Check one frame and write it; never overwrites an existing file."""
    satellite, polarization, product = key
    summary = check_catalogue(frame, param_names)
    frame = frame[columns(param_names)]
    frame = frame.sort_values(["sar_time", "primary_key"]).reset_index(drop=True)
    start = frame["sar_time"].min().strftime("%Y%m%d")
    stop = frame["sar_time"].max().strftime("%Y%m%d")
    path = Path(out_dir) / catalogue_name(
        satellite, polarization, product, start, stop, production, version)
    if path.exists():
        raise FileExistsError(f"refusing to overwrite {path}")
    table = _typed(pa.Table.from_pandas(frame, preserve_index=False), param_names)
    meta = {
        "sar_mode": SAR_MODE, "satellite": satellite, "polarization": polarization,
        "ref_product": product, "version": version, "start_date": start, "end_date": stop,
        "creation_date": production, "rows": str(summary["rows"]), **(extra_meta or {}),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table.replace_schema_metadata(meta), path)
    return path


def _global_attributes(frame, key, with_classes, production) -> dict:
    """The spec's mandatory global attributes (section 4) of one catalogue."""
    kind = {"KNMI": "scat", "CMEM": "alti", "CCI-": "alti", "SWOT": "swot"}[key[2][:4]]
    ancillary = ["class: TenGeoP SAR Sentinel-1 surface roughness classification"]
    return {
        f"source {kind}": ", ".join(sorted(frame["ref_source"].unique())),
        "source ancillary datasets": "; ".join(ancillary) if with_classes else "",
        "library used to produce the parquet": "soba_reference_repo",
        "library version": library_version(),
        "creation date": production,
    }


def build_catalogues(product_dirs, start, end, out_dir, version="0.1", slc_lookup=slc_safe_dir,
                     satellites=None, production=None, stac_root="", tengeop_root=None,
                     recipe_path=None, ancillary_paths=None) -> tuple:
    """Merge STAC day folders into catalogue Parquets. Returns (paths, counts)."""
    production = production or date.today().strftime("%Y%m%d")
    tengeop = TenGeoP(tengeop_root) if tengeop_root else None
    frames, counts, params = collect(product_dirs, start, end, slc_lookup, satellites, tengeop)
    if tengeop is not None:
        counts["missing_tengeop_summaries"] = tengeop.missing_summaries
    paths = []
    for key in sorted(frames):
        meta = {"stac_root": str(stac_root), "stac_days": f"{start}..{end}"}
        meta.update(_global_attributes(frames[key], key, bool(tengeop), production))
        if recipe_path:
            meta["recipe_path"] = str(recipe_path)
        for name, folder in (ancillary_paths or {}).items():
            meta[f"ancillary path {name}"] = str(folder)
        paths.append(write_catalogue(frames[key], key, params[key], out_dir, production,
                                     version, meta))
    return paths, dict(counts)
