"""Read-only validation of individual SWOT and SCAT product Parquets."""

from __future__ import annotations

import argparse
import math
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from .swot_test import TARGET_COLUMNS, TEST_COLUMNS, validate_swot_file
from .hscat_test import output_columns, validate_scat_file

FILE_TYPES = ("test", "target", "challenger")


def validate_file(path: Path, file_type: str, reference: str = "swot") -> bool:
    """Validate one supported product file, not its alignment with another file."""
    if reference not in {"swot", "scat"}:
        raise ValueError(f"unsupported reference: {reference}; supported: swot, scat")
    if file_type not in FILE_TYPES:
        raise ValueError(f"unsupported file type: {file_type}")
    if reference == "scat" and file_type not in ("test", "target"):
        raise ValueError("SCAT validation supports TEST and TARGET files only")
    path = Path(path)
    if not path.is_file():
        raise OSError(f"not a readable Parquet file: {path}")
    if reference == "scat":
        return validate_scat_file(path, file_type)
    if file_type in ("test", "target"):
        return validate_swot_file(path, file_type)

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


def _validate_keys(keys):
    if any(not isinstance(key, str) or not key.strip() for key in keys):
        raise ValueError("primary_key values must be non-null and nonempty")
    if len(keys) != len(set(keys)):
        raise ValueError("primary_key values must be unique")


def _print_file_report(path: Path, file_type: str, reference="swot") -> None:
    """Summarize checks only after the complete file validation succeeds."""
    parquet = pq.ParquetFile(path)
    print(f"\n{file_type.upper()}: {path}")
    print(f"  Rows: {parquet.metadata.num_rows:,} | Columns: {len(parquet.schema_arrow)}")
    if file_type in ("test", "target"):
        if reference == "scat":
            schema_arrow = parquet.schema_arrow
            variable = "windspeed" if "scat_windspeed" in schema_arrow.names else "winddirection"
            columns = output_columns(file_type, variable)
            strings = {"primary_key", "sar_safe_slc", "sar_safe_ocn"}
            types = {
                name: "string|large_string" if name in strings
                else "timestamp[ns]" if name.endswith("time") else "float32"
                for name in columns
            }
        else:
            columns = TEST_COLUMNS if file_type == "test" else TARGET_COLUMNS
            strings = {"primary_key", "sar_safe_slc", "sar_safe_ocn", "swot_source"}
            types = {
                name: "string|large_string" if name in strings
                else "timestamp[ns]" if name.endswith("time") else "float32"
                for name in columns
            }
        policy = "exact columns and order"
    else:
        types = {"primary_key": "string", "swh": "float64 (nullable)"}
        policy = "exact columns and order"
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
        ]
        if reference == "scat":
            checks.append("Selected SCAT reference variable is present; key values preserved")
        else:
            checks += [
                "Primary key components non-null and SAFE name nonempty",
                "Primary key composition (SAFE + SWOT coordinates at 0.1 degree)",
            ]
    else:
        checks += [
            "Exact column names and order (primary_key, swh)",
            "Field types (string keys, nullable float64 predictions)",
            "Primary keys non-null, nonempty and unique",
            "Non-null predictions finite",
        ]
    for check in checks:
        print(f"  [PASS] {check}")
    if file_type == "challenger":
        print("  Note: Missing predictions are allowed; prediction coverage is not checked")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--type", choices=FILE_TYPES, dest="file_type", required=True,
                        help="role of the Parquet supplied with --file")
    parser.add_argument("--file", type=Path, required=True,
                        help="single Parquet to validate (read-only)")
    parser.add_argument(
        "--reference", default="swot", choices=("swot", "scat"),
        help="reference contract (default: swot; SCAT supports TEST/TARGET only)",
    )
    args = parser.parse_args(argv)
    try:
        validate_file(args.file, args.file_type, args.reference)
        _print_file_report(args.file, args.file_type, args.reference)
        print("  Note: Cross-file alignment: not checked (single-file mode)")
    except (ValueError, OSError) as error:
        parser.exit(1, f"validation failed: {error}\n")
    print(f"{args.file_type.upper()} validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
