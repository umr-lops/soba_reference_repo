"""Create a merged SWOT WV TEST/TARGET pair and its report."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from matplotlib import pyplot as plt

from .pdf_support import (
    ASSET_DIR,
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
    return _normalize_swot_frame(frame, match.group(1), path)


def _normalize_swot_frame(frame, mission, path, input_order=None):
    frame = frame.copy()
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
    frame["_mission"] = mission
    frame["_source_path"] = str(path)
    frame["_input_order"] = input_order if input_order is not None else range(len(frame))
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

    return _build_swot_frames(by_mission)


def build_swot_test_from_merged(merged_path, curation):
    """Generate the pair from saved Curated rows without running filters again."""
    table = pq.read_table(merged_path)
    required = {
        "sar_time", "sar_lat", "sar_lon", "sar_incidence_angle", "sar_elevation_angle",
        "sar_distance_to_coast", "sar_safe_slc", "sar_safe_ocn", "ref_lon", "ref_lat",
        "ref_time", "ref_mean_hs_karin", "swot_path", "_curation_mission", "_curation_row",
    }
    missing = required - set(table.column_names)
    if missing:
        raise ValueError(f"merged Curated file lacks output fields: {sorted(missing)}")
    raw = table.to_pandas()
    by_mission = {}
    for mission, info in sorted(curation.items()):
        selected = raw.loc[raw["_curation_mission"].eq(mission)].copy()
        for safe_column in ("sar_safe_slc", "sar_safe_ocn"):
            safe = (
                selected[safe_column].astype("string").str.rsplit("/", n=1).str[-1]
                .replace({"nan": pd.NA, "None": pd.NA, "<NA>": pd.NA, "": pd.NA})
            )
            matches = safe.str.startswith(f"{mission}_WV_").fillna(False).astype(bool)
            wrong = safe.notna() & ~matches
            if wrong.any():
                raise ValueError(f"{mission}: SAFE mission mismatch in {safe_column}")
        by_mission[mission] = {
            "frame": _normalize_swot_frame(
                selected[[column for column in SOURCE_COLUMNS if column in required]],
                mission, info["path"],
                input_order=selected["_curation_row"].to_numpy(),
            ),
            "path": info["path"],
            "input_rows": info["input_rows"],
            "steps": info["steps"],
            "curated_path": info["curated_path"],
        }
    if len(raw) != sum(len(item["frame"]) for item in by_mission.values()):
        raise ValueError("merged Curated file contains an unknown mission")
    return _build_swot_frames(by_mission, curated=True)


def _build_swot_frames(by_mission, curated=False):
    merged = []
    sources = []
    exclusions = {"missing_key_fields": 0, "missing_required_fields": 0, "duplicate_keys": 0}
    quality_totals = [
        {"name": name, "rule": rule, "before": 0, "remaining": 0, "removed": 0}
        for name, rule, _ in QUALITY_FILTERS
    ]
    input_rows = 0
    for mission in sorted(by_mission):
        item = by_mission[mission] if curated else {"path": by_mission[mission]}
        path = item["path"]
        frame = item["frame"] if curated else read_swot_catalogue(path)
        count = item["input_rows"] if curated else len(frame)
        input_rows += count
        if curated:
            quality_steps = item["steps"]
        else:
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
        for coordinate in ("swot_lon", "swot_lat"):
            frame[coordinate] = frame[coordinate].astype("float32")
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
        source_record = {
            "mission": mission,
            "path": str(path),
            "input_rows": count,
            "quality_filter_steps": quality_steps,
            "accepted_rows": len(deduplicated),
            "excluded_rows": mission_exclusions,
        }
        if curated:
            source_record["curated_path"] = item["curated_path"]
            source_record["curated_rows"] = len(item["frame"])
        sources.append(source_record)
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
        "quality_filter_steps": [] if curated else quality_totals,
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
    test_path, manifest, summary, figures, report_dir, compile_report=True
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
    report_missions = summary["mission_counts"]
    mission_rows = "\n".join(
        f"{mission} & {summary['mission_counts'].get(mission, 0):,} \\\\"
        for mission in report_missions
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

    def filter_table(caption, label, headers, rows, widths):
        header = " & ".join(r"\textbf{" + _latex_escape(h) + "}" for h in headers)
        return "\n".join([
            rf"\begin{{longtable}}{{@{{}}{''.join('p{' + w + '}' for w in widths)}@{{}}}}",
            rf"    \caption{{{caption}.\label{{tab:{label}}}}} {linebreak}",
            r"    \toprule", f"    {header} {linebreak}", r"    \midrule",
            r"    \endfirsthead", r"    \toprule", f"    {header} {linebreak}",
            r"    \midrule", r"    \endhead", r"    \bottomrule",
            r"    \endlastfoot", *rows, r"\end{longtable}",
        ])

    if manifest.get("curated_merge"):
        sources = manifest["sources"]
        steps_by_mission = {
            source["mission"]: {step["id"]: step for step in source["quality_filter_steps"]}
            for source in sources
        }
        ids = list(dict.fromkeys(
            step["id"] for source in sources for step in source["quality_filter_steps"]
        ))
        def definition(step):
            return (step["name"], step["status"], step["clauses"])

        common = {
            rule_id for rule_id in ids
            if all(rule_id in steps for steps in steps_by_mission.values())
            and len({str(definition(steps[rule_id])) for steps in steps_by_mission.values()}) == 1
        }
        def rule_row(step):
            rule = "disabled" if step["status"] == "disabled" else str(step["clauses"])
            return (f"    {_latex_escape(step['name'])} & "
                    f"\\texttt{{{_latex_escape(rule)}}} {linebreak}")

        tables = []
        if common:
            tables.append(r"\textbf{Common quality filters (same rule for every mission):}")
            tables.append(filter_table(
                "Common quality-filter rules", "common_filters",
                ("Filter", "Rule on source catalogue"),
                [rule_row(steps_by_mission[sources[0]["mission"]][rule_id])
                 for rule_id in ids if rule_id in common], ("4.1cm", "10.2cm"),
            ))
        for source in sources:
            changed = [steps_by_mission[source["mission"]][rule_id]
                       for rule_id in ids if rule_id not in common
                       and rule_id in steps_by_mission[source["mission"]]]
            if changed:
                mission = source["mission"]
                tables.append(rf"\textbf{{{mission}-specific quality filters:}}")
                tables.append(filter_table(
                    f"{mission}-specific quality-filter rules", f"{mission.lower()}_filters",
                    ("Filter", "Rule on source catalogue"),
                    [rule_row(step) for step in changed], ("4.1cm", "10.2cm"),
                ))
        tables.append(r"\textbf{Sequential counts by mission (Remaining / removed):}")
        tables.append(filter_table(
            "Sequential quality-filter counts by mission", "quality_filters",
            ("Filter", *(source["mission"] for source in sources)),
            ["    " + " & ".join([
                _latex_escape(next((steps[rule_id]["name"] for steps in steps_by_mission.values()
                                     if rule_id in steps), rule_id)),
                *(f"{steps[rule_id]['remaining']:,} / {steps[rule_id]['removed']:,}"
                  if rule_id in steps else "--" for steps in steps_by_mission.values()),
            ]) + f" {linebreak}" for rule_id in ids],
            ("5cm", *("3.1cm" for _ in sources)),
        ))
        quality_tables = "\n".join(tables)
    else:
        raise ValueError("SWOT report requires a recipe Curated merge")
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
    command = "soba_reference_repo --recipe recipe.toml"
    key_formula = (
        r"\texttt{sar\_safe\_slc} + \texttt{swot\_lon} + \texttt{swot\_lat} "
        r"(longitude and latitude formatted to one decimal place)"
    )
    date = match.group(1)
    date_iso = f"{date[:4]}-{date[4:6]}-{date[6:]}"
    missions = ", ".join(summary["mission_counts"])
    steps = "\n".join(
        f"    \\item {_latex_escape(text)}"
        for text in (
            "read each specified WV SWOT catalogue and apply its own resolved filter rules;",
            "save each Curated Parquet, then persist their strict-schema merge;",
            "map SWOT fields; exclude incomplete rows and closest-time duplicate keys;",
            "write and validate the aligned TEST and TARGET Parquets.",
        )
    )
    tokens = {
        "@@GEO_CRITERION@@":
            r"The geographic conditions for each catalogue are listed in its filter rows below.",
        "@@TIME_CRITERION@@": (
            r"The time conditions for each catalogue are listed below; for duplicate keys "
            r"within a mission, keep the closest reference time."
        ),
        "@@SELECTED_PRODUCTS@@": (
            "The TEST dataset merges Curated Sentinel-1 WV catalogues "
            f"from {_latex_escape(missions)} with SWOT KaRIn reference observations."
        ),
        "@@FILTER_INTRO@@": (
            r"\textbf{Quality filters applied in order within each mission "
            r"(counts restart per catalogue):}"
        ),
        "@@COMMAND_INTRO@@": (
            r"From the run folder, edit \texttt{recipe.toml} for local catalogue locations "
            r"and a fresh output directory before rerunning:"
        ),
        "@@CONFIG_INTRO@@": (
            r"The saved TOML recipe sets each catalogue's filters and run settings. "
            r"Edit its catalogue paths and choose a fresh output directory before rerunning."
        ),
        "@@DATASET_NAME@@": "S1 WV SWOT",
        "@@FILE_VERSION_ROW@@": (
            f"{_path(test_path.name)} & {date_iso} & Merged SWOT KaRIn TEST dataset; "
            f"{summary['row_count']:,} rows. {linebreak}"
        ),
        "@@GENERAL_DESCRIPTION@@": (
            "Sentinel-1 Wave Mode (WV) SAR matchups with SWOT KaRIn L2 WindWave "
            f"significant wave height (m). The merged dataset contains {summary['row_count']:,} "
            f"rows from {_latex_escape(missions)}. TEST file: {_path(test_path.name)}. "
            r"The reference parameter is \texttt{swot\_waveheight}; "
            r"\texttt{sar\_ground\_heading} is null because it is unavailable in the source."
        ),
        "@@SOURCE_FILES@@": "\n".join(
            f"        \\item {_path(Path(source['path']).name)}"
            + f" (Curated: {_path(Path(source['curated_path']).name)})"
            for source in manifest["sources"]
        ),
        "@@MISSION_ROWS@@": mission_rows,
        "@@QUALITY_FILTER_TABLES@@": quality_tables,
        "@@ROW_COUNT@@": f"{summary['row_count']:,}",
        "@@EXCLUSION_ROWS@@": exclusion_rows,
        "@@KEY_FORMULA@@": key_formula,
        "@@TEST_COLUMNS@@": "\n".join(column_rows),
        "@@REFERENCE_STATS_TABLE@@": stats_table,
        "@@METADATA_ROWS@@": metadata_rows,
        "@@PROCESS_STEPS@@": steps,
        "@@COMMAND@@": command,
        "@@RUN_CONFIG@@": (
            f"mode: WV\nreference: SWOT KaRIn\nmissions: {missions}\n"
            f"production_date: {date}\nversion: {match.group(2)}\n"
            + "recipe: recipe.toml\ncurated_merge: merged/S1_WV_swot_curated.parquet\n"
            + f"compile_pdf: {str(compile_report).lower()}\n"
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


def run_recipe(recipe_path):
    """Save mission-specific curation, then export from the persisted merge."""
    from .curation import read_recipe, resolve_rules, write_curated, merge_curated

    recipe_path = Path(recipe_path).resolve()
    recipe = read_recipe(recipe_path)
    root = Path(recipe["output_dir"])
    if root.exists():
        raise FileExistsError(f"refusing existing run directory: {root}")
    if not DEFAULT_SWOT_TEMPLATE.is_file():
        raise FileNotFoundError(DEFAULT_SWOT_TEMPLATE)
    catalogues = sorted(recipe["catalogues"], key=lambda item: item["mission"])
    expected_schema = None
    for item in catalogues:
        item["resolved_rules"] = resolve_rules(item.get("rules", []))
        schema = pq.read_schema(item["path"])
        if expected_schema is not None and not schema.equals(expected_schema, check_metadata=False):
            raise ValueError("source catalogue schemas differ; cannot merge Curated files")
        expected_schema = schema
        missing = {col for col in SOURCE_COLUMNS[:13] if col not in schema.names}
        for rule in item["resolved_rules"]:
            if rule.get("enabled", True):
                missing.update(
                    clause[0] for clause in rule["clauses"] if clause[0] not in schema.names
                )
        if missing:
            raise ValueError(f"{item['mission']}: missing source columns: {sorted(missing)}")
    root.mkdir(parents=True)
    shutil.copyfile(recipe_path, root / "recipe.toml")
    manifest = {"status": "running", "reference": "swot", "sources": []}
    stage = "curation"
    try:
        curated, provenance = {}, {}
        for item in catalogues:
            mission = item["mission"]
            target = root / "curated" / mission / (
                f"{mission}_curated_coaligned_dataset_WV_"
                f"{recipe['production_date']}_swh_{recipe['version']}.parquet"
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            info = write_curated(item["path"], target, item["resolved_rules"], mission)
            curated[mission] = target
            provenance[mission] = {
                "path": str(item["path"]), "curated_path": str(target),
                "input_rows": info["input_rows"], "curated_rows": info["curated_rows"],
                "steps": info["steps"],
            }
            manifest["sources"].append({"mission": mission, **provenance[mission]})
        stage = "merge"
        merged_path = root / "merged" / "S1_WV_swot_curated.parquet"
        merged_path.parent.mkdir(parents=True, exist_ok=True)
        merge_curated(curated, merged_path)
        manifest["curated_merge"] = str(merged_path)
        stage = "pair"
        test, target, result = build_swot_test_from_merged(merged_path, provenance)
        result["curated_merge"] = str(merged_path)
        result["recipe_path"] = str(recipe_path)
        result["recipe_snapshot"] = str(root / "recipe.toml")
        manifest = result
        test_path, target_path = write_swot_test_pair(
            test, target, root / "datasets", recipe["production_date"], recipe["version"]
        )
        stage = "report"
        report_dir = root / "report" / test_path.stem
        report_dir.mkdir(parents=True, exist_ok=True)
        figures = plot_swot_figures(test, report_dir / f"images_{test_path.stem}")
        summary = summarize_swot_test(test, result)
        stage_latex_assets(DEFAULT_SWOT_TEMPLATE.parent, report_dir)
        tex = build_swot_test_report(
            test_path, result, summary, figures,
            report_dir, compile_report=recipe.get("compile", False),
        )
        tex_path = report_dir / f"{test_path.stem}.tex"
        tex_path.write_text(tex, encoding="utf-8")
        pdf_path = None
        if recipe.get("compile", False):
            stage = "pdf"
            pdf_path = compile_swot_pdf(report_dir, test_path.stem)
            purge_latex_byproducts(report_dir, test_path.stem)
        result.update({
            "status": "complete", "reference": "swot", "mode": "WV", "variable": "swh",
            "version": recipe["version"], "production_date": recipe["production_date"],
            "test_parquet": str(test_path), "target_parquet": str(target_path),
            "report_tex": str(tex_path), "pdf": str(pdf_path) if pdf_path else None,
            "figures": {name: str(path) for name, path in figures.items()}, "summary": summary,
        })
        manifest = result
    except Exception as exc:
        manifest.update({"status": "failed", "failed_stage": stage, "error": str(exc)})
        (root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        raise
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"accepted rows: {len(test):,} of {manifest['input_rows']:,}")
    print(f"TEST:   {test_path}\nTARGET: {target_path}\nREPORT: {tex_path}")
    return 0


def main(argv=None) -> int:
    """Run the recipe-only export (also available through the package CLI)."""
    parser = argparse.ArgumentParser(description="Build a reference TEST/TARGET pair from a recipe")
    parser.add_argument("--recipe", required=True, type=Path)
    args = parser.parse_args(argv)
    return run_recipe(args.recipe)
