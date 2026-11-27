"""Build co-aligned catalogue Parquets from soba_matchups day folders: the part every mode shares.

A mode (``wv.WV``, ``iw.IW``) says which days to read, how an Item becomes rows, and the columns,
schema and checks of its catalogues; this module streams the rows into one file per (satellite,
polarization, reference product name), named as in the SOBA spec section 1.1, fills the
ancillary columns (``ancillary`` package) and writes the global attributes. Writes catalogues
only: curation (``curation.py``) reads them like any other.

Rows are streamed: every group buffers at most ``FLUSH_ROWS`` rows, sorts them by SAR time and
appends them to a hidden ``.partial`` file as one row group. When the run ends, the row groups
are copied into the final file, which now carries its dates and row count among the global
attributes, and renamed into place. A run that fails leaves no file behind, and an existing
catalogue is never overwritten. Memory depends on ``FLUSH_ROWS``, not on the length of the run;
the one thing kept for the whole run is an 8-byte digest per ``primary_key``.
"""

from __future__ import annotations

import hashlib
import warnings
from collections import Counter
from datetime import date
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from ..pdf_support import library_version
from .ancillary import make as make_source

FLUSH_ROWS = 100_000  # rows buffered per output file before a row group is written


class Mode:
    """What one SAR mode contributes to its catalogues (see ``wv.WV``, ``iw.IW``)."""

    name = ""        # SAR mode in the file names and the ``sar_mode`` attribute
    sources = ()     # ancillary sources (recipe names) that apply to this mode

    def rows(self, product_dirs, start, end, counts, satellites=None):
        """Yield ``(file key, parameter column names, row)`` for every row, day by day.

        ``counts`` (a Counter) tallies Items read and the reasons Items produced no row.
        """
        raise NotImplementedError

    def schema(self, param_names, ancillary) -> pa.Schema:
        raise NotImplementedError

    def check(self, rows, param_names, ancillary) -> dict:
        """Raise on rows that break the mode's rules; return counts (unused by the writer)."""
        raise NotImplementedError

    def attributes(self, sources) -> dict:
        """Global attributes of this mode, beyond the shared ones."""
        return {}

    def counts(self) -> dict:
        """Run counters of this mode, added to the summary."""
        return {}


def catalogue_name(satellite, polarization, product, start, stop, production, version,
                   mode="WV") -> str:
    """Spec 1.1 name: S1X_coaligned_catalogue_<mode>_<start>_<stop>_<production>_<pol>_
    <product>_<X.Y>.parquet."""
    return (f"{satellite}_coaligned_catalogue_{mode}_{start}_{stop}_{production}_"
            f"{polarization}_{product}_{version}.parquet")


def _wv(slc_lookup=None):
    from .wv import WV, slc_safe_dir

    return WV(slc_lookup or slc_safe_dir)


def iter_rows(product_dirs, start, end, counts, slc_lookup=None, satellites=None, mode=None):
    """Every row of the days, as the mode yields them (WV by default)."""
    return (mode or _wv(slc_lookup)).rows(product_dirs, start, end, counts, satellites)


def collect(product_dirs, start, end, slc_lookup=None, satellites=None, sources=(),
            mode=None) -> tuple:
    """All rows of a (small) run as one DataFrame per file: ``(frames, counts, param_names)``.

    For inspection and tests; ``build_catalogues`` streams instead of holding every row.
    ``sources`` are ancillary readers (``Source``) that fill their columns.
    """
    counts, by_file, params = Counter(), {}, {}
    for key, names, row in iter_rows(product_dirs, start, end, counts, slc_lookup, satellites,
                                     mode):
        by_file.setdefault(key, []).append(row)
        params[key] = names
    for rows in by_file.values():
        for source in sources:
            source.fill(rows)
    for source in sources:
        counts.update(source.counts())
    return {key: pd.DataFrame(rows) for key, rows in by_file.items()}, counts, params


def _digest(key: str) -> bytes:
    return hashlib.blake2b(key.encode(), digest_size=8).digest()


class _Sink:
    """The rows of one output file, written as they arrive and finished by ``finish``."""

    def __init__(self, key, param_names, out_dir, sources=(), mode=None):
        self.mode = mode or _wv()
        self.key, self.param_names, self.out_dir = key, list(param_names), Path(out_dir)
        self.sources = list(sources)
        self.ancillary = [c for source in self.sources for c in source.columns]
        self.schema = self.mode.schema(param_names, self.ancillary)
        self.buffer, self.digests, self.ref_sources = [], set(), set()
        self.rows = 0
        self.dropped = []                 # keys of repeated rows, first one seen is kept
        self.first = self.last = None
        satellite, polarization, product = key
        self.partial = self.out_dir / f".{satellite}_{polarization}_{product}.rows.partial"
        self.writer = None

    def add(self, row):
        digest = _digest(row["primary_key"])
        if digest in self.digests:
            self.dropped.append(row["primary_key"])
            return
        self.digests.add(digest)
        self.buffer.append(row)
        if len(self.buffer) >= FLUSH_ROWS:
            self.flush()

    def flush(self):
        if not self.buffer:
            return
        rows = sorted(self.buffer, key=lambda r: (r["sar_time"], r["primary_key"]))
        self.buffer = []
        for source in self.sources:
            source.fill(rows)
        self.mode.check(rows, self.param_names, self.ancillary)
        self.ref_sources.update(row["ref_source"] for row in rows)
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
            satellite, polarization, product, start, stop, production, version, self.mode.name)
        if path.exists():
            raise FileExistsError(f"refusing to overwrite {path}")
        meta = {
            "sar_mode": self.mode.name, "satellite": satellite, "polarization": polarization,
            "ref_product": product, "version": version, "start_date": start, "end_date": stop,
            "creation_date": production, "rows": str(self.rows), **meta_for(self),
        }
        if self.dropped:
            meta["rows_dropped_duplicate_key"] = str(len(self.dropped))
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


# The reference kind of the ``source <kind>`` global attribute, from the product name's start.
REFERENCE_KIND = {"KNMI": "scat", "CMEM": "alti", "CCI-": "alti", "SWOT": "swot", "PODA": "swot"}


def _global_attributes(ref_sources, key, ancillary, production) -> dict:
    """The spec's mandatory global attributes (section 4) of one catalogue."""
    kind = REFERENCE_KIND[key[2][:4]]
    return {
        f"source {kind}": ", ".join(sorted(ref_sources)),
        "source ancillary datasets": (
            ", ".join(source.description for source in ancillary) or "none"),
        "library used to produce the parquet": "soba_reference_repo",
        "library version": library_version(),
        "creation date": production,
    }


def build_catalogues(product_dirs, start, end, out_dir, version="0.1", slc_lookup=None,
                     satellites=None, production=None, stac_root="", recipe_path=None,
                     ancillary_paths=None, mode=None, meta=None) -> tuple:
    """Merge STAC day folders into catalogue Parquets. Returns (paths, counts).

    ``mode`` is a ``Mode`` (default: WV, its SLC twins found with ``slc_lookup``); ``meta`` adds
    global attributes.
    """
    mode = mode or _wv(slc_lookup)
    production = production or date.today().strftime("%Y%m%d")
    paths_by_source = dict(ancillary_paths or {})
    for name in paths_by_source:
        if name not in mode.sources:
            raise ValueError(f"ancillary source {name!r} does not apply to {mode.name} "
                             f"catalogues; it takes {sorted(mode.sources)}")
    sources = [make_source(name, folder) for name, folder in paths_by_source.items()]
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    counts, sinks = Counter(), {}

    def meta_for(sink):
        attributes = {"stac_root": str(stac_root), "stac_days": f"{start}..{end}"}
        attributes.update(_global_attributes(sink.ref_sources, sink.key, sources, production))
        attributes.update(mode.attributes(sources))
        if recipe_path:
            attributes["recipe_path"] = str(recipe_path)
        for name, folder in paths_by_source.items():
            attributes[f"ancillary path {name}"] = str(folder)
        attributes.update(meta or {})
        return attributes

    paths = []
    try:
        for key, names, row in mode.rows(product_dirs, start, end, counts, satellites):
            if key not in sinks:
                sinks[key] = _Sink(key, names, out_dir, sources, mode)
            sinks[key].add(row)
        for key in sorted(sinks):
            paths.append(sinks[key].finish(production, version, meta_for))
            counts["rows"] += sinks[key].rows
            if sinks[key].dropped:
                counts["dropped_duplicate_key"] += len(sinks[key].dropped)
                warnings.warn(
                    f"{len(sinks[key].dropped)} duplicated primary_key dropped (first row kept) "
                    f"in {paths[-1].name}, first: {sinks[key].dropped[:3]}", stacklevel=2)
    except BaseException:
        for sink in sinks.values():
            sink.abort()
        for finished in paths:
            finished.unlink(missing_ok=True)
        for leftover in out_dir.glob(".*.partial"):
            leftover.unlink(missing_ok=True)
        raise
    for source in sources:
        counts.update(source.counts())
    counts.update(mode.counts())
    return paths, dict(counts)
