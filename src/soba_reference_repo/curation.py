"""Per-catalogue SWOT quality curation and persisted Arrow stages."""

from __future__ import annotations

import math
import re
import tomllib
from datetime import datetime
from pathlib import Path
from copy import deepcopy

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq


_DEFAULT_SPEC = (
    ("sea_proxy", "S1 land and coast proxy", [
        ["oswLandCoverage", "eq", 0], ["sar_distance_to_coast", "gt", 0],
    ]),
    ("no_dynamic_ice", "dynamic ice == 0", [["swot_dynamic_ice_flag", "eq", 0]]),
    ("time_present", "time delta present", [["ref_time_delta", "present"]]),
    ("karin_positive", "SWOT KaRIn Hs > 0", [["ref_mean_hs_karin", "gt", 0]]),
    ("ocn_positive", "S1 OCN Hs > 0", [["oswTotalHs", "gt", 0]]),
    ("altimeter_positive", "nearest altimeter Hs > 0", [["ref_hs_alti_closest", "gt", 0]]),
    ("ww3_positive", "WW3 Hs > 0", [["ww3_hs", "gt", 0]]),
    ("classification_present", "classification present", [["prob_1", "ge", 0]]),
    ("time_under_two_hours", "time delta < 2 h", [["ref_time_delta", "lt", 7200]]),
    ("full_overlap", "full overlap", [["overlap_pct", "ge", 100]]),
    ("imerg_rain", "no IMERG rain", [["max_rainrate_IMERG", "le", 0]]),
    ("no_swot_rain_flag", "SWOT rain flag == 0", [["swot_rain_flag", "eq", 0]]),
    ("accepted_classes", "S1 accepted classes", [
        ["class_1", "in", ["AF", "BS", "MCC", "OF", "POS", "RC", "WS"]],
    ]),
)
DEFAULT_SWOT_RULES = tuple(
    {"id": identifier, "name": name, "clauses": clauses, "enabled": True}
    for identifier, name, clauses in _DEFAULT_SPEC
)


def resolve_rules(overrides, include_defaults=True, defaults=None):
    """Resolve ordered rule overrides over a caller-selected default set."""
    base_rules = DEFAULT_SWOT_RULES if defaults is None else defaults
    rules = deepcopy(list(base_rules) if include_defaults else [])
    indices = {rule["id"]: index for index, rule in enumerate(rules)}
    seen = set()
    for override in overrides:
        _keys(override, {"id", "name", "clauses", "enabled"}, {"id"})
        identifier = override["id"]
        if not isinstance(identifier, str) or not identifier or identifier in seen:
            raise ValueError(f"duplicate or invalid rule id: {identifier!r}")
        seen.add(identifier)
        if identifier not in indices and not {"name", "clauses"} <= override.keys():
            raise ValueError(f"new rule {identifier} requires name and clauses")
        if "name" in override and (not isinstance(override["name"], str) or not override["name"]):
            raise ValueError("rule name must be nonempty")
        if "enabled" in override and not isinstance(override["enabled"], bool):
            raise ValueError("enabled must be boolean")
        if "clauses" in override:
            if not isinstance(override["clauses"], list) or not override["clauses"]:
                raise ValueError("clauses must be nonempty")
            for clause in override["clauses"]:
                _validate_clause(clause)
        if identifier in indices:
            rules[indices[identifier]].update(deepcopy(override))
        else:
            rules.append({**deepcopy(override), "enabled": override.get("enabled", True)})
            indices[identifier] = len(rules) - 1
    return rules


def _keys(value, allowed, required=()):
    if not isinstance(value, dict):
        raise ValueError("expected a TOML table")
    unknown = value.keys() - allowed
    missing = set(required) - value.keys()
    if unknown or missing:
        raise ValueError(f"unknown keys {sorted(unknown)}; missing keys {sorted(missing)}")


def _validate_clause(clause):
    if not isinstance(clause, list) or len(clause) not in (2, 3):
        raise ValueError(f"invalid clause: {clause!r}")
    column, operation = clause[:2]
    if not isinstance(column, str) or not column:
        raise ValueError("clause column must be a nonempty string")
    if operation == "present" and len(clause) == 2:
        return
    if operation == "in" and len(clause) == 3 and isinstance(clause[2], list) and all(
        isinstance(value, str) for value in clause[2]
    ):
        return
    if operation in {"date_gt", "date_lt"} and len(clause) == 3 and isinstance(clause[2], str):
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", clause[2]):
            raise ValueError(f"{operation} requires a YYYY-MM-DD date")
        try:
            datetime.strptime(clause[2], "%Y-%m-%d")
        except ValueError as error:
            raise ValueError(f"invalid {operation} date") from error
        return
    if operation in {"abs_diff_le", "angle_diff_le"} and len(clause) == 3:
        comparison = clause[2]
        valid_tolerance = (
            isinstance(comparison, list)
            and len(comparison) == 2
            and isinstance(comparison[0], str)
            and bool(comparison[0])
            and type(comparison[1]) in (int, float)
            and math.isfinite(float(comparison[1]))
            and comparison[1] >= 0
        )
        if valid_tolerance and (
            operation == "abs_diff_le" or comparison[1] <= 180
        ):
            return
    if (operation in ("eq", "gt", "ge", "lt", "le") and len(clause) == 3
            and type(clause[2]) in (int, float) and math.isfinite(float(clause[2]))):
        return
    raise ValueError(f"invalid filter operation or operand: {clause!r}")


def read_recipe(path):
    """Load and validate a SWOT recipe without writing files."""
    path = Path(path).resolve()
    with path.open("rb") as stream:
        recipe = tomllib.load(stream)
    _keys(
        recipe,
        {
            "reference", "reference_variable", "output_dir", "production_date",
            "version", "compile", "catalogues",
        },
        {"reference", "output_dir", "production_date", "version", "catalogues"},
    )
    if recipe["reference"] not in {"swot", "scat"}:
        raise ValueError(f"unsupported reference: {recipe['reference']!r}; supported: swot, scat")
    if recipe["reference"] == "swot":
        recipe.setdefault("reference_variable", "swh")
        if recipe["reference_variable"] != "swh":
            raise ValueError("SWOT WV supports only reference_variable = 'swh'")
    else:
        if "reference_variable" not in recipe:
            raise ValueError(
                "HSCAT recipes require reference_variable = "
                "'windspeed' or 'winddirection'"
            )
        if (not isinstance(recipe["reference_variable"], str)
                or recipe["reference_variable"] not in {"windspeed", "winddirection"}):
            raise ValueError("HSCAT reference_variable must be 'windspeed' or 'winddirection'")
    date = recipe["production_date"]
    if not isinstance(date, str) or not re.fullmatch(r"[0-9]{8}", date):
        raise ValueError("production_date must use YYYYMMDD")
    try:
        datetime.strptime(date, "%Y%m%d")
    except ValueError as error:
        raise ValueError("invalid production_date") from error
    if (not isinstance(recipe["version"], str)
            or not re.fullmatch(r"[0-9]+\.[0-9]+", recipe["version"])):
        raise ValueError("version must use X.Y")
    if not isinstance(recipe.get("compile", False), bool):
        raise ValueError("compile must be boolean")
    if not isinstance(recipe["output_dir"], str):
        raise ValueError("output_dir must be a path")
    recipe.setdefault("compile", False)
    recipe["output_dir"] = (path.parent / recipe["output_dir"]).resolve()
    if any(char in str(recipe["output_dir"]) for char in ("|", "\n", "\r")):
        raise ValueError("unsafe output directory for LaTeX report")
    if recipe["output_dir"].exists():
        raise FileExistsError(f"output_dir already exists: {recipe['output_dir']}")
    if not isinstance(recipe["catalogues"], list) or not recipe["catalogues"]:
        raise ValueError("catalogues must be a nonempty list")
    missions = set()
    for catalogue in recipe["catalogues"]:
        _keys(catalogue, {"mission", "path", "rules"}, {"mission", "path"})
        mission = catalogue["mission"]
        if not isinstance(mission, str) or not re.fullmatch(r"S1[A-D]", mission):
            raise ValueError(f"invalid mission: {mission!r}")
        if mission in missions:
            raise ValueError(f"duplicate mission: {mission}")
        missions.add(mission)
        if not isinstance(catalogue["path"], str):
            raise ValueError("catalogue path must be a string")
        catalogue["path"] = (path.parent / catalogue["path"]).resolve()
        if any(char in str(catalogue["path"]) for char in ("|", "\n", "\r")):
            raise ValueError(f"unsafe catalogue path for LaTeX report: {catalogue['path']}")
        if not catalogue["path"].is_file():
            raise FileNotFoundError(f"missing catalogue: {catalogue['path']}")
        if recipe["reference"] == "swot":
            match = re.match(r"^(S1[A-D])_coaligned_catalogue_(WV)_", catalogue["path"].name)
        else:
            match = re.match(
                r"^(S1[A-D])_coaligned_catalogue_WV_\d{8}_\d{8}_\d{8}_"
                r"(SV|DV|SH|DH)_KNMI-HSCAT-HY2-25km_"
                r"\d+\.\d+\.parquet$",
                catalogue["path"].name,
            )
        if match is None or match.group(1) != mission:
            raise ValueError(
                "mission/filename conflict or unsupported HSCAT catalogue: "
                f"{catalogue['path'].name}"
            )
        catalogue.setdefault("rules", [])
        if not isinstance(catalogue["rules"], list):
            raise ValueError("rules must be a list")
        ids = set()
        for rule in catalogue["rules"]:
            _keys(rule, {"id", "name", "clauses", "enabled"}, {"id"})
            if not isinstance(rule["id"], str) or not rule["id"] or rule["id"] in ids:
                raise ValueError(f"duplicate or invalid rule id: {rule['id']!r}")
            ids.add(rule["id"])
            if "name" in rule and (not isinstance(rule["name"], str) or not rule["name"]):
                raise ValueError("rule name must be nonempty")
            if "enabled" in rule and not isinstance(rule["enabled"], bool):
                raise ValueError("enabled must be boolean")
            if "clauses" in rule:
                if not isinstance(rule["clauses"], list) or not rule["clauses"]:
                    raise ValueError("clauses must be nonempty")
                for clause in rule["clauses"]:
                    _validate_clause(clause)
    return recipe


def clause_mask(frame, clause):
    """Evaluate one validated numeric, membership or numeric-presence clause."""
    _validate_clause(clause)
    column, operation = clause[:2]
    if column not in frame:
        raise ValueError(f"missing filter column: {column}")
    if operation == "in":
        return frame[column].astype("string").isin(clause[2]).fillna(False)
    if operation in {"date_gt", "date_lt"}:
        timestamp = pd.to_datetime(frame[column], utc=True, errors="coerce")
        boundary = pd.Timestamp(clause[2], tz="UTC")
        comparator = timestamp.gt if operation == "date_gt" else timestamp.lt
        return comparator(boundary).fillna(False)
    if operation in {"abs_diff_le", "angle_diff_le"}:
        comparison_column, tolerance = clause[2]
        if comparison_column not in frame:
            raise ValueError(f"missing filter column: {comparison_column}")
        left = pd.to_numeric(frame[column], errors="coerce")
        right = pd.to_numeric(frame[comparison_column], errors="coerce")
        difference = (left - right).abs()
        if operation == "angle_diff_le":
            difference = difference.mod(360)
            difference = difference.where(difference <= 180, 360 - difference)
        return difference.le(tolerance).fillna(False)
    values = pd.to_numeric(frame[column], errors="coerce")
    if operation == "present":
        return values.notna()
    operations = {"eq": values.eq, "gt": values.gt, "ge": values.ge,
                  "lt": values.lt, "le": values.le}
    return operations[operation](clause[2]).fillna(False)


def curate_frame(frame, rules):
    """Filter raw source columns cumulatively and retain original row indices."""
    selected = frame.copy()
    steps = []
    for rule in rules:
        before = len(selected)
        if rule.get("enabled", True):
            mask = pd.Series(True, index=selected.index)
            for clause in rule["clauses"]:
                mask &= clause_mask(selected, clause)
            selected = selected.loc[mask].copy()
            status = "applied"
        else:
            status = "disabled"
        steps.append({"id": rule["id"], "name": rule["name"], "status": status,
                      "clauses": rule["clauses"], "before": before,
                      "remaining": len(selected), "removed": before - len(selected)})
    return selected, steps


def curated_filename(mission, mode, production_date, variable, version):
    """Name the supported SWOT WV Curated Parquet."""
    if not isinstance(mission, str) or not re.fullmatch(r"S1[A-D]", mission):
        raise ValueError("invalid mission")
    if mode != "WV":
        raise ValueError("only WV mode is supported")
    if variable != "swh":
        raise ValueError("only swh variable is supported")
    if not isinstance(production_date, str) or not re.fullmatch(r"[0-9]{8}", production_date):
        raise ValueError("production_date must use YYYYMMDD")
    try:
        datetime.strptime(production_date, "%Y%m%d")
    except ValueError as error:
        raise ValueError("invalid production_date") from error
    if not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+", version):
        raise ValueError("version must use X.Y")
    return (
        f"{mission}_curated_coaligned_dataset_{mode}_"
        f"{production_date}_{variable}_{version}.parquet"
    )


def write_curated(source_path, target_path, rules, mission):
    """Save selected original Arrow rows, fields, metadata and row provenance."""
    source_path, target_path = Path(source_path), Path(target_path)
    if not isinstance(mission, str) or not re.fullmatch(r"S1[A-D]", mission):
        raise ValueError(f"invalid mission: {mission}")
    source = pq.read_table(source_path)
    if {"_curation_mission", "_curation_row"} & set(source.column_names):
        raise ValueError("source contains reserved curation fields")
    kept, steps = curate_frame(source.to_pandas().reset_index(drop=True), rules)
    ordinals = kept.index.to_numpy(dtype="int64")
    table = source.take(pa.array(ordinals, type=pa.int64()))
    table = table.append_column(
        "_curation_mission", pa.array([mission] * len(kept), type=pa.string())
    )
    table = table.append_column("_curation_row", pa.array(ordinals, type=pa.int64()))
    target_path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, target_path)
    return {
        "path": target_path, "input_rows": len(source),
        "curated_rows": len(kept), "steps": steps,
    }


def merge_curated(paths_by_mission, merged_path, normalize_timestamp_units=False):
    """Concatenate common Curated columns in mission order.

    SCAT source catalogues can encode the same instant with different Arrow
    timestamp units. Normalize those fields to the finest observed unit only
    when the product adapter explicitly requests it.
    """
    if not paths_by_mission:
        raise ValueError("no Curated Parquets to merge")
    tables = [pq.read_table(paths_by_mission[mission]) for mission in sorted(paths_by_mission)]
    common = [
        field.name for field in tables[0].schema
        if all(field.name in table.column_names for table in tables[1:])
    ]
    fields = []
    timestamp_rank = {"s": 0, "ms": 1, "us": 2, "ns": 3}
    for name in common:
        source_fields = [table.schema.field(name) for table in tables]
        types = [field.type for field in source_fields]
        field = source_fields[0]
        if any(value != types[0] for value in types[1:]):
            compatible_timestamps = (
                normalize_timestamp_units
                and all(pa.types.is_timestamp(value) for value in types)
                and len({value.tz for value in types}) == 1
            )
            if not compatible_timestamps:
                raise ValueError(f"Curated schema has incompatible column types: {name}")
            unit = max((value.unit for value in types), key=lambda value: timestamp_rank[value])
            field = pa.field(name, pa.timestamp(unit, tz=types[0].tz), nullable=field.nullable)
        fields.append(field)
    schema = pa.schema(fields, metadata=tables[0].schema.metadata)
    projected = []
    for table in tables:
        projected.append(table.select(common).cast(schema))
    merged = pa.concat_tables(projected)
    merged_path = Path(merged_path)
    merged_path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(merged, merged_path)
    return merged_path
