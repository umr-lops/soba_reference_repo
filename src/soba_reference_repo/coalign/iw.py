"""IW specifics of the co-aligned catalogues (spec 1.5.0, IW section).

A row is one reference point matched with one IW **GRD** scene (``primary_key`` = GRD SAFE name +
ref_lon + ref_lat). The GRD matchups are read from ``<stac_root>/<collection>/<YYYY-MM-DD>/grd/iw``
(``collection`` e.g. ``swot/d2``). The OCN and SLC twins of each GRD (same acquisition, slice
times differing by a second or so) are found in the CDSE daily metadata Parquets with cdsodatacli
(``match_s1_prodtypes`` parquet backend: same platform, orbit, datatake and polarisation,
closest start time within 8 s); without a matcher their columns stay empty.

The datasets need SAR metadata only: a ``sar_path_*`` column is empty (null) when the SAFE is not
on the configured archives. The spec ancillary columns without a reader (or not asked for in the
recipe) are written empty and named in the ``ancillary columns not filled`` global attribute, so
a catalogue can be built and checked before every source is ready. The writer, file naming and
ancillary readers are shared (``catalogue.py``, ``ancillary``).
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pyarrow as pa

from .catalogue import Mode
from .rows import KEY_DECIMALS, as_flag, as_float, stac_time
from .sar import PLATFORM
from .stac import DAY

SAR_MODE = "IW"
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
# Spec ancillary columns; written empty when no source fills them.
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
MANDATORY = ("primary_key", "sar_time", "sar_safe_grd", "ref_lon", "ref_lat", "ref_time",
             "ref_id")


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


def columns(param_names, extras=(), ancillary=()) -> list:
    """All IW catalogue columns; ``ancillary`` are the source columns beyond the spec ones."""
    return [*HEAD, *param_names, *TAIL, *ANCILLARY,
            *[c for c in ancillary if c not in ANCILLARY], *EXTRA, *extras]


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
    }
    rows = []
    for group in props["sarmatchup:matchups"]:
        for p in group["points"]:
            lon, lat = p["ref_lon"], p["ref_lat"]
            row = {
                **base,
                "primary_key": f"{grd}_{lon:.{KEY_DECIMALS}f}_{lat:.{KEY_DECIMALS}f}",
                "sar_incidence_angle": as_float(p.get("sar_incidence_angle")),
                "sar_elevation_angle": as_float(p.get("sar_elevation_angle")),
                "sar_ground_heading": as_float(p.get("sar_ground_heading")),
                "sar_ground_heading_uncertainty": as_float(p.get("sar_ground_heading_uncertainty")),
                "sar_distance_to_along_track_edge": as_float(
                    p.get("sar_distance_to_along_track_edge")),
                "sar_distance_to_cross_track_edge": as_float(
                    p.get("sar_distance_to_cross_track_edge")),
                "ref_lon": float(lon), "ref_lat": float(lat),
                "ref_time": stac_time(p["ref_time_utc"]).replace(microsecond=0),
                "ref_time_delta_min": as_float(p.get("delta_diff_time_minutes")),
                "ref_id": group["reference_file"], "ref_source": group["reference_source"],
                "ref_distance_to_coast": as_float(p.get("sar_distance_to_coast")),
                "ref_flag": as_flag(p.get(reference.flag)) if reference.flag else None,
            }
            for column, field in reference.params:
                row[column] = as_float(p.get(field))
            for field in reference.extras:
                row[field] = (as_flag if field in INTEGERS else as_float)(p.get(field))
            rows.append(row)
    return rows, ""


def find_days(collection_dir, start: str, end: str) -> list:
    """GRD IW run folders ``<collection>/<YYYY-MM-DD>/grd/iw`` holding a manifest, in day order."""
    days = []
    root = Path(collection_dir)
    for day in sorted(root.iterdir()) if root.is_dir() else []:
        run = day / "grd" / "iw"
        if DAY.match(day.name) and start <= day.name <= end and (run / "manifest.json").is_file():
            days.append(run)
    return days


class IW(Mode):
    """IW GRD scenes, with their OCN and SLC twins on the same rows."""

    name = SAR_MODE
    # TenGeoP classifies WV imagettes only.
    sources = ("ecmwf", "era5", "ww3")

    def __init__(self, matcher=None, archives=None):
        self.matcher, self.archives = matcher, archives or Archives()
        self.extras = SWOT.extras
        self.tally = Counter()

    def schema(self, param_names, ancillary):
        def kind(name):
            if name in STRINGS:
                return pa.string()
            if name in TIMES:
                return pa.timestamp("us")
            return pa.int64() if name in INTEGERS else pa.float64()

        return pa.schema([(name, kind(name))
                          for name in columns(param_names, self.extras, ancillary)])

    def check(self, rows, param_names, ancillary):
        for row in rows:
            missing = [name for name in MANDATORY if row.get(name) is None]
            if missing:
                raise ValueError(f"empty mandatory columns {missing} in {row['primary_key']}")
        return {"rows": len(rows)}

    def attributes(self, sources):
        filled = {c for source in sources for c in source.columns}
        return {
            "ancillary columns not filled": ", ".join(c for c in ANCILLARY if c not in filled),
            "triplets": "OCN / SLC twins from the CDSE daily metadata Parquets (cdsodatacli)"
                        if self.matcher is not None else "not associated (no sar_parquet_root)",
        }

    def counts(self):
        return {**self.tally,
                **{f"paths_found_{k.lower()}": n for k, n in self.archives.found.items()}}

    def rows(self, product_dirs, start, end, counts, satellites=None):
        for collection in product_dirs:
            for run in find_days(collection, start, end):
                label = json.loads((run / "manifest.json").read_text())["reference_product"]
                reference, product = reference_for(label)
                names = [column for column, _ in reference.params]
                items = [json.loads(p.read_text()) for p in sorted((run / "items").glob("*.json"))]
                twins = {}
                if self.matcher is not None and items:
                    grds = [i["properties"]["sarmatchup:sar_safe"] for i in items]
                    ocn, slc = self.matcher(grds, "OCN_"), self.matcher(grds, "SLC_")
                    twins = {n: (ocn.get(n), slc.get(n)) for n in grds}
                self.tally["days"] += 1
                for item in items:
                    counts["items"] += 1
                    rows, skipped = item_rows(item, reference, twins, self.archives)
                    if skipped:
                        counts[f"skipped_{skipped}"] += 1
                        continue
                    if satellites and rows and rows[0]["_satellite"] not in satellites:
                        continue
                    ocn_name, slc_name = twins.get(item["properties"]["sarmatchup:sar_safe"],
                                                   (None, None))
                    self.tally["grd_without_ocn"] += ocn_name is None
                    self.tally["grd_without_slc"] += slc_name is None
                    for row in rows:
                        yield (row["_satellite"], row["_polarization"], product), names, row
