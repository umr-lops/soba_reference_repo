"""IW co-aligned catalogue Parquets from soba_matchups GRD matchups (spec 1.5.0, IW section).

A row is one reference point matched with one IW **GRD** scene (``primary_key`` = GRD SAFE name +
ref_lon + ref_lat). The GRD matchups are read from ``<stac_root>/<collection>/<YYYY-MM-DD>/grd/iw``
(``collection`` e.g. ``swot/d2``). The OCN and SLC twins of each GRD (same acquisition, slice
times differing by a second or so) are found in the CDSE daily metadata Parquets with cdsodatacli
(``match_s1_prodtypes`` parquet backend, same platform, orbit, datatake and polarisation, closest
start time within 8 s); without ``sar_parquet_root`` their columns stay empty.

The datasets need SAR metadata only: a ``sar_path_*`` column is empty (null) when the SAFE is not
on the configured archives. Ancillary columns without a reader yet (sea ice, bathymetry, WW3,
ECMWF) are written empty and named in the ``ancillary columns not filled`` global attribute, so a
catalogue can be built and checked before every source is ready.

Rows are streamed: each output file buffers at most ``FLUSH_ROWS`` rows before a row group is
written; the final file is written under a hidden name and renamed, and never overwrites one.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import tomllib
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from ..pdf_support import library_version

SAR_MODE = "IW"
FLUSH_ROWS = 100_000
KEY_DECIMALS = 4  # lon/lat decimals in primary_key (spec: at least 2)
DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
PLATFORM = {"sentinel-1a": "S1A", "sentinel-1b": "S1B", "sentinel-1c": "S1C", "sentinel-1d": "S1D"}
# File-name polarization (spec 1.1) from the two polarization letters of a SAFE name. A product
# holding one channel of a dual-polarization acquisition (HH, VV) is filed with the single ones.
POLARIZATION = {"SV": "SV", "SH": "SH", "DV": "DV", "DH": "DH", "VV": "SV", "HH": "SH",
                "HV": "HV", "VH": "VH"}


@dataclass(frozen=True)
class IWReference:
    """How one soba_matchups reference product maps onto the IW catalogue."""

    params: tuple  # ((catalogue column, STAC point field), ...): the reference parameters
    flag: str | None  # STAC point field copied to ref_flag
    extras: tuple  # STAC point fields kept as they are (quality, ice, rain flags ...)


SWOT = IWReference(
    params=(("swh_swot", "swh_karin"), ("windspeed_swot", "wind_speed_karin"),
            ("windspeed_2_swot", "wind_speed_karin_2")),
    flag="swh_karin_qual",
    extras=("swh_karin_uncert", "wind_speed_karin_qual", "wind_speed_karin_2_qual",
            "dynamic_ice_flag", "rain_flag"),
)
REFERENCES = {"PODAAC_SWOT_KARIN_L2_WINDWAVE_": SWOT}  # label prefix -> mapping (release follows)

HEAD = (
    "primary_key", "sar_time", "sar_incidence_angle", "sar_elevation_angle", "sar_ground_heading",
    "sar_distance_to_along_track_edge", "sar_distance_to_cross_track_edge",
    "sar_path_ocn", "sar_path_slc", "sar_path_grd", "sar_safe_grd", "sar_safe_slc", "sar_safe_ocn",
    "ref_lon", "ref_lat",
)
TAIL = ("ref_time", "ref_id", "ref_distance_to_coast")
# Spec ancillary columns; no IW reader yet, so written empty for now.
ANCILLARY = ("sea_ice_concentration", "bathymetry", "WW3_Hs", "ECMWF_Wind_Speed",
             "ECMWF_Wind_Direction")
# Kept beyond the spec: useful for curation.
EXTRA = ("ref_flag", "ref_source", "ref_time_delta_min", "sar_ground_heading_uncertainty",
         "sar_orbit_direction")
STRINGS = {"primary_key", "sar_path_ocn", "sar_path_slc", "sar_path_grd", "sar_safe_grd",
           "sar_safe_slc", "sar_safe_ocn", "ref_id", "ref_source", "sar_orbit_direction"}
TIMES = {"sar_time", "ref_time"}
INTEGERS = {"ref_flag", "swh_karin_qual", "wind_speed_karin_qual", "wind_speed_karin_2_qual",
            "dynamic_ice_flag", "rain_flag"}
MANDATORY = {"primary_key", "sar_time", "sar_safe_grd", "ref_lon", "ref_lat", "ref_time", "ref_id"}


def reference_for(label: str) -> tuple[IWReference, str]:
    """(mapping, ``<refproductname>``) of a soba_matchups ``reference_product`` label.

    The SWOT name carries the release of the run: ``PODAAC_SWOT_KARIN_L2_WINDWAVE_D2`` ->
    ``PODAAC-SWOT-KARIN-L2-WINDWAVE-D2`` (spec 1.5.0).
    """
    for prefix, reference in REFERENCES.items():
        if label.startswith(prefix) and len(label) > len(prefix):
            return reference, label.replace("_", "-")
    raise ValueError(f"no IW catalogue mapping for reference product {label!r}; "
                     f"known: {sorted(p + '<release>' for p in REFERENCES)}")


def columns(reference: IWReference) -> list:
    return [*HEAD, *(c for c, _ in reference.params), *TAIL, *ANCILLARY, *EXTRA,
            *reference.extras]


def schema(reference: IWReference) -> pa.Schema:
    def kind(name):
        if name in STRINGS:
            return pa.string()
        if name in TIMES:
            return pa.timestamp("us")
        return pa.int64() if name in INTEGERS else pa.float64()

    return pa.schema([(name, kind(name)) for name in columns(reference)])


def catalogue_name(satellite, polarization, product, start, stop, production, version) -> str:
    return (f"{satellite}_coaligned_catalogue_{SAR_MODE}_{start}_{stop}_{production}_"
            f"{polarization}_{product}_{version}.parquet")


# --------------------------------------------------------------------------------------------
# SAFE names, archive paths, twins
# --------------------------------------------------------------------------------------------
START = re.compile(r"_(\d{8}T\d{6})_")


def _start(safe: str) -> datetime:
    """Start time of a SAFE name (the first stamp: ``..._1SDV_<start>_<stop>_...``)."""
    return datetime.strptime(START.search(safe).group(1), "%Y%m%dT%H%M%S")


def _folder(safe: str) -> tuple[str, str, str]:
    """(sentinel-1x, product folder, year/doy) of a SAFE name."""
    mission, mode, kind = safe[:3], safe[4:6], safe[7:11]
    suffix = {"OCN_": "OCN__2S", "SLC_": "SLC__1S"}.get(kind, f"{kind}_1S")
    return (f"sentinel-1{mission[-1].lower()}", f"{mission}_{mode}_{suffix}",
            _start(safe).strftime("%Y/%j"))


def safe_slc_ref(safe: str) -> str:
    """Spec form of ``sar_safe_slc``: ``S1A_IW_SLC__1S/<year>/<doy>/<SAFE>``."""
    _, folder, yearday = _folder(safe)
    return f"{folder}/{yearday}/{safe}"


class Archives:
    """Where SAFEs are looked for; a SAFE found nowhere gives an empty path.

    ``ocn``: the CERSAT provider tree of the L2 products
    (``sentinel-1x/c-band_sar/s1x_iw_ocn__2s/<year>/<doy>/``); ``l1``: roots in the MPC layout
    (``sentinel-1x/L1/IW/<S1x>_IW_<GRDH_1S|SLC__1S>/<year>/<doy>/``), tried in order.
    """

    def __init__(self, ocn=None, l1=()):
        self.ocn = Path(ocn) if ocn else None
        self.l1 = [Path(p) for p in l1]
        self.found = Counter()

    def path(self, safe: str | None) -> str | None:
        if not safe:
            return None
        sat, folder, yearday = _folder(safe)
        kind = safe[7:10]
        if kind == "OCN":
            candidates = [self.ocn / sat / "c-band_sar" / folder.lower() / yearday / safe] \
                if self.ocn else []
        else:
            candidates = [root / sat / "L1" / safe[4:6] / folder / yearday / safe
                          for root in self.l1]
        for candidate in candidates:
            if candidate.is_dir():
                self.found[kind] += 1
                return str(candidate)
        return None


def cdsodatacli_matcher(parquet_root):
    """A twin matcher on the CDSE daily metadata Parquets (cdsodatacli >= 2026.11.27)."""
    from cdsodatacli.scripts.match_s1_product_types import find_products_in_parquet

    logger = logging.getLogger("soba_reference_repo.coalign.iw.twins")

    def match(names: list, target: str) -> dict:
        results = find_products_in_parquet(list(names), target, Path(parquet_root), logger,
                                           defaultdict(int))
        return {r["source_id"]: r.get("target_name") for r in results}

    return match


# --------------------------------------------------------------------------------------------
# Rows
# --------------------------------------------------------------------------------------------
def _time(value: str) -> datetime:
    value = value.rstrip("Z")
    head, _, frac = value.partition(".")
    return datetime.fromisoformat(f"{head}.{(frac + '000000')[:6]}" if frac else head)


def _float(value):
    return None if value is None else float(value)


def _int(value):
    if value is None:
        return None
    if float(value) != int(value):
        raise ValueError(f"flag must be an integer bit mask, got {value!r}")
    return int(value)


def item_rows(item: dict, reference: IWReference, twins: dict, archives: Archives) -> tuple:
    """(rows, skipped) for one GRD IW Item; ``twins`` maps the GRD SAFE to (OCN, SLC) names."""
    props = item["properties"]
    if props["sarmatchup:sar_instrument_mode"] != SAR_MODE:
        return [], "mode"
    if props["sarmatchup:sar_product_type"] != "GRD":
        return [], "product_type"
    grd = props["sarmatchup:sar_safe"]
    ocn, slc = twins.get(grd, (None, None))
    base = {
        "sar_time": _start(grd),
        "sar_safe_grd": grd, "sar_safe_ocn": ocn, "sar_safe_slc": slc and safe_slc_ref(slc),
        "sar_path_grd": archives.path(grd), "sar_path_ocn": archives.path(ocn),
        "sar_path_slc": archives.path(slc),
        "sar_orbit_direction": props.get("sarmatchup:sar_orbit_direction"),
        "_satellite": PLATFORM[props["sarmatchup:sar_platform"]],
        # S1C_IW_GRDH_1SDV_...: the two polarization letters are at 14:16 (OCN__2SDV too).
        "_polarization": POLARIZATION[grd[14:16]],
        **{name: None for name in ANCILLARY},
    }
    rows = []
    for group in props["sarmatchup:matchups"]:
        for p in group["points"]:
            lon, lat = p["ref_lon"], p["ref_lat"]
            row = {
                **base,
                "primary_key": f"{grd}_{lon:.{KEY_DECIMALS}f}_{lat:.{KEY_DECIMALS}f}",
                "sar_incidence_angle": _float(p.get("sar_incidence_angle")),
                "sar_elevation_angle": _float(p.get("sar_elevation_angle")),
                "sar_ground_heading": _float(p.get("sar_ground_heading")),
                "sar_ground_heading_uncertainty": _float(p.get("sar_ground_heading_uncertainty")),
                "sar_distance_to_along_track_edge": _float(
                    p.get("sar_distance_to_along_track_edge")),
                "sar_distance_to_cross_track_edge": _float(
                    p.get("sar_distance_to_cross_track_edge")),
                "ref_lon": float(lon), "ref_lat": float(lat),
                "ref_time": _time(p["ref_time_utc"]).replace(microsecond=0),
                "ref_time_delta_min": _float(p.get("delta_diff_time_minutes")),
                "ref_id": group["reference_file"], "ref_source": group["reference_source"],
                "ref_distance_to_coast": _float(p.get("sar_distance_to_coast")),
                "ref_flag": _int(p.get(reference.flag)) if reference.flag else None,
            }
            for column, field in reference.params:
                row[column] = _float(p.get(field))
            for field in reference.extras:
                row[field] = (_int if field in INTEGERS else _float)(p.get(field))
            rows.append(row)
    return rows, ""


# --------------------------------------------------------------------------------------------
# Day folders
# --------------------------------------------------------------------------------------------
def find_days(collection_dir, start: str, end: str) -> list:
    """GRD IW run folders ``<collection>/<YYYY-MM-DD>/grd/iw`` holding a manifest, in day order."""
    days = []
    root = Path(collection_dir)
    for day in sorted(root.iterdir()) if root.is_dir() else []:
        run = day / "grd" / "iw"
        if DAY.match(day.name) and start <= day.name <= end and (run / "manifest.json").is_file():
            days.append(run)
    return days


def _items(run_dir):
    for path in sorted((Path(run_dir) / "items").glob("*.json")):
        yield json.loads(path.read_text())


# --------------------------------------------------------------------------------------------
# Writer
# --------------------------------------------------------------------------------------------
class _Sink:
    """The rows of one output file, streamed to a hidden partial file."""

    def __init__(self, key, reference, out_dir):
        self.key, self.reference, self.out_dir = key, reference, Path(out_dir)
        self.schema = schema(reference)
        self.buffer, self.digests, self.sources, self.dropped = [], set(), set(), []
        self.rows, self.first, self.last, self.writer = 0, None, None, None
        self.partial = self.out_dir / (".{}_{}_{}.rows.partial".format(*key))

    def add(self, row):
        digest = hashlib.blake2b(row["primary_key"].encode(), digest_size=8).digest()
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
        for row in rows:
            missing = [name for name in MANDATORY if row[name] is None]
            if missing:
                raise ValueError(f"empty mandatory columns {missing} in {row['primary_key']}")
        self.sources.update(row["ref_source"] for row in rows)
        self.rows += len(rows)
        self.first = min(filter(None, [self.first, rows[0]["sar_time"]]))
        self.last = max(filter(None, [self.last, rows[-1]["sar_time"]]))
        if self.writer is None:
            self.writer = pq.ParquetWriter(self.partial, self.schema)
        self.writer.write_table(pa.Table.from_pylist(rows, schema=self.schema))

    def finish(self, production, version, meta) -> Path:
        self.flush()
        self.writer.close()
        self.writer = None
        satellite, polarization, product = self.key
        start, stop = self.first.strftime("%Y%m%d"), self.last.strftime("%Y%m%d")
        path = self.out_dir / catalogue_name(satellite, polarization, product, start, stop,
                                             production, version)
        if path.exists():
            raise FileExistsError(f"refusing to overwrite {path}")
        attributes = {
            "sar_mode": SAR_MODE, "satellite": satellite, "polarization": polarization,
            "ref_product": product, "version": version, "start_date": start, "end_date": stop,
            "creation_date": production, "rows": str(self.rows),
            "source swot": ", ".join(sorted(self.sources)), **meta,
        }
        if self.dropped:
            attributes["rows_dropped_duplicate_key"] = str(len(self.dropped))
        final = path.with_name(f".{path.name}.partial")
        source = pq.ParquetFile(self.partial)
        with pq.ParquetWriter(final, self.schema.with_metadata(attributes)) as writer:
            for group in range(source.num_row_groups):
                writer.write_table(source.read_row_group(group))
        self.partial.unlink()
        final.rename(path)
        return path

    def abort(self):
        if self.writer is not None:
            self.writer.close()
        self.partial.unlink(missing_ok=True)


def build_iw_catalogues(collection_dirs, start, end, out_dir, version="0.1", production=None,
                        satellites=None, matcher=None, archives=None, meta=None) -> tuple:
    """Merge GRD IW matchup days into catalogue Parquets. Returns (paths, counts).

    ``matcher(names, target)`` returns ``{grd name: twin name or None}`` for ``target`` ``"OCN_"``
    or ``"SLC_"`` (``cdsodatacli_matcher``); None leaves the twin columns empty.
    """
    production = production or date.today().strftime("%Y%m%d")
    archives = archives or Archives()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    counts, sinks, paths = Counter(), {}, []
    try:
        for collection in collection_dirs:
            for run in find_days(collection, start, end):
                label = json.loads((run / "manifest.json").read_text())["reference_product"]
                reference, product = reference_for(label)
                items = list(_items(run))
                twins = {}
                if matcher is not None and items:
                    names = [i["properties"]["sarmatchup:sar_safe"] for i in items]
                    ocn, slc = matcher(names, "OCN_"), matcher(names, "SLC_")
                    twins = {n: (ocn.get(n), slc.get(n)) for n in names}
                counts["days"] += 1
                for item in items:
                    counts["items"] += 1
                    rows, skipped = item_rows(item, reference, twins, archives)
                    if skipped:
                        counts[f"skipped_{skipped}"] += 1
                        continue
                    if satellites and rows and rows[0]["_satellite"] not in satellites:
                        continue
                    grd = item["properties"]["sarmatchup:sar_safe"]
                    counts["grd_without_ocn"] += twins.get(grd, (None, None))[0] is None
                    counts["grd_without_slc"] += twins.get(grd, (None, None))[1] is None
                    for row in rows:
                        key = (row.pop("_satellite"), row.pop("_polarization"), product)
                        if key not in sinks:
                            sinks[key] = _Sink(key, reference, out_dir)
                        sinks[key].add(row)
        attributes = {
            "source ancillary datasets": "",
            "ancillary columns not filled": ", ".join(ANCILLARY),
            "triplets": "OCN / SLC twins from the CDSE daily metadata Parquets (cdsodatacli)"
                        if matcher is not None else "not associated (no sar_parquet_root)",
            "library used to produce the parquet": "soba_reference_repo",
            "library version": library_version(),
            "creation date": production,
            **(meta or {}),
        }
        for key in sorted(sinks):
            paths.append(sinks[key].finish(production, version, attributes))
            counts["rows"] += sinks[key].rows
            counts["dropped_duplicate_key"] += len(sinks[key].dropped)
    except BaseException:
        for sink in sinks.values():
            sink.abort()
        for finished in paths:
            finished.unlink(missing_ok=True)
        raise
    counts.update({f"paths_found_{kind.lower()}": n for kind, n in archives.found.items()})
    return paths, dict(counts)


# --------------------------------------------------------------------------------------------
# Recipe
# --------------------------------------------------------------------------------------------
KEYS = {"sar_mode", "stac_root", "output_dir", "production_date", "version", "start", "end",
        "products", "satellites", "sar_parquet_root", "archives"}
REQUIRED = {"sar_mode", "stac_root", "output_dir", "production_date", "version", "start", "end",
            "products"}


def _check(condition, message):
    if not condition:
        raise ValueError(message)


def read_iw_recipe(path) -> dict:
    """Load and validate an IW coalign recipe without touching the data."""
    path = Path(path).resolve()
    with path.open("rb") as stream:
        recipe = tomllib.load(stream)
    unknown, missing = recipe.keys() - KEYS, REQUIRED - recipe.keys()
    _check(not unknown and not missing,
           f"unknown keys {sorted(unknown)}; missing keys {sorted(missing)}")
    _check(recipe["sar_mode"] == SAR_MODE, "sar_mode must be IW here")
    _check(re.fullmatch(r"\d+\.\d+", str(recipe["version"])), "version must look like X.Y")
    _check(re.fullmatch(r"\d{8}", str(recipe["production_date"])),
           "production_date must be YYYYMMDD")
    for key in ("start", "end"):
        _check(DAY.fullmatch(str(recipe[key])), f"{key} must be YYYY-MM-DD")
    _check(recipe["start"] <= recipe["end"], "start is after end")
    products = recipe["products"]
    _check(isinstance(products, list) and products and all(isinstance(p, str) for p in products),
           "products must be a nonempty list of collection folders (e.g. 'swot/d2')")
    satellites = recipe.get("satellites")
    _check(satellites is None or all(re.fullmatch(r"S1[A-D]", str(s)) for s in satellites),
           "satellites must be a list of S1A..S1D")
    archives = recipe.get("archives", {})
    _check(isinstance(archives, dict) and set(archives) <= {"ocn", "l1"},
           "archives takes 'ocn' (provider L2 tree) and 'l1' (list of MPC-layout roots)")
    resolve = lambda value: (path.parent / value).resolve()  # noqa: E731
    recipe["archives"] = Archives(
        resolve(archives["ocn"]) if archives.get("ocn") else None,
        [resolve(p) for p in archives.get("l1", [])])
    for key in ("stac_root", "output_dir", "sar_parquet_root"):
        if recipe.get(key):
            recipe[key] = resolve(recipe[key])
    recipe["satellites"] = satellites
    recipe["recipe_path"] = path
    return recipe


def run_iw_recipe(path) -> tuple:
    recipe = read_iw_recipe(path)
    parquet = recipe.get("sar_parquet_root")
    if parquet is not None and not parquet.is_dir():
        raise FileNotFoundError(f"sar_parquet_root {parquet} is not a folder")
    meta = {"stac_root": str(recipe["stac_root"]),
            "stac_days": f"{recipe['start']}..{recipe['end']}",
            "recipe_path": str(recipe["recipe_path"])}
    if parquet is not None:
        meta["sar_parquet_root"] = str(parquet)
    return build_iw_catalogues(
        [recipe["stac_root"] / p for p in recipe["products"]], recipe["start"], recipe["end"],
        recipe["output_dir"], recipe["version"], recipe["production_date"],
        recipe["satellites"], cdsodatacli_matcher(parquet) if parquet else None,
        recipe["archives"], meta)


def is_iw_recipe(path) -> bool:
    with Path(path).open("rb") as stream:
        return tomllib.load(stream).get("sar_mode") == SAR_MODE
