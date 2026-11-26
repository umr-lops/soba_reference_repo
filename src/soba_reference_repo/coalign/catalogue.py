"""Build WV co-aligned catalogue Parquets from soba_matchups day folders.

One file per (satellite, polarization, reference product name), named as in the SOBA spec
section 1.1. Writes catalogues only: curation (``curation.py``) reads them like any other.

Rows are streamed: every group buffers at most ``FLUSH_ROWS`` rows, sorts them by SAR time and
appends them to a hidden ``.partial`` file as one row group. When the run ends, the row groups
are copied into the final file, which now carries its dates and row count among the global
attributes, and renamed into place. A run that fails leaves no file behind, and an existing
catalogue is never overwritten. Memory depends on ``FLUSH_ROWS``, not on the length of the run;
the one thing kept for the whole run is an 8-byte digest per ``primary_key``.
"""

from __future__ import annotations

import hashlib
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
FLUSH_ROWS = 100_000  # rows buffered per output file before a row group is written
# Null outside the footprint, or when the SLC SAFE is missing: not an error.
NULLABLE = {
    "sar_lat", "sar_lon", "sar_incidence_angle", "sar_elevation_angle", "sar_ground_heading",
    "sar_ground_heading_uncertainty", "sar_distance_to_coast", "legacy_usage", "class_1",
    "pos_1", "ref_flag", "ref_time_delta_min",
}
STRINGS = {"primary_key", "sar_path_ocn", "sar_path_slc", "sar_safe_slc", "sar_safe_ocn",
           "ref_id", "ref_source", "legacy_usage", "class_1"}
TIMES = {"sar_time", "ref_time"}


def catalogue_schema(param_names) -> pa.Schema:
    """The Arrow schema of a catalogue: explicit, so no column depends on the data it holds."""
    def kind(name):
        if name in STRINGS:
            return pa.string()
        if name in TIMES:
            return pa.timestamp("us")
        return pa.int64() if name == "ref_flag" else pa.float64()

    return pa.schema([(name, kind(name)) for name in columns(param_names)])


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


def iter_rows(product_dirs, start, end, counts, slc_lookup=slc_safe_dir, satellites=None,
              tengeop=None):
    """Yield ``(file key, parameter column names, row)`` for every catalogue row, day by day.

    ``counts`` (a Counter) tallies Items read and the reasons Items produced no row. With
    ``tengeop`` (a ``TenGeoP`` reader) ``class_1`` and ``pos_1`` are filled and the rows left
    without a class are counted as ``no_class``.
    """
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
                    yield key, names, row


def collect(product_dirs, start, end, slc_lookup=slc_safe_dir, satellites=None,
            tengeop=None) -> tuple:
    """All rows of a (small) run as one DataFrame per file: ``(frames, counts, param_names)``.

    For inspection and tests; ``build_catalogues`` streams instead of holding every row.
    """
    counts, by_file, params = Counter(), {}, {}
    for key, names, row in iter_rows(product_dirs, start, end, counts, slc_lookup, satellites,
                                     tengeop):
        by_file.setdefault(key, []).append(row)
        params[key] = names
    return {key: pd.DataFrame(rows) for key, rows in by_file.items()}, counts, params


def _digest(key: str) -> bytes:
    return hashlib.blake2b(key.encode(), digest_size=8).digest()


class _Sink:
    """The rows of one output file, written as they arrive and finished by ``finish``."""

    def __init__(self, key, param_names, out_dir):
        self.key, self.param_names, self.out_dir = key, list(param_names), Path(out_dir)
        self.schema = catalogue_schema(param_names)
        self.buffer, self.digests, self.sources = [], set(), set()
        self.rows = self.null_angles = self.null_distance = 0
        self.first = self.last = None
        satellite, polarization, product = key
        self.partial = self.out_dir / f".{satellite}_{polarization}_{product}.rows.partial"
        self.writer = None

    def add(self, row):
        self.buffer.append(row)
        if len(self.buffer) >= FLUSH_ROWS:
            self.flush()

    def flush(self):
        if not self.buffer:
            return
        rows = sorted(self.buffer, key=lambda r: (r["sar_time"], r["primary_key"]))
        self.buffer = []
        summary = check_catalogue(pd.DataFrame(rows), self.param_names)
        for row in rows:
            digest = _digest(row["primary_key"])
            if digest in self.digests:
                raise ValueError(f"duplicated primary_key, first: {[row['primary_key']]}")
            self.digests.add(digest)
            self.sources.add(row["ref_source"])
        self.null_angles += summary["null_angles"]
        self.null_distance += summary["null_distance_to_coast"]
        self.rows += len(rows)
        self.first = min(filter(None, [self.first, rows[0]["sar_time"]]))
        self.last = max(filter(None, [self.last, max(r["sar_time"] for r in rows)]))
        if self.writer is None:
            self.writer = pq.ParquetWriter(self.partial, self.schema)
        self.writer.write_table(pa.Table.from_pylist(rows, schema=self.schema))

    def finish(self, production, version, meta_for) -> Path:
        """Write the final file (global attributes included) and return its path."""
        self.flush()
        self.writer.close()
        self.writer = None
        satellite, polarization, product = self.key
        start, stop = self.first.strftime("%Y%m%d"), self.last.strftime("%Y%m%d")
        path = self.out_dir / catalogue_name(
            satellite, polarization, product, start, stop, production, version)
        if path.exists():
            raise FileExistsError(f"refusing to overwrite {path}")
        meta = {
            "sar_mode": SAR_MODE, "satellite": satellite, "polarization": polarization,
            "ref_product": product, "version": version, "start_date": start, "end_date": stop,
            "creation_date": production, "rows": str(self.rows), **meta_for(self),
        }
        final = path.with_name(f".{path.name}.partial")
        source = pq.ParquetFile(self.partial)
        with pq.ParquetWriter(final, self.schema.with_metadata(meta)) as writer:
            for group in range(source.num_row_groups):
                writer.write_table(source.read_row_group(group))
        self.partial.unlink()
        final.rename(path)
        return path

    def abort(self):
        if self.writer is not None:
            self.writer.close()
            self.writer = None
        self.partial.unlink(missing_ok=True)


def _global_attributes(sources, key, with_classes, production) -> dict:
    """The spec's mandatory global attributes (section 4) of one catalogue."""
    kind = {"KNMI": "scat", "CMEM": "alti", "CCI-": "alti", "SWOT": "swot"}[key[2][:4]]
    ancillary = ["class: TenGeoP SAR Sentinel-1 surface roughness classification"]
    return {
        f"source {kind}": ", ".join(sorted(sources)),
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
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    counts, sinks = Counter(), {}

    def meta_for(sink):
        meta = {"stac_root": str(stac_root), "stac_days": f"{start}..{end}"}
        meta.update(_global_attributes(sink.sources, sink.key, bool(tengeop), production))
        if recipe_path:
            meta["recipe_path"] = str(recipe_path)
        for name, folder in (ancillary_paths or {}).items():
            meta[f"ancillary path {name}"] = str(folder)
        return meta

    try:
        for key, names, row in iter_rows(product_dirs, start, end, counts, slc_lookup,
                                         satellites, tengeop):
            if key not in sinks:
                sinks[key] = _Sink(key, names, out_dir)
            sinks[key].add(row)
        paths = []
        for key in sorted(sinks):
            paths.append(sinks[key].finish(production, version, meta_for))
            counts["rows"] += sinks[key].rows
    except BaseException:
        for sink in sinks.values():
            sink.abort()
        for leftover in out_dir.glob(".*.partial"):
            leftover.unlink(missing_ok=True)
        raise
    if tengeop is not None:
        counts["missing_tengeop_summaries"] = tengeop.missing_summaries
    return paths, dict(counts)
