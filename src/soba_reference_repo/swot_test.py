"""Create a merged SWOT WV TEST/TARGET pair and its report."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from matplotlib import pyplot as plt

from .report import (
    ASSET_DIR,
    DEFAULT_TEST_DIR,
    PACKAGE_ROOT,
    compile_pdf,
    library_version,
    purge_latex_byproducts,
    stage_latex_assets,
    _path,
)

MISSION_PATTERN = re.compile(r"^(S1[A-Z])_coaligned_catalogue_(WV)_")
EXPECTED_MISSIONS = ("S1A", "S1C", "S1D")
MISSION_COLORS = {
    "S1A": "#287D8E",
    "S1B": "#E69F00",
    "S1C": "#7B51A3",
    "S1D": "#C75731",
}
DEFAULT_SWOT_TEMPLATE = ASSET_DIR / "latex" / "swot_test_template.tex"
SWOT_COLUMN_DESCRIPTIONS = {
    "primary_key": "SLC SAFE plus reference longitude and latitude at one decimal degree.",
    "sar_time": "UTC SAR acquisition time, rounded down to whole seconds.",
    "sar_lat": "SAR WV imagette latitude in degrees.",
    "sar_lon": "SAR WV imagette longitude in degrees.",
    "sar_incidence_angle": "SAR incidence angle in degrees.",
    "sar_elevation_angle": "SAR elevation angle in degrees.",
    "sar_ground_heading": "SAR ground heading in degrees; unavailable in the source and null.",
    "sar_distance_to_coast": "Distance from the SAR WV imagette to the coast in kilometres.",
    "sar_safe_slc": "SLC SAFE identifier, including the WV imagette suffix.",
    "sar_safe_ocn": "OCN SAFE identifier, including the WV imagette suffix.",
    "swot_lon": "SWOT reference longitude in degrees.",
    "swot_lat": "SWOT reference latitude in degrees.",
    "swot_waveheight": "SWOT significant wave height in metres.",
    "swot_time": "UTC SWOT reference time, rounded down to whole seconds.",
    "swot_source": "Source path from the SWOT catalogue swot_path column.",
}
SOURCE_COLUMNS = (
    "sar_time",
    "sar_lat",
    "sar_lon",
    "sar_incidence_angle",
    "sar_elevation_angle",
    "sar_distance_to_coast",
    "sar_safe_slc",
    "sar_safe_ocn",
    "ref_lon",
    "ref_lat",
    "ref_time",
    "ref_mean_hs_karin",
    "swot_path",
    "oswLandCoverage",
    "swot_dynamic_ice_flag",
    "ref_time_delta",
    "oswTotalHs",
    "ref_hs_alti_closest",
    "ww3_hs",
    "prob_1",
    "overlap_pct",
    "max_rainrate_IMERG",
    "swot_rain_flag",
    "class_1",
)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--swot-catalogue",
        action="append",
        required=True,
        type=Path,
        help="SWOT co-aligned WV catalogue; repeat once for S1A, S1C, and S1D",
    )
    parser.add_argument("--test-dir", type=Path, default=DEFAULT_TEST_DIR)
    parser.add_argument("--report-dir", type=Path, default=PACKAGE_ROOT / "runs" / "swot_merged")
    parser.add_argument("--version", default="0.1")
    parser.add_argument("--production-date", help="reproducible YYYYMMDD output date")
    parser.add_argument("--miktex-bin", default=os.environ.get("MIKTEX_BIN"))
    parser.add_argument("--compile", action=argparse.BooleanOptionalAction, default=True)
    args = parser.parse_args(argv)

    missions = []
    for path in args.swot_catalogue:
        match = MISSION_PATTERN.match(path.name)
        if match is None:
            parser.error(f"invalid WV co-aligned catalogue filename: {path.name}")
        missions.append(match.group(1))
    if len(missions) != len(EXPECTED_MISSIONS) or set(missions) != set(EXPECTED_MISSIONS):
        parser.error("provide exactly one catalogue each for S1A, S1C, and S1D")
    if len(set(missions)) != len(missions):
        parser.error("provide exactly one catalogue each for S1A, S1C, and S1D")
    if args.production_date:
        if not re.fullmatch(r"\d{8}", args.production_date):
            parser.error("--production-date must use YYYYMMDD")
        try:
            datetime.strptime(args.production_date, "%Y%m%d")
        except ValueError:
            parser.error("--production-date must be a valid calendar date")
    if not re.fullmatch(r"\d+\.\d+", args.version):
        parser.error("--version must use X.Y format")
    return args


def read_swot_catalogue(path: Path) -> pd.DataFrame:
    """Read and normalize one S1A/S1C/S1D SWOT WV catalogue."""
    path = Path(path)
    match = MISSION_PATTERN.match(path.name)
    if match is None:
        raise ValueError(f"{path}: expected an S1A/S1C/S1D SWOT WV catalogue filename")
    missing = [column for column in SOURCE_COLUMNS if column not in pq.read_schema(path).names]
    if missing:
        raise ValueError(
            f"{match.group(1)} {path}: missing required source columns: {', '.join(missing)}"
        )

    frame = pq.read_table(path, columns=list(SOURCE_COLUMNS)).to_pandas()
    for column in ("sar_safe_slc", "sar_safe_ocn"):
        frame[column] = (
            frame[column]
            .astype("string")
            .str.rsplit("/", n=1)
            .str[-1]
            .replace({"nan": pd.NA, "None": pd.NA, "<NA>": pd.NA, "": pd.NA})
        )
    frame["swot_lon"] = frame.pop("ref_lon")
    frame["swot_lat"] = frame.pop("ref_lat")
    frame["swot_time"] = frame.pop("ref_time")
    frame["swot_waveheight"] = pd.to_numeric(frame.pop("ref_mean_hs_karin"), errors="coerce")
    frame["swot_source"] = frame.pop("swot_path").astype("string")
    frame["sar_ground_heading"] = pd.Series(pd.NA, index=frame.index, dtype="Float32")
    frame["_mission"] = match.group(1)
    frame["_source_path"] = str(path)
    frame["_input_order"] = range(len(frame))
    return frame


TEST_COLUMNS = [
    "primary_key",
    "sar_time",
    "sar_lat",
    "sar_lon",
    "sar_incidence_angle",
    "sar_elevation_angle",
    "sar_ground_heading",
    "sar_distance_to_coast",
    "sar_safe_slc",
    "sar_safe_ocn",
    "swot_lon",
    "swot_lat",
    "swot_waveheight",
    "swot_time",
    "swot_source",
]
TARGET_COLUMNS = [
    "primary_key",
    "sar_safe_slc",
    "sar_safe_ocn",
    "swot_lon",
    "swot_lat",
    "swot_time",
]
KEY_FIELDS = ("sar_safe_slc", "swot_lon", "swot_lat")
REQUIRED_VALUES = (
    "sar_safe_ocn",
    "sar_time",
    "sar_lat",
    "sar_lon",
    "sar_incidence_angle",
    "sar_elevation_angle",
    "sar_distance_to_coast",
    "swot_time",
    "swot_waveheight",
    "swot_source",
)


QUALITY_FILTERS = (
    (
        "S1 land and coast proxy",
        "oswLandCoverage == 0 and sar_distance_to_coast > 0",
        lambda f: (f["oswLandCoverage"] == 0) & (f["sar_distance_to_coast"] > 0),
    ),
    ("dynamic ice == 0", "swot_dynamic_ice_flag == 0", lambda f: f["swot_dynamic_ice_flag"] == 0),
    ("time delta present", "ref_time_delta not NaN", lambda f: f["ref_time_delta"].notna()),
    ("SWOT KaRIn Hs > 0", "ref_mean_hs_karin > 0", lambda f: f["swot_waveheight"] > 0),
    ("S1 OCN Hs > 0", "oswTotalHs > 0", lambda f: f["oswTotalHs"] > 0),
    (
        "nearest altimeter Hs > 0",
        "ref_hs_alti_closest > 0 (proxy for SWOT nadir Hs)",
        lambda f: f["ref_hs_alti_closest"] > 0,
    ),
    ("WW3 Hs > 0", "ww3_hs > 0", lambda f: f["ww3_hs"] > 0),
    ("classification present", "prob_1 >= 0", lambda f: f["prob_1"] >= 0),
    ("time delta < 2 h", "ref_time_delta < 7200 seconds", lambda f: f["ref_time_delta"] < 7200),
    ("full overlap", "overlap_pct >= 100", lambda f: f["overlap_pct"] >= 100),
    ("no IMERG rain", "max_rainrate_IMERG <= 0", lambda f: f["max_rainrate_IMERG"] <= 0),
    ("SWOT rain flag == 0", "swot_rain_flag == 0", lambda f: f["swot_rain_flag"] == 0),
    (
        "S1 accepted classes",
        "class_1 in AF, BS, MCC, OF, POS, RC, WS",
        lambda f: f["class_1"].isin(("AF", "BS", "MCC", "OF", "POS", "RC", "WS")),
    ),
)
QUALITY_NUMERIC_COLUMNS = (
    "oswLandCoverage",
    "swot_dynamic_ice_flag",
    "ref_time_delta",
    "oswTotalHs",
    "ref_hs_alti_closest",
    "ww3_hs",
    "prob_1",
    "overlap_pct",
    "max_rainrate_IMERG",
    "swot_rain_flag",
    "sar_distance_to_coast",
    "swot_waveheight",
)


def apply_quality_filters(frame: pd.DataFrame) -> tuple[pd.DataFrame, list[dict]]:
    """Apply the thirteen NECTAR criteria in order to a normalized SWOT catalogue."""
    frame = frame.copy()
    for column in QUALITY_NUMERIC_COLUMNS:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    steps = []
    for name, rule, predicate in QUALITY_FILTERS:
        before = len(frame)
        mask = predicate(frame).fillna(False).astype(bool)
        frame = frame.loc[mask].copy()
        steps.append(
            {
                "name": name,
                "rule": rule,
                "before": before,
                "remaining": len(frame),
                "removed": before - len(frame),
            }
        )
    return frame, steps


def _second_precision(values):
    return (
        pd.to_datetime(values, utc=True).dt.tz_convert(None).dt.floor("s").astype("datetime64[ns]")
    )


def build_swot_test_frames(catalogues):
    """Merge three SWOT WV catalogues into ordered TEST/TARGET frames and provenance."""
    paths = [Path(path) for path in catalogues]
    by_mission = {}
    for path in paths:
        match = MISSION_PATTERN.match(path.name)
        if match is None:
            raise ValueError(f"invalid SWOT WV catalogue filename: {path}")
        mission = match.group(1)
        if mission in by_mission:
            raise ValueError(f"duplicate catalogue for {mission}: {path}")
        by_mission[mission] = path
    if tuple(sorted(by_mission)) != tuple(sorted(EXPECTED_MISSIONS)):
        raise ValueError("provide exactly one catalogue each for S1A, S1C, and S1D")

    merged = []
    sources = []
    exclusions = {"missing_key_fields": 0, "missing_required_fields": 0, "duplicate_keys": 0}
    quality_totals = [
        {"name": name, "rule": rule, "before": 0, "remaining": 0, "removed": 0}
        for name, rule, _ in QUALITY_FILTERS
    ]
    input_rows = 0
    for mission in EXPECTED_MISSIONS:
        path = by_mission[mission]
        frame = read_swot_catalogue(path)
        count = len(frame)
        input_rows += count
        frame, quality_steps = apply_quality_filters(frame)
        for total, step in zip(quality_totals, quality_steps, strict=True):
            for key in ("before", "remaining", "removed"):
                total[key] += step[key]
        mission_exclusions = {reason: 0 for reason in exclusions}
        numeric = (
            "sar_lat",
            "sar_lon",
            "sar_incidence_angle",
            "sar_elevation_angle",
            "sar_distance_to_coast",
            "swot_lon",
            "swot_lat",
            "swot_waveheight",
        )
        for column in numeric:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        frame["sar_time"] = pd.to_datetime(frame["sar_time"], utc=True, errors="coerce")
        frame["swot_time"] = pd.to_datetime(frame["swot_time"], utc=True, errors="coerce")

        key_missing = frame["sar_safe_slc"].isna() | frame[["swot_lon", "swot_lat"]].isna().any(
            axis=1
        )
        exclusions["missing_key_fields"] += int(key_missing.sum())
        mission_exclusions["missing_key_fields"] += int(key_missing.sum())
        frame = frame.loc[~key_missing].copy()
        remaining_required = frame[list(REQUIRED_VALUES)].isna().any(axis=1)
        numeric_required = (
            "sar_lat",
            "sar_lon",
            "sar_incidence_angle",
            "sar_elevation_angle",
            "sar_distance_to_coast",
            "swot_lon",
            "swot_lat",
            "swot_waveheight",
        )
        finite = np.isfinite(frame[list(numeric_required)]).all(axis=1)
        invalid = remaining_required | ~finite
        exclusions["missing_required_fields"] += int(invalid.sum())
        mission_exclusions["missing_required_fields"] += int(invalid.sum())
        frame = frame.loc[~invalid].copy()
        frame["primary_key"] = (
            frame["sar_safe_slc"]
            + "_"
            + frame["swot_lon"].map(lambda value: f"{value:.1f}")
            + "_"
            + frame["swot_lat"].map(lambda value: f"{value:.1f}")
        )
        frame["_time_delta_s"] = (frame["swot_time"] - frame["sar_time"]).abs().dt.total_seconds()
        frame = frame.sort_values(["primary_key", "_time_delta_s", "_input_order"], kind="stable")
        deduplicated = frame.drop_duplicates("primary_key", keep="first")
        duplicate_count = len(frame) - len(deduplicated)
        exclusions["duplicate_keys"] += duplicate_count
        mission_exclusions["duplicate_keys"] += duplicate_count
        deduplicated = deduplicated.sort_values("_input_order", kind="stable")
        sources.append(
            {
                "mission": mission,
                "path": str(path),
                "input_rows": count,
                "quality_filter_steps": quality_steps,
                "accepted_rows": len(deduplicated),
                "excluded_rows": mission_exclusions,
            }
        )
        merged.append(deduplicated)

    frame = pd.concat(merged, ignore_index=True)
    duplicate = frame["primary_key"].duplicated(keep=False)
    if duplicate.any():
        collision_keys = frame.loc[duplicate].groupby("primary_key")["_mission"].nunique()
        if collision_keys.gt(1).any():
            raise ValueError("primary_key collision across missions")
        raise ValueError("duplicate primary_key remains after mission-level deduplication")
    if frame.empty:
        raise ValueError("no valid SWOT rows remain after required-field filtering")

    frame = frame.assign(
        sar_time=_second_precision(frame["sar_time"]),
        swot_time=_second_precision(frame["swot_time"]),
    )
    for column in (
        "sar_lat",
        "sar_lon",
        "sar_incidence_angle",
        "sar_elevation_angle",
        "sar_distance_to_coast",
        "swot_lon",
        "swot_lat",
        "swot_waveheight",
    ):
        frame[column] = frame[column].astype("float32")
    frame["sar_ground_heading"] = pd.Series(pd.NA, index=frame.index, dtype="Float32")
    test = frame[TEST_COLUMNS].reset_index(drop=True)
    target = test[TARGET_COLUMNS].copy()
    manifest = {
        "sources": sources,
        "input_rows": input_rows,
        "quality_filter_steps": quality_totals,
        "accepted_rows": len(test),
        "excluded_rows": exclusions,
        "key_formula": "sar_safe_slc + '_' + swot_lon + '_' + swot_lat (coordinates at 0.1 degree)",
    }
    return test, target, manifest


SWOT_METADATA = {
    "source swot": "PODAAC SWOT Karin L2 WindWave PGD0 and PID0 (D0)",
    "source ancillary datasets": "Sentinel-1 SAR fields from SWOT co-aligned catalogue",
    "library used to produce the parquet": "soba_reference_repo",
}


def validate_swot_test_pair(test_path: Path, target_path: Path) -> bool:
    """Validate the notebook-specific pair schema, metadata, and shared keys."""
    test_path, target_path = Path(test_path), Path(target_path)
    test_schema = pq.read_schema(test_path)
    target_schema = pq.read_schema(target_path)
    if test_schema.names != TEST_COLUMNS:
        raise ValueError(f"TEST schema mismatch: {test_schema.names}")
    if target_schema.names != TARGET_COLUMNS:
        raise ValueError(f"TARGET schema mismatch: {target_schema.names}")
    expected_metadata = {
        b"source swot",
        b"source ancillary datasets",
        b"library used to produce the parquet",
        b"library version",
        b"creation date",
    }
    if set(test_schema.metadata or {}) != expected_metadata:
        raise ValueError("TEST metadata keys do not match the required five attributes")
    if set(target_schema.metadata or {}) != expected_metadata:
        raise ValueError("TARGET metadata keys do not match the required five attributes")
    if test_schema.metadata != target_schema.metadata:
        raise ValueError("TEST and TARGET metadata values differ")

    test = pq.read_table(test_path).to_pandas()
    target = pq.read_table(target_path).to_pandas()
    if not test["primary_key"].is_unique or not target["primary_key"].is_unique:
        raise ValueError("primary_key values must be unique in both files")
    if test["primary_key"].tolist() != target["primary_key"].tolist():
        raise ValueError("TEST and TARGET primary_key values differ")
    for frame in (test, target):
        if frame[list(KEY_FIELDS)].isna().any().any():
            raise ValueError("primary_key components must be non-null")
        expected = (
            frame["sar_safe_slc"]
            + "_"
            + frame["swot_lon"].map(lambda value: f"{float(value):.1f}")
            + "_"
            + frame["swot_lat"].map(lambda value: f"{float(value):.1f}")
        )
        if not frame["primary_key"].equals(expected):
            raise ValueError("primary_key composition mismatch")
    return True


def write_swot_test_pair(test, target, test_dir, production_date, version="0.1"):
    """Write a TEST/TARGET pair under one dated directory and validate read-back."""
    if not re.fullmatch(r"\d{8}", str(production_date)):
        raise ValueError("production_date must use YYYYMMDD")
    if not re.fullmatch(r"\d+\.\d+", str(version)):
        raise ValueError("version must use X.Y format")
    stem = f"S1_WV_{production_date}_swh_{version}"
    directory = Path(test_dir) / stem
    test_name = f"S1_reference_test_dataset_WV_{production_date}_swh_{version}.parquet"
    target_name = f"S1_target_dataset_WV_{production_date}_swh_{version}.parquet"
    expected_names = {test_name, target_name}
    directory.mkdir(parents=True, exist_ok=True)
    unexpected = {path.name for path in directory.iterdir()} - expected_names
    if unexpected:
        raise FileExistsError(
            f"refusing to overwrite unrelated files in {directory}: {sorted(unexpected)}"
        )

    metadata = {
        **{key.encode(): value.encode() for key, value in SWOT_METADATA.items()},
        b"library version": library_version().encode(),
        b"creation date": str(production_date).encode(),
    }
    outputs = []
    for frame, name, columns in (
        (test, test_name, TEST_COLUMNS),
        (target, target_name, TARGET_COLUMNS),
    ):
        if frame.columns.tolist() != columns:
            raise ValueError(
                f"cannot write {name}: expected columns {columns}, got {frame.columns.tolist()}"
            )
        path = directory / name
        table = pa.Table.from_pandas(frame, preserve_index=False).replace_schema_metadata(metadata)
        pq.write_table(table, path)
        outputs.append(path)
    validate_swot_test_pair(*outputs)
    return tuple(outputs)


def summarize_swot_test(test: pd.DataFrame, manifest: dict) -> dict:
    """Return report statistics with JSON-serializable nulls for empty values."""
    swh = pd.to_numeric(
        test.get("swot_waveheight", pd.Series(dtype=float)), errors="coerce"
    ).dropna()
    missions = {
        source["mission"]: int(source["accepted_rows"]) for source in manifest.get("sources", [])
    }
    return {
        "row_count": int(len(test)),
        "mission_counts": missions,
        "swh_count": int(len(swh)),
        "swh_mean": float(swh.mean()) if len(swh) else None,
        "swh_median": float(swh.median()) if len(swh) else None,
        "swh_min": float(swh.min()) if len(swh) else None,
        "swh_max": float(swh.max()) if len(swh) else None,
        "missing_heading_count": int(test["sar_ground_heading"].isna().sum())
        if "sar_ground_heading" in test
        else 0,
        "excluded_rows": manifest.get("excluded_rows", {}),
    }


def _latex_escape(value) -> str:
    replacements = {
        "\\": r"\textbackslash{}",
        "&": r"\&",
        "%": r"\%",
        "$": r"\$",
        "#": r"\#",
        "_": r"\_",
        "{": r"\{",
        "}": r"\}",
        "~": r"\textasciitilde{}",
        "^": r"\textasciicircum{}",
    }
    return str(value).translate(str.maketrans(replacements))


def build_swot_test_report(
    test_path, source_paths, manifest, summary, figures, report_dir, compile_report=True
):
    """Fill the SOBA TEST document template for this SWOT-only export."""
    test_path, report_dir = Path(test_path), Path(report_dir)
    match = re.search(r"_WV_(\d{8})_swh_(.+)$", test_path.stem)
    if match is None:
        raise ValueError(f"cannot derive production date and version from {test_path.name}")
    metadata = pq.read_schema(test_path).metadata or {}
    metadata_rows = "\n".join(
        f"\\texttt{{{_latex_escape(key.decode())}}} & {_latex_escape(metadata[key].decode())} \\\\"
        for key in (
            b"source swot",
            b"source ancillary datasets",
            b"library used to produce the parquet",
            b"library version",
            b"creation date",
        )
    )
    mission_rows = "\n".join(
        f"{mission} & {summary['mission_counts'].get(mission, 0):,} \\\\"
        for mission in EXPECTED_MISSIONS
    )
    exclusion_rows = (
        "\n".join(
            f"{_latex_escape(reason)} & {int(count):,} \\\\"
            for reason, count in summary.get("excluded_rows", {}).items()
        )
        or r"None & 0 \\"
    )
    schema = pq.read_schema(test_path)
    groups = {
        "Identification": ("primary_key", "sar_safe_slc", "sar_safe_ocn"),
        "SAR geometry": tuple(
            name
            for name in TEST_COLUMNS
            if name.startswith("sar_") and name not in {"sar_safe_slc", "sar_safe_ocn"}
        ),
        "Reference (SWOT KaRIn)": tuple(name for name in TEST_COLUMNS if name.startswith("swot_")),
    }
    if sorted(name for names in groups.values() for name in names) != sorted(TEST_COLUMNS):
        raise ValueError("TEST schema and SOBA document column groups differ")
    linebreak = "\\" * 2
    quality_rows = "\n".join(
        f"    {_latex_escape(step['name'])} & "
        f"\\texttt{{{_latex_escape(step['rule'])}}} & "
        f"{step['remaining']:,} & {step['removed']:,} {linebreak}"
        for step in manifest["quality_filter_steps"]
    )
    column_rows = []
    for title, names in groups.items():
        column_rows.extend(
            [
                r"    \midrule",
                r"    \multicolumn{3}{@{}l}{\textbf{" + title + "}} " + linebreak,
            ]
        )
        for name in names:
            column_rows.append(
                f"    \\texttt{{{_latex_escape(name)}}} & "
                f"{_latex_escape(schema.field(name).type)} & "
                f"{_latex_escape(SWOT_COLUMN_DESCRIPTIONS[name])} {linebreak}"
            )
    if summary["swh_count"]:
        stats = " & ".join(
            f"{summary[key]:.2f}" for key in ("swh_min", "swh_max", "swh_mean", "swh_median")
        )
    else:
        stats = " & ".join(["--"] * 4)
    stats_header = (
        r"    \textbf{Reference parameter} & \textbf{Min} & \textbf{Max} & "
        r"\textbf{Mean} & \textbf{Median} " + linebreak
    )
    stats_table = "\n".join(
        [
            r"\begin{longtable}{@{}p{6.4cm}rrrr@{}}",
            (
                r"    \caption{Reference parameter statistics of the "
                r"reference TEST dataset.\label{tab:reference_stats}} " + linebreak
            ),
            r"    \toprule",
            stats_header,
            r"    \midrule",
            r"    \endfirsthead",
            r"    \toprule",
            stats_header,
            r"    \midrule",
            r"    \endhead",
            r"    \bottomrule",
            r"    \endlastfoot",
            f"    SWOT KaRIn wave height (m) & {stats} {linebreak}",
            r"\end{longtable}",
        ]
    )
    command = "\n".join(
        [
            "soba_reference_repo swot-test \\",
            *(f'  --swot-catalogue "$DATA_DIR/{Path(path).name}" \\' for path in source_paths),
            '  --test-dir "$TEST_DIR" --report-dir "$REPORT_DIR" \\',
            f"  --production-date {match.group(1)} --version {match.group(2)}"
            + (" --no-compile" if not compile_report else ""),
        ]
    )
    key_formula = (
        r"\texttt{sar\_safe\_slc} + \texttt{swot\_lon} + \texttt{swot\_lat} "
        r"(longitude and latitude formatted to one decimal place)"
    )
    steps = "\n".join(
        [
            r"    \item \textbf{step 1:} read the S1A, S1C and S1D WV SWOT co-aligned catalogues;",
            r"    \item \textbf{step 2:} apply the thirteen quality filters in the table above;",
            (
                r"    \item \textbf{step 3:} map source fields to the \texttt{swot\_} family "
                "and normalise SAFE identifiers;"
            ),
            r"    \item \textbf{step 4:} exclude rows with incomplete key or required fields;",
            r"    \item \textbf{step 5:} keep the closest-time duplicate within each mission;",
            r"    \item \textbf{step 6:} reject cross-mission key collisions;",
            r"    \item \textbf{step 7:} write and validate the paired TEST and TARGET Parquets.",
        ]
    )
    date = match.group(1)
    date_iso = f"{date[:4]}-{date[4:6]}-{date[6:]}"
    tokens = {
        "@@DATASET_NAME@@": "S1A / S1C / S1D / SWOT KaRIn",
        "@@FILE_VERSION_ROW@@": (
            f"{_path(test_path.name)} & {date_iso} & Merged SWOT KaRIn TEST dataset; "
            f"{summary['row_count']:,} rows. {linebreak}"
        ),
        "@@GENERAL_DESCRIPTION@@": (
            "Sentinel-1 Wave Mode (WV) SAR matchups with SWOT KaRIn L2 WindWave "
            f"significant wave height (m). The merged dataset contains {summary['row_count']:,} "
            f"rows from S1A, S1C and S1D. TEST file: {_path(test_path.name)}. "
            r"The reference parameter is \texttt{swot\_waveheight}; "
            r"\texttt{sar\_ground\_heading} is null because it is unavailable in the source."
        ),
        "@@SOURCE_FILES@@": "\n".join(
            f"        \\item {_path(Path(path).name)}" for path in source_paths
        ),
        "@@MISSION_ROWS@@": mission_rows,
        "@@QUALITY_FILTER_ROWS@@": quality_rows,
        "@@ROW_COUNT@@": f"{summary['row_count']:,}",
        "@@EXCLUSION_ROWS@@": exclusion_rows,
        "@@KEY_FORMULA@@": key_formula,
        "@@TEST_COLUMNS@@": "\n".join(column_rows),
        "@@REFERENCE_STATS_TABLE@@": stats_table,
        "@@METADATA_ROWS@@": metadata_rows,
        "@@PROCESS_STEPS@@": steps,
        "@@COMMAND@@": command,
        "@@RUN_CONFIG@@": (
            "mode: WV\nreference: SWOT KaRIn\nmissions: S1A, S1C, S1D\n"
            f"production_date: {date}\nversion: {match.group(2)}\n"
            f"compile_pdf: {str(compile_report).lower()}\n"
            "duplicate_keys: closest_reference_time\nmissing_required_values: exclude"
        ),
        "@@COVERAGE_FIGURE@@": Path(figures["coverage"]).relative_to(report_dir).as_posix(),
        "@@MONTHLY_FIGURE@@": Path(figures["monthly_rows"]).relative_to(report_dir).as_posix(),
        "@@SWH_FIGURE@@": Path(figures["swot_waveheight"]).relative_to(report_dir).as_posix(),
    }
    template = DEFAULT_SWOT_TEMPLATE.read_text(encoding="utf-8")
    for token, value in tokens.items():
        if template.count(token) != 1:
            raise ValueError(f"LaTeX template must contain {token} exactly once")
        template = template.replace(token, value)
    if "@@" in template:
        raise ValueError("unexpanded token remains in the SWOT TEST template")
    return template


def plot_swot_figures(test: pd.DataFrame, figures_dir: Path) -> dict[str, Path]:
    """Write coverage, monthly-count, and SWH-distribution figures for the TEST rows."""
    figures_dir = Path(figures_dir)
    figures_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "coverage": figures_dir / "sar_coverage.png",
        "monthly_rows": figures_dir / "monthly_rows.png",
        "swot_waveheight": figures_dir / "swot_waveheight_distribution.png",
    }

    missions = test["sar_safe_slc"].astype("string").str.extract(r"^(S1[A-D])", expand=False)
    if missions.isna().any():
        raise ValueError("cannot identify the SAR mission for every TEST row")
    present = [mission for mission in MISSION_COLORS if missions.eq(mission).any()]

    figure, axis = plt.subplots(figsize=(9, 4.5))
    if len(test):
        axis.scatter(test["sar_lon"], test["sar_lat"], s=2, alpha=0.4, color="#355C7D")
    axis.set(
        xlim=(-180, 180),
        ylim=(-90, 90),
        xlabel="Longitude (degrees)",
        ylabel="Latitude (degrees)",
        title="Sentinel-1 WV coverage",
    )
    axis.grid(alpha=0.2)
    figure.tight_layout()
    figure.savefig(paths["coverage"], dpi=150)
    plt.close(figure)

    figure, axis = plt.subplots(figsize=(9, 4.5))
    times = pd.to_datetime(test["sar_time"], errors="coerce", utc=True)
    periods = times.dt.tz_localize(None).dt.to_period("M")
    monthly = (
        pd.DataFrame({"month": periods, "mission": missions})
        .dropna()
        .groupby(["month", "mission"])
        .size()
        .unstack(fill_value=0)
        .sort_index()
    )
    bottom = pd.Series(0, index=monthly.index)
    for mission in present:
        counts = monthly[mission] if mission in monthly else pd.Series(0, index=monthly.index)
        axis.bar(
            monthly.index.astype(str),
            counts,
            bottom=bottom,
            color=MISSION_COLORS[mission],
            label=mission,
        )
        bottom += counts
    if present:
        axis.legend()
    axis.set(xlabel="SAR month", ylabel="TEST rows", title="TEST rows by month")
    axis.tick_params(axis="x", labelrotation=45)
    axis.grid(axis="y", alpha=0.2)
    figure.tight_layout()
    figure.savefig(paths["monthly_rows"], dpi=150)
    plt.close(figure)

    figure, axis = plt.subplots(figsize=(9, 4.5))
    swh = pd.Series(pd.to_numeric(test["swot_waveheight"], errors="coerce"), index=test.index)
    if present:
        axis.hist(
            [swh.loc[missions.eq(mission)].dropna().to_numpy() for mission in present],
            bins=40,
            stacked=True,
            color=[MISSION_COLORS[mission] for mission in present],
            label=present,
        )
        axis.legend()
    axis.set(
        xlabel="SWOT significant wave height (m)",
        ylabel="TEST rows",
        title="Reference SWH distribution",
    )
    axis.grid(axis="y", alpha=0.2)
    figure.tight_layout()
    figure.savefig(paths["swot_waveheight"], dpi=150)
    plt.close(figure)
    return paths


def compile_swot_pdf(output_dir: Path, stem: str, miktex_bin=None) -> Path:
    """Compile the report with pdflatex, falling back to Tectonic when available."""
    output_dir = Path(output_dir)
    try:
        return compile_pdf(output_dir, stem, miktex_bin)
    except FileNotFoundError:
        if miktex_bin:
            raise
        tectonic = shutil.which("tectonic")
        if tectonic is None:
            raise
        completed = subprocess.run(
            [tectonic, "--keep-intermediates", "--keep-logs", "--reruns", "1", f"{stem}.tex"],
            cwd=output_dir,
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode:
            output = (completed.stdout + completed.stderr)[-4000:]
            raise RuntimeError(f"Tectonic failed for {stem}.tex:\n{output}")
        pdf_path = output_dir / f"{stem}.pdf"
        if not pdf_path.is_file():
            raise RuntimeError(f"Tectonic reported success but {pdf_path} is missing")
        return pdf_path


def main(argv=None) -> int:
    args = parse_args(argv)
    production_date = args.production_date or datetime.now(timezone.utc).strftime("%Y%m%d")
    test, target, manifest = build_swot_test_frames(args.swot_catalogue)
    test_path, target_path = write_swot_test_pair(
        test, target, args.test_dir, production_date, args.version
    )

    report_dir = args.report_dir / test_path.stem
    report_dir.mkdir(parents=True, exist_ok=True)
    figures_dir = report_dir / f"images_{test_path.stem}"
    figures = plot_swot_figures(test, figures_dir)
    summary = summarize_swot_test(test, manifest)
    stage_latex_assets(DEFAULT_SWOT_TEMPLATE.parent, report_dir)
    tex = build_swot_test_report(
        test_path,
        [source["path"] for source in manifest["sources"]],
        manifest,
        summary,
        figures,
        report_dir,
        compile_report=args.compile,
    )
    tex_path = report_dir / f"{test_path.stem}.tex"
    tex_path.write_text(tex, encoding="utf-8")

    pdf_path = None
    if args.compile:
        pdf_path = compile_swot_pdf(report_dir, test_path.stem, args.miktex_bin)
        purge_latex_byproducts(report_dir, test_path.stem)
    manifest.update(
        {
            "mode": "WV",
            "variable": "swh",
            "version": args.version,
            "production_date": production_date,
            "test_parquet": str(test_path),
            "target_parquet": str(target_path),
            "report_tex": str(tex_path),
            "pdf": str(pdf_path) if pdf_path else None,
            "figures": {name: str(path) for name, path in figures.items()},
            "summary": summary,
        }
    )
    manifest_path = report_dir / f"{test_path.stem}_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    print(f"accepted rows: {len(test):,} of {manifest['input_rows']:,}")
    for source in manifest["sources"]:
        print(f"{source['mission']}: {source['accepted_rows']:,} of {source['input_rows']:,}")
    print(f"TEST:   {test_path}")
    print(f"TARGET: {target_path}")
    print(f"REPORT: {tex_path}")
    if pdf_path:
        print(f"PDF:    {pdf_path}")
    return 0
