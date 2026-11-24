"""Create TARGET dataset Parquets from published reference TEST dataset Parquets.

A TARGET dataset is the public, reference-free counterpart of a reference TEST
dataset. It is a strict column projection of the TEST Parquet: the same rows in
the same order, keyed by the same ``primary_key``, but WITHOUT the reference
parameter value (the quantity a challenge participant must predict). See the
SOBA parquet format specification, "Target dataset" section (WV §3.5, naming §1.5).

The projection keeps only the SAR identifiers and the reference location/time::

    primary_key, sar-safe-slc, sar-safe-ocn, ref_lon, ref_lat, ref_time

Because the row order and the ``primary_key`` sequence are preserved, a
challenger prediction file (``primary_key`` + predicted value) can be joined
straight back onto the TEST dataset for scoring.

The SAFE-name columns follow the normalized (hyphen) spelling of the published
TEST export; an underscored spelling is accepted for forward compatibility.

Usage:
    soba_create_target_dataset --test-dir <dir> --out-dir <dir> [--only PATTERN]
"""

from __future__ import annotations

import argparse
import fnmatch
import re
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from .pdf_support import library_version

# The six TARGET columns, in output order (the SAFE columns are re-resolved to
# whichever spelling the TEST Parquet uses, see :func:`_resolved_target_columns`).
TARGET_COLUMNS = ["primary_key", "sar-safe-slc", "sar-safe-ocn", "ref_lon", "ref_lat", "ref_time"]
_SAFE_ALIASES = {
    "sar-safe-slc": ("sar-safe-slc", "sar_safe_slc"),
    "sar-safe-ocn": ("sar-safe-ocn", "sar_safe_ocn"),
}
_TEST_STEM = re.compile(r"S1_reference_test_dataset_(\w+?)_(\d{8})_(.+?)_([\d.]+)\.parquet$")
_SOURCE_KEYS = (
    "source swot",
    "source scat",
    "source alti",
    "source ancillary datasets",
    "soba_format_specification",
    "soba_format_specification_version",
    "soba_split_export_version",
    "soba_task",
    "soba_primary_key_policy",
)


def _resolved_target_columns(names) -> list[str]:
    """Return the six TARGET columns using the TEST Parquet's SAFE-name spelling."""
    names = set(names)
    resolved = []
    for column in TARGET_COLUMNS:
        if column in _SAFE_ALIASES:
            alias = next((a for a in _SAFE_ALIASES[column] if a in names), column)
            resolved.append(alias)
        else:
            resolved.append(column)
    return resolved


def target_name(test_name: str) -> str:
    """Derive the TARGET filename from a reference TEST filename (spec §1.5)."""
    match = _TEST_STEM.match(Path(test_name).name)
    if match is None:
        raise ValueError(
            "not a reference TEST filename (expected "
            "S1_reference_test_dataset_<mode>_<YYYYMMDD>_<variable>_<version>.parquet): "
            f"{test_name}"
        )
    mode, date, variable, version = match.groups()
    return f"S1_target_dataset_{mode}_{date}_{variable}_{version}.parquet"


def build_target_from_test(test_path: Path) -> pa.Table:
    """Project one TEST Parquet to its TARGET (six columns, order preserved)."""
    table = pq.read_table(Path(test_path))
    columns = _resolved_target_columns(table.column_names)
    missing = [column for column in columns if column not in table.column_names]
    if missing:
        raise ValueError(f"{test_path.name}: TEST Parquet is missing target columns: {missing}")
    target = table.select(columns)
    validate_target_table(target, columns)
    return target


def validate_target_table(target: pa.Table, columns: list[str] | None = None) -> None:
    """Check the six-column TARGET contract on a freshly projected table."""
    columns = columns or list(target.column_names)
    if list(target.column_names) != columns:
        raise ValueError(f"TARGET columns must be {columns}, got {list(target.column_names)}")
    if target.num_rows == 0:
        raise ValueError("TARGET Parquet must not be empty")

    keys = target.column("primary_key")
    if not (pa.types.is_string(keys.type) or pa.types.is_large_string(keys.type)):
        raise ValueError(f"TARGET primary_key must be a string, got {keys.type}")
    key_values = keys.to_pylist()
    if any(key is None or not str(key).strip() for key in key_values):
        raise ValueError("TARGET primary_key values must be non-null and nonempty")
    if len(set(key_values)) != len(key_values):
        raise ValueError("TARGET primary_key values must be unique")

    for name in columns[1:3]:
        column = target.column(name)
        if not (pa.types.is_string(column.type) or pa.types.is_large_string(column.type)):
            raise ValueError(f"TARGET {name} must be a string, got {column.type}")
        if column.null_count:
            raise ValueError(f"TARGET {name} values must be non-null")

    for name in ("ref_lon", "ref_lat"):
        column = target.column(name)
        if not pa.types.is_floating(column.type):
            raise ValueError(f"TARGET {name} must be a float, got {column.type}")
        if column.null_count:
            raise ValueError(f"TARGET {name} values must be non-null")

    ref_time = target.column("ref_time")
    if not pa.types.is_timestamp(ref_time.type):
        raise ValueError(f"TARGET ref_time must be a timestamp, got {ref_time.type}")
    if ref_time.null_count:
        raise ValueError("TARGET ref_time values must be non-null")


def _target_metadata(test_path: Path, production_date: str) -> dict[bytes, bytes]:
    """Target global attributes: the five mandatory plus provenance of the TEST."""
    source = pq.read_schema(test_path).metadata or {}
    text = {
        (key.decode() if isinstance(key, bytes) else str(key)): value.decode()
        for key, value in source.items()
    }
    meta: dict[str, str] = {}
    for name in _SOURCE_KEYS:
        if name in text:
            meta[name] = text[name]
    meta.update(
        {
            "library used to produce the parquet": "soba_reference_repo",
            "library version": library_version(),
            "creation date": production_date,
            "soba_split": "target",
            "source test dataset": test_path.name,
            "soba_disclaimer": (
                "Public challenge input: the S1 reference TEST dataset reduced to its SAR "
                "identifiers and reference location/time. The reference parameter value is "
                "withheld; the row order and primary_key sequence match the TEST Parquet exactly."
            ),
        }
    )
    return {name.encode(): value.encode() for name, value in meta.items()}


def write_target(test_path: Path, out_dir: Path, production_date: str | None = None) -> Path:
    """Write the TARGET Parquet for one TEST Parquet, validate it against the TEST.

    ``production_date`` defaults to the date parsed from the TEST filename so the
    TARGET is the exact pair of its TEST (same date and version).
    """
    test_path = Path(test_path)
    name_match = _TEST_STEM.match(test_path.name)
    if name_match is None:
        raise ValueError(f"not a reference TEST filename: {test_path.name}")
    if production_date is None:
        production_date = name_match.group(2)
    if not re.fullmatch(r"\d{8}", production_date):
        raise ValueError("production_date must use YYYYMMDD")

    target = build_target_from_test(test_path)
    test = pq.read_table(test_path, columns=["primary_key"])
    if test.num_rows != target.num_rows:
        raise ValueError("TARGET row count differs from the TEST Parquet")
    if test.column("primary_key").to_pylist() != target.column("primary_key").to_pylist():
        raise ValueError("TARGET primary_key sequence differs from the TEST Parquet")

    metadata = _target_metadata(test_path, production_date)
    table = target.replace_schema_metadata(metadata)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / target_name(test_path.name)
    pq.write_table(table, path, compression="zstd")

    validate_target_file(path, test_path)
    return path


def validate_target_file(target_path: Path, test_path: Path | None = None) -> bool:
    """Validate one TARGET Parquet, and its pairing with the TEST when supplied."""
    target_path = Path(target_path)
    schema = pq.read_schema(target_path)
    table = pq.read_table(target_path)
    columns = _resolved_target_columns(schema.names)
    validate_target_table(table, columns)
    if test_path is not None:
        test = pq.read_table(Path(test_path), columns=["primary_key"])
        if test.num_rows != table.num_rows:
            raise ValueError("TARGET row count differs from the TEST Parquet")
        if test.column("primary_key").to_pylist() != table.column("primary_key").to_pylist():
            raise ValueError("TARGET primary_key sequence differs from the TEST Parquet")
    return True


def main(argv=None) -> int:
    """Create the TARGET Parquet for every reference TEST Parquet in a directory."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--test-dir", type=Path, required=True,
                        help="directory holding the reference TEST Parquets")
    parser.add_argument("--out-dir", type=Path, required=True,
                        help="directory to write the TARGET Parquets into")
    parser.add_argument("--only", default="*",
                        help="glob pattern matching the TEST Parquet basenames")
    args = parser.parse_args(argv)

    test_dir = Path(args.test_dir)
    pattern = args.only
    paths = sorted(str(path) for path in test_dir.glob("*.parquet")
                   if fnmatch.fnmatch(path.name, pattern))
    if not paths:
        raise SystemExit(f"no TEST Parquet matching {pattern!r} in {test_dir}")

    count = 0
    for test_path in paths:
        out = write_target(Path(test_path), args.out_dir)
        count += 1
        print(f"TARGET: {out}  ({out.stat().st_size / 1e6:.2f} MB)")
    print(f"created {count} TARGET Parquet(s) in {args.out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
