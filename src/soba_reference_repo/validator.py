"""Read-only validation of individual SWOT Parquets."""

from __future__ import annotations

import argparse
import math
import re
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from .swot_test import SOURCE_COLUMNS, TARGET_COLUMNS, TEST_COLUMNS, validate_swot_file

FILE_TYPES = ("catalogue", "curated", "test", "target", "challenger")


def validate_file(path: Path, file_type: str, reference: str = "swot") -> bool:
    """Validate one file; this cannot establish alignment with another product."""
    if reference != "swot":
        raise ValueError(f"unsupported reference: {reference}; only swot is available")
    path = Path(path)
    if not path.is_file():
        raise OSError(f"not a readable Parquet file: {path}")
    if file_type in ("test", "target"):
        return validate_swot_file(path, file_type)
    if file_type in ("catalogue", "curated"):
        return _validate_native(path, curated=file_type == "curated")
    if file_type == "challenger":
        table = pq.read_table(path)
        if table.column_names != ["primary_key", "swh"]:
            raise ValueError("CHALLENGER schema must be primary_key, swh")
        if table.schema.field("primary_key").type != pa.string():
            raise ValueError("CHALLENGER primary_key type must be string")
        prediction = table.schema.field("swh")
        if prediction.type != pa.float64() or not prediction.nullable:
            raise ValueError("CHALLENGER swh must be nullable float64")
        _validate_keys(table.column("primary_key").to_pylist())
        for value in table.column("swh").to_pylist():
            if value is not None and not math.isfinite(value):
                raise ValueError("CHALLENGER non-null swh values must be finite")
        return True
    raise ValueError(f"unsupported file type: {file_type}")


def _validate_keys(keys):
    if any(not isinstance(key, str) or not key.strip() for key in keys):
        raise ValueError("primary_key values must be non-null and nonempty")
    if len(keys) != len(set(keys)):
        raise ValueError("primary_key values must be unique")


def _validate_native(path, curated=False):
    """Check source-native output fields, not recipe-specific quality selections.

    The Curated writer retains source metadata and adds only row provenance.
    Missing measurements are allowed here: the TEST producer excludes them.
    """
    table = pq.read_table(path)
    required = list(SOURCE_COLUMNS[:13])
    if curated:
        required += ["_curation_mission", "_curation_row"]
    missing = set(required) - set(table.column_names)
    if missing:
        raise ValueError(f"missing source columns: {sorted(missing)}")
    strings = {"sar_safe_slc", "sar_safe_ocn", "swot_path"}
    for name in SOURCE_COLUMNS[:13]:
        dtype = table.schema.field(name).type
        if name in strings:
            valid = dtype in (pa.string(), pa.large_string())
        elif name in ("sar_time", "ref_time"):
            valid = pa.types.is_timestamp(dtype) or dtype in (pa.string(), pa.large_string())
            if valid:
                values = table.column(name).to_pandas()
                parsed = pd.to_datetime(values, utc=True, errors="coerce")
                if (values.notna() & parsed.isna()).any():
                    raise ValueError(f"invalid source time values: {name}")
        else:
            valid = pa.types.is_floating(dtype) or pa.types.is_integer(dtype)
        if not valid:
            raise ValueError(f"invalid source column type: {name}: {dtype}")
    if "primary_key" in table.column_names:
        if table.schema.field("primary_key").type not in (pa.string(), pa.large_string()):
            raise ValueError("primary_key type must be string")
        _validate_keys(table.column("primary_key").to_pylist())
    if curated:
        if table.schema.field("_curation_mission").type != pa.string():
            raise ValueError("_curation_mission type must be string")
        if table.schema.field("_curation_row").type != pa.int64():
            raise ValueError("_curation_row type must be int64")
        provenance = list(zip(table.column("_curation_mission").to_pylist(),
                              table.column("_curation_row").to_pylist()))
        if any(not isinstance(mission, str) or not re.fullmatch(r"S1[A-D]", mission)
               or row is None or row < 0 for mission, row in provenance):
            raise ValueError("invalid curation mission or row ordinal")
        if len(provenance) != len(set(provenance)):
            raise ValueError("curation mission/row values must be unique")
    return True


def _print_file_report(path: Path, file_type: str) -> None:
    """Summarize checks only after the complete file validation succeeds."""
    parquet = pq.ParquetFile(path)
    print(f"\n{file_type.upper()}: {path}")
    print(f"  Rows: {parquet.metadata.num_rows:,} | Columns: {len(parquet.schema_arrow)}")
    if file_type in ("test", "target"):
        columns = TEST_COLUMNS if file_type == "test" else TARGET_COLUMNS
        strings = {"primary_key", "sar_safe_slc", "sar_safe_ocn", "swot_source"}
        types = {
            name: "string|large_string" if name in strings
            else "timestamp[ns]" if name.endswith("time") else "float32"
            for name in columns
        }
        policy = "exact columns and order"
    elif file_type == "challenger":
        types = {"primary_key": "string", "swh": "float64 (nullable)"}
        policy = "exact columns and order"
    else:
        strings = {"sar_safe_slc", "sar_safe_ocn", "swot_path"}
        types = {
            name: "string|large_string" if name in strings
            else "timestamp|string|large_string" if name in ("sar_time", "ref_time")
            else "integer|floating"
            for name in SOURCE_COLUMNS[:13]
        }
        if file_type == "curated":
            types.update({"_curation_mission": "string", "_curation_row": "int64"})
        if "primary_key" in parquet.schema_arrow.names:
            types["primary_key"] = "string|large_string"
        policy = "required columns; extras allowed; order unrestricted"
    schema = ", ".join(f"{name}: {dtype}" for name, dtype in types.items())
    print(f"  Schema checked: [{schema}] ({policy})")
    checks = ["Parquet readable"]
    if file_type in ("test", "target"):
        checks += [
            "Exact column names and order",
            "Field types (strings, float32 values, nanosecond timestamps)",
            "Required five metadata attributes",
            "Required values non-null",
            "Non-null numeric values finite",
            "Primary keys non-null, nonempty and unique",
            "Primary key components non-null and SAFE name nonempty",
            "Primary key composition (SAFE + SWOT coordinates at 0.1 degree)",
        ]
    elif file_type == "challenger":
        checks += [
            "Exact column names and order (primary_key, swh)",
            "Field types (string keys, nullable float64 predictions)",
            "Primary keys non-null, nonempty and unique",
            "Non-null predictions finite",
        ]
    else:
        checks += [
            "Required source columns present (additional columns allowed)",
            "Field types of required source columns",
            "Source time values parseable (non-null values)",
        ]
        if "primary_key" in parquet.schema_arrow.names:
            checks += ["Primary key string type", "Primary keys non-null, nonempty and unique"]
        if file_type == "curated":
            checks.append(
                "Curation mission/row provenance: types, valid missions, ordinals, uniqueness"
            )
    for check in checks:
        print(f"  [PASS] {check}")
    if file_type in ("catalogue", "curated"):
        if "primary_key" not in parquet.schema_arrow.names:
            print("  [SKIP] Primary keys: column absent")
        print("  Note: Source metadata and recipe quality filters: not checked")
        print("  Note: Missing source measurements are allowed")
    elif file_type == "challenger":
        print("  Note: Missing predictions are allowed; prediction coverage is not checked")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--type", choices=FILE_TYPES, dest="file_type", required=True,
                        help="role of the Parquet supplied with --file")
    parser.add_argument("--file", type=Path, required=True,
                        help="single Parquet to validate (read-only)")
    parser.add_argument("--reference", default="swot", choices=("swot",),
                        help="reference contract (default: swot; only SWOT is implemented)")
    args = parser.parse_args(argv)
    try:
        validate_file(args.file, args.file_type, args.reference)
        _print_file_report(args.file, args.file_type)
        print("  Note: Cross-file alignment: not checked (single-file mode)")
    except (ValueError, OSError) as error:
        parser.exit(1, f"validation failed: {error}\n")
    print(f"{args.file_type.upper()} validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
