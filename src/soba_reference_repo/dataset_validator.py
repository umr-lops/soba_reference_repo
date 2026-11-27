"""Read-only validation of SOBA coaligned catalogue and curated Parquets.

Checks against the SOBA parquet format specification (``spec``): file naming convention and
reference product list, readability, the mandatory columns of the mode (WV or IW), SAR dates
against the filename, SAR positions, SAFE identities, primary keys and their precision, the global
attributes; plus SOBA production checks outside the spec (curation provenance, version metadata).
Each check is reported as PASS, WARN or FAIL; a file is valid when no check FAILs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from . import spec

DATASET_TYPES = ("catalogue", "curated")

# <mission>_<kind>_<mode>_<start>_<stop>_<production>_<pol>_<product>_<X.Y>.parquet
# Dates are YYYYMMDD (spec); YYYYMMDDTHHMMSS (older WV x altimeter catalogues) is refused.
_DATE = r"\d{8}"
TIMESTAMP_NAME_RE = re.compile(r"_\d{8}T\d{6}_")
NAME_RE = re.compile(
    rf"(?P<mission>S1[A-D])_(?P<kind>coaligned_catalogue|curated)_(?P<mode>WV|IW|EW|SM)_"
    rf"(?P<start>{_DATE})_(?P<stop>{_DATE})_(?P<production>{_DATE})_"
    r"(?P<pol>SV|DV|SH|DH)_(?P<product>[A-Za-z0-9][A-Za-z0-9._-]*?)_(?P<version>\d+\.\d+)"
    r"\.parquet"
)
KIND = {"catalogue": "coaligned_catalogue", "curated": "curated"}

# Optional: checked only when present. Spec 1.5.0 dropped them from every dataset; files produced
# before it may still carry them.
POSITION_COLUMNS = spec.OPTIONAL_POSITION_COLUMNS
# Written by soba_reference_repo curation (not in the spec): checked when present.
CURATION_COLUMNS = ("_curation_mission", "_curation_row")
MISSING_SAFE = ("", "UNKNOWN")
# primary_key ends with the reference coordinates: ..._<lon>_<lat>
KEY_COORDS_RE = r"_(-?\d+(?:\.(\d*))?)_(-?\d+(?:\.(\d*))?)$"
MIN_KEY_DECIMALS = spec.MIN_KEY_DECIMALS


@dataclass
class Report:
    """Outcome of the checks run on one file."""

    path: Path
    file_type: str
    rows: int = 0
    columns: int = 0
    checks: list[tuple[str, str, str]] = field(default_factory=list)

    def add(self, status: str, name: str, detail: str = "") -> None:
        self.checks.append((status, name, detail))

    @property
    def ok(self) -> bool:
        return all(status != "FAIL" for status, _, _ in self.checks)


def _parse_date(token: str) -> date:
    return datetime.strptime(token[:8], "%Y%m%d").date()


def _strings(table: pa.Table, name: str) -> pd.Series:
    return table.column(name).to_pandas().astype("string")


def _check_name(report: Report) -> dict | None:
    if TIMESTAMP_NAME_RE.search(report.path.name):
        report.add("FAIL", "Filename convention",
                   "dates must be YYYYMMDD (spec), found YYYYMMDDTHHMMSS")
        return None
    match = NAME_RE.fullmatch(report.path.name)
    if match is None:
        report.add("FAIL", "Filename convention",
                   "expected <S1X>_<coaligned_catalogue|curated>_<mode>_<start>_<stop>_"
                   "<production>_<pol>_<product>_<X.Y>.parquet")
        return None
    parts = match.groupdict()
    if parts["kind"] != KIND[report.file_type]:
        report.add("FAIL", "Filename convention",
                   f"'{parts['kind']}' file checked as --type {report.file_type}")
        return None
    try:
        start, stop, production = (
            _parse_date(parts[key]) for key in ("start", "stop", "production")
        )
    except ValueError as error:
        report.add("FAIL", "Filename convention", f"invalid date: {error}")
        return None
    if start > stop:
        report.add("FAIL", "Filename convention", f"start {start} after stop {stop}")
        return None
    if production < start:
        report.add("FAIL", "Filename convention",
                   f"production date {production} before start {start}")
        return None
    report.add("PASS", "Filename convention",
               f"{parts['mission']} {parts['mode']} {parts['pol']} {parts['product']} "
               f"v{parts['version']}")
    if spec.is_spec_product(parts["product"]):
        report.add("PASS", "Reference product in the spec list", parts["product"])
    else:
        report.add("FAIL", "Reference product in the spec list",
                   f"{parts['product']!r} is not a spec {spec.SPEC_VERSION} <refproductname>")
    return {**parts, "start_d": start, "stop_d": stop, "production_d": production}


def _mode(table: pa.Table, name: dict | None) -> str:
    """The SAR mode: from the filename, else IW when the GRD SAFE column is there, else WV."""
    if name is not None:
        return name["mode"]
    return "IW" if "sar_safe_grd" in table.column_names else "WV"


def _check_columns(report: Report, table: pa.Table, mode: str) -> bool:
    required = list(spec.CATALOGUE_COLUMNS[mode])
    missing = [name for name in required if name not in table.column_names]
    if missing:
        report.add("FAIL", "Required columns",
                   f"missing spec {spec.SPEC_VERSION} {mode} columns {missing}")
        return False
    dtype = table.schema.field("sar_time").type
    if not (pa.types.is_timestamp(dtype) or pa.types.is_string(dtype)
            or pa.types.is_large_string(dtype)):
        report.add("FAIL", "Required columns", f"sar_time type {dtype}")
        return False
    report.add("PASS", "Required columns",
               f"{len(required)} spec {spec.SPEC_VERSION} {mode} columns present "
               "(ref_param columns are named per dataset: not checked)")
    return True


def _check_sar_dates(report: Report, table: pa.Table, name: dict | None) -> None:
    times = pd.to_datetime(table.column("sar_time").to_pandas(), utc=True, errors="coerce")
    missing = int(times.isna().sum())
    if missing:
        report.add("FAIL", "SAR times", f"{missing:,} null or unparsable sar_time")
    if times.notna().sum() == 0:
        return
    first, last = times.min().date(), times.max().date()
    if not missing:
        report.add("PASS", "SAR times", f"{first} -> {last}")
    if name is None:
        return
    start, stop = name["start_d"], name["stop_d"]
    if report.file_type == "curated":
        # SOBA convention: curated dates are the first/last SAR dates kept.
        if (first, last) != (start, stop):
            report.add("FAIL", "Filename dates match the data",
                       f"name {start} -> {stop}, data {first} -> {last}")
        else:
            report.add("PASS", "Filename dates match the data")
    elif first < start or last > stop:
        report.add("FAIL", "Data within the filename dates",
                   f"name {start} -> {stop}, data {first} -> {last}")
    elif (first, last) != (start, stop):
        report.add("WARN", "Data within the filename dates",
                   f"name {start} -> {stop} wider than data {first} -> {last}")
    else:
        report.add("PASS", "Data within the filename dates")


def _check_positions(report: Report, table: pa.Table) -> None:
    absent = [name for name in POSITION_COLUMNS if name not in table.column_names]
    if absent:
        report.add("PASS", "SAR positions", f"not checked: optional {absent} absent")
        return
    lat = pd.to_numeric(table.column("sar_lat").to_pandas(), errors="coerce")
    lon = pd.to_numeric(table.column("sar_lon").to_pandas(), errors="coerce")
    bad = int((~(lat.abs().le(90) & lon.ge(-180) & lon.le(180))).sum())
    if bad:
        # Catalogues keep the source rows (e.g. netCDF fill value 9.97e36);
        # curation is expected to remove them.
        level = "FAIL" if report.file_type == "curated" else "WARN"
        report.add(level, "SAR positions",
                   f"{bad:,} rows with sar_lat outside [-90, 90] or sar_lon outside [-180, 180]")
    else:
        report.add("PASS", "SAR positions")


def _check_safes(report: Report, table: pa.Table, name: dict | None, mode: str) -> None:
    unknown = 0
    for column in ("sar_safe_slc", "sar_safe_ocn"):
        values = _strings(table, column)
        unknown += int((values.isna() | values.str.strip().isin(MISSING_SAFE)).sum())
    if unknown:
        report.add("WARN", "SAFE identities", f"{unknown:,} missing or UNKNOWN SLC/OCN SAFE")
    else:
        report.add("PASS", "SAFE identities")
    if name is None:
        return
    # The SAFE the primary key is built on: GRD in IW (and EW, SM), SLC in WV.
    column = "sar_safe_slc" if mode == "WV" else "sar_safe_grd"
    safes = _strings(table, column).dropna()
    units = safes.str.extract(r"(S1[A-D])_[A-Z]{2}_", expand=False).dropna()
    other = int((units != name["mission"]).sum())
    if other:
        report.add("FAIL", "SAFE unit matches the filename",
                   f"{other:,} {column} not from {name['mission']}")
    else:
        report.add("PASS", "SAFE unit matches the filename")


def _check_keys(report: Report, table: pa.Table) -> None:
    keys = _strings(table, "primary_key")
    empty = int((keys.isna() | (keys.str.strip() == "")).sum())
    duplicated = int(keys.dropna().duplicated().sum())
    if empty or duplicated:
        report.add("FAIL", "Primary keys",
                   f"{empty:,} null/empty, {duplicated:,} duplicated")
    else:
        report.add("PASS", "Primary keys", "non-null and unique")


def _check_key_precision(report: Report, table: pa.Table) -> None:
    """Reference lon/lat in primary_key need >= 2 decimals (spec), for every reference.

    One WV imagette can match several reference points (several SCAT cells,
    two SCAT passes, products at 7.5/12.5/25 km): with 1 decimal (~11 km)
    distinct points collide on the same key.
    """
    coords = _strings(table, "primary_key").str.extract(KEY_COORDS_RE)
    found = coords[0].notna()
    if not found.any():
        report.add("WARN", "Primary key coordinate precision",
                   "no trailing _<lon>_<lat> in primary_key")
        return
    decimals = pd.concat([coords[1].fillna("").str.len(), coords[3].fillna("").str.len()],
                         axis=1).min(axis=1)[found]
    coarse = int((decimals < MIN_KEY_DECIMALS).sum())
    if not coarse:
        report.add("PASS", "Primary key coordinate precision",
                   f">= {MIN_KEY_DECIMALS} decimals on lon/lat")
        return
    report.add("FAIL", "Primary key coordinate precision",
               f"{coarse:,} keys with < {MIN_KEY_DECIMALS} decimals on lon/lat")


def _check_provenance(report: Report, table: pa.Table, name: dict | None) -> None:
    absent = [column for column in CURATION_COLUMNS if column not in table.column_names]
    if absent:
        report.add("WARN", "Curation provenance",
                   f"not checked: {absent} absent (SOBA curation columns, not in the spec)")
        return
    missions = _strings(table, "_curation_mission")
    rows = pd.to_numeric(table.column("_curation_row").to_pandas(), errors="coerce")
    invalid = int((missions.isna() | ~missions.fillna("").str.fullmatch(r"S1[A-D]")
                   | rows.isna() | rows.lt(0)).sum())
    duplicated = int(pd.DataFrame({"m": missions, "r": rows}).duplicated().sum())
    other = 0 if name is None else int((missions != name["mission"]).sum())
    if invalid or duplicated or other:
        report.add("FAIL", "Curation provenance",
                   f"{invalid:,} invalid, {duplicated:,} duplicated, "
                   f"{other:,} not from the filename unit")
    else:
        report.add("PASS", "Curation provenance")


def _check_global_attributes(report: Report, schema: pa.Schema) -> None:
    """The global attributes the spec makes mandatory, plus the curated source catalogue."""
    meta = {key.decode(errors="replace"): value.decode(errors="replace")
            for key, value in (schema.metadata or {}).items()}
    required = list(spec.GLOBAL_ATTRIBUTES)
    if report.file_type == "curated":
        required.append(spec.CURATED_ATTRIBUTE)
    missing = [key for key in required if not meta.get(key, "").strip()]
    sources = [key for key in meta if spec.SOURCE_ATTRIBUTE_RE.fullmatch(key)]
    if not sources:
        missing.insert(0, "source <ref>")
    if missing:
        report.add("FAIL", "Global attributes",
                   f"missing or empty spec {spec.SPEC_VERSION} attributes {missing}")
    else:
        report.add("PASS", "Global attributes", ", ".join([*sources, *required]))


def _check_metadata(report: Report, schema: pa.Schema, name: dict | None) -> None:
    """dataset_version / production_date against the filename (SOBA curation, not in the spec)."""
    meta = {key.decode(errors="replace"): value.decode(errors="replace")
            for key, value in (schema.metadata or {}).items()}
    if report.file_type != "curated" or name is None:
        return
    expected = {"dataset_version": name["version"], "production_date": name["production"][:8]}
    present = {key: meta[key] for key in expected if key in meta}
    if not present:
        report.add("WARN", "Version metadata",
                   "no dataset_version / production_date (curated before v4.0)")
        return
    wrong = {key: value for key, value in present.items() if value != expected[key]}
    if wrong:
        report.add("FAIL", "Version metadata", f"{wrong} != filename {expected}")
    else:
        report.add("PASS", "Version metadata", ", ".join(f"{k}={v}" for k, v in present.items()))


def validate_dataset(path: Path, file_type: str) -> Report:
    """Run every catalogue/curated check on one Parquet and return the report."""
    if file_type not in DATASET_TYPES:
        raise ValueError(f"unsupported file type: {file_type}")
    path = Path(path)
    if not path.is_file():
        raise OSError(f"not a readable Parquet file: {path}")
    report = Report(path, file_type)
    name = _check_name(report)
    try:
        table = pq.read_table(path)
    except (OSError, pa.ArrowException) as error:
        report.add("FAIL", "Parquet readable", str(error))
        return report
    report.rows, report.columns = table.num_rows, table.num_columns
    if table.num_rows == 0:
        report.add("FAIL", "Parquet readable", "no rows")
        return report
    report.add("PASS", "Parquet readable")
    _check_global_attributes(report, table.schema)
    mode = _mode(table, name)
    if not _check_columns(report, table, mode):
        return report
    _check_sar_dates(report, table, name)
    _check_positions(report, table)
    _check_safes(report, table, name, mode)
    _check_keys(report, table)
    _check_key_precision(report, table)
    if file_type == "curated":
        _check_provenance(report, table, name)
    _check_metadata(report, table.schema, name)
    return report


def print_report(report: Report) -> None:
    print(f"\n{report.file_type.upper()}: {report.path}")
    print(f"  Rows: {report.rows:,} | Columns: {report.columns}")
    for status, check, detail in report.checks:
        print(f"  [{status}] {check}" + (f": {detail}" if detail else ""))
