"""Per-catalogue SWOT quality curation and persisted Arrow stages."""

from __future__ import annotations

import math
import re
import tomllib
from datetime import datetime
from pathlib import Path
from copy import deepcopy

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from .quality_disclaimers import flagged_safe_mask


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

# WV x altimeter coaligned catalogue filenames carry full timestamps
# (YYYYMMDDTHHMMSS) in the start/stop/production slots and a CCI or CMEMS
# product token. Example:
#   S1A_..._WV_20191001T005910_20260629T193043_20261018T022921_SV_CMEMS-ALTI-L3-VAVH_1.0.parquet
S1_WV_ALTI_NAME_RE = re.compile(
    r"(S1[A-D])_coaligned_catalogue_(WV)_(\d{8}T\d{6})_(\d{8}T\d{6})_(\d{8}T\d{6})_"
    r"(SV|DV|SH|DH)_([A-Za-z0-9][A-Za-z0-9._-]*)_(\d+\.\d+)\.parquet"
)

# Reference filenames identify the altimeter that produced the row:
# * CMEMS ALTI-L3-VAVH : ``global_vavh_l3_rt_<token>_<start>_<stop>_<gen>.nc``
#   (cfo = CFOSAT, h2b = HaiYang-2B, c2 = CryoSat-2, al = SARAL, j3 = Jason-3,
#   h2c = HaiYang-2C, s3a/s3b/s6a/swon, ...)
# * CCI SeaState L2P   : ``SEASTATE-L2P-SWH-<satellite>-<timestamp>-fv01.nc``
# The ``not_in_window_for`` clause takes plain-English prefixes of these names
# (e.g. ``global_vavh_l3_rt_cfo_`` or ``SEASTATE-L2P-SWH-CFOSAT-``) as regexes.


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
    validate_rule_order(rules)
    return rules


def validate_rule_order(rules):
    """Group statistics must see all and only the preceding eligible rows."""
    active = [rule for rule in rules if rule.get("enabled", True)]
    for index, rule in enumerate(active):
        for clause in rule["clauses"]:
            _validate_clause(clause)
            if clause[1] == "imagette_circstd_le" and (
                len(rule["clauses"]) != 1 or index != len(active) - 1
            ):
                raise ValueError("imagette_circstd_le must be the sole clause of the "
                                 "last enabled rule")


def validate_filter_columns(columns, rules):
    """Check active source operands even when earlier rules select no rows."""
    for rule in rules:
        if not rule.get("enabled", True):
            continue
        for clause in rule["clauses"]:
            if clause[1] == "qd_flagged":
                required = list(clause[2])
            else:
                required = [clause[0]]
                if clause[1] in {"abs_diff_le", "angle_diff_le", "rel_diff_le",
                                 "imagette_circstd_le", "not_in_window_for"}:
                    required.append(clause[2][0])
            for name in required:
                if name not in columns:
                    raise ValueError(f"missing filter column: {name}")


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
    if operation == "qd_flagged" and len(clause) == 3 and isinstance(clause[2], list) and all(
        isinstance(value, str) and value for value in clause[2]
    ):
        return
    if operation == "in" and len(clause) == 3 and isinstance(clause[2], list) and clause[2]:
        values = clause[2]
        if all(isinstance(value, str) for value in values):
            return
        if all(type(value) in (int, float) and math.isfinite(float(value)) for value in values):
            return
    if operation in {"date_gt", "date_lt"} and len(clause) == 3 and isinstance(clause[2], str):
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", clause[2]):
            raise ValueError(f"{operation} requires a YYYY-MM-DD date")
        try:
            datetime.strptime(clause[2], "%Y-%m-%d")
        except ValueError as error:
            raise ValueError(f"invalid {operation} date") from error
        return
    if operation in {"abs_diff_le", "angle_diff_le", "rel_diff_le",
                     "imagette_circstd_le"} and len(clause) == 3:
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
            operation != "angle_diff_le" or comparison[1] <= 180
        ):
            return
    if operation == "not_in_window_for" and len(clause) == 3:
        window = clause[2]
        if (isinstance(window, list) and len(window) == 4
                and isinstance(window[0], str) and bool(window[0])
                and isinstance(window[1], list)
                and window[1]
                and all(isinstance(pattern, str) and pattern for pattern in window[1])
                and all(
                    isinstance(bound, str) and _is_valid_yyyymmdd(bound)
                    for bound in (window[2], window[3])
                )
                and window[2] < window[3]):
            for pattern in window[1]:
                try:
                    re.compile(pattern)
                except re.error:
                    raise ValueError(f"invalid filter operation or operand: {clause!r}")
            return
    if (operation in ("eq", "gt", "ge", "lt", "le") and len(clause) == 3
            and type(clause[2]) in (int, float) and math.isfinite(float(clause[2]))):
        return
    raise ValueError(f"invalid filter operation or operand: {clause!r}")


def _is_valid_yyyymmdd(value):
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return False
    return True


def read_recipe(path):
    """Load and validate a SWOT recipe without writing files."""
    path = Path(path).resolve()
    with path.open("rb") as stream:
        recipe = tomllib.load(stream)
    _keys(
        recipe,
        {
            "reference", "reference_variable", "output_dir", "production_date",
            "version", "compile", "report", "catalogues", "quality_disclaimers",
            "rename_columns",
        },
        {"reference", "output_dir", "production_date", "version", "catalogues"},
    )
    if "report" in recipe and not isinstance(recipe["report"], bool):
        raise ValueError("report must be boolean")
    if "quality_disclaimers" in recipe and not isinstance(
        recipe["quality_disclaimers"], (str, dict)
    ):
        raise ValueError("quality_disclaimers must be a path or a mapping")
    if "rename_columns" in recipe:
        rename = recipe["rename_columns"]
        if (not isinstance(rename, dict)
                or not all(isinstance(k, str) and isinstance(v, str)
                           and k and v for k, v in rename.items())):
            raise ValueError("rename_columns must map nonempty strings to nonempty strings")
    if recipe["reference"] not in {"swot", "scat", "altimeter"}:
        raise ValueError(
            f"unsupported reference: {recipe['reference']!r}; supported: swot, scat, altimeter"
        )
    if recipe["reference"] == "swot":
        recipe.setdefault("reference_variable", "swh")
        if recipe["reference_variable"] != "swh":
            raise ValueError("SWOT WV supports only reference_variable = 'swh'")
    elif recipe["reference"] == "scat":
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
        if recipe["reference"] != "altimeter" and mission in missions:
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
        elif recipe["reference"] == "scat":
            match = re.match(
                r"^(S1[A-D])_coaligned_catalogue_WV_\d{8}_\d{8}_\d{8}_"
                r"(SV|DV|SH|DH)_KNMI-HSCAT-HY2-25km_"
                r"\d+\.\d+\.parquet$",
                catalogue["path"].name,
            )
        else:
            match = S1_WV_ALTI_NAME_RE.match(catalogue["path"].name)
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
    if isinstance(recipe.get("quality_disclaimers"), str):
        qd_path = (path.parent / recipe["quality_disclaimers"]).resolve()
        if any(char in str(qd_path) for char in ("|", "\n", "\r")):
            raise ValueError("unsafe quality-disclaimer path for LaTeX report")
        recipe["quality_disclaimers"] = qd_path
    return recipe


def clause_mask(frame, clause, context=None):
    """Evaluate one validated numeric, membership or numeric-presence clause."""
    _validate_clause(clause)
    column, operation = clause[:2]
    if operation == "qd_flagged":
        flagged = (context or {}).get("qd_flagged")
        if flagged is None:
            raise ValueError("qd_flagged rule requires the quality_disclaimers config")
        return ~flagged_safe_mask(frame, clause[2], flagged)
    if column not in frame:
        raise ValueError(f"missing filter column: {column}")
    if operation == "in":
        values = clause[2]
        if all(type(value) in (int, float) and not isinstance(value, bool) for value in values):
            numeric = pd.to_numeric(frame[column], errors="coerce")
            return numeric.isin([float(value) for value in values]).fillna(False)
        return frame[column].astype("string").isin(values).fillna(False)
    if operation in {"date_gt", "date_lt"}:
        timestamp = pd.to_datetime(frame[column], utc=True, errors="coerce")
        boundary = pd.Timestamp(clause[2], tz="UTC")
        comparator = timestamp.gt if operation == "date_gt" else timestamp.lt
        return comparator(boundary).fillna(False)
    if operation in {"abs_diff_le", "angle_diff_le", "rel_diff_le"}:
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
        if operation == "rel_diff_le":
            # relative difference against the reference column; the reference
            # must be strictly positive (an Hs reference), NaN -> rejected
            ratio = difference / right.where(right > 0)
            return (ratio <= float(tolerance) + 1e-12).fillna(False)
        return difference.le(tolerance).fillna(False)
    if operation == "not_in_window_for":
        # Reject rows whose reference ``fname`` matches any of the patterns
        # (the altimeter that produced the row) while the row time is in the
        # half-open window [start_date, end_date).
        time_column, patterns, start_date, end_date = clause[2]
        if time_column not in frame:
            raise ValueError(f"missing filter column: {time_column}")
        timestamps = pd.to_datetime(frame[time_column], utc=True, errors="coerce")
        start = pd.Timestamp(start_date, tz="UTC")
        end = pd.Timestamp(end_date, tz="UTC")
        in_window = timestamps.ge(start) & timestamps.lt(end)
        names = frame[column].astype("string").fillna("")
        name_match = pd.Series(False, index=frame.index)
        for pattern in patterns:
            name_match |= names.str.contains(pattern, regex=True, na=False)
        return ~(in_window & name_match)
    values = pd.to_numeric(frame[column], errors="coerce")
    if operation == "present":
        return values.notna()
    operations = {"eq": values.eq, "gt": values.gt, "ge": values.ge,
                  "lt": values.lt, "le": values.le}
    return operations[operation](clause[2]).fillna(False)


def _imagette_circstd_mask(frame, clause):
    column, _, (identity, threshold) = clause
    keys = frame[identity].astype("string").str.rsplit("/", n=1).str[-1]
    missing = (keys.isna() | keys.str.strip().eq("").fillna(False)).astype(bool)
    valid_keys = keys.str.fullmatch(r"[^/:\s]+\.SAFE:WV_[0-9]{3}").fillna(False).astype(bool)
    malformed = ~missing & ~valid_keys
    if malformed.any():
        examples = frame.loc[malformed, identity].head(3).tolist()
        raise ValueError(f"{identity}: malformed imagette identity; "
                         f"expected final basename SAFE:WV_###; examples: {examples!r}")
    values = pd.to_numeric(frame[column], errors="coerce").to_numpy(dtype=float, na_value=np.nan)
    identified = ~missing.to_numpy(dtype=bool)
    finite = np.isfinite(values) & identified
    normalized = np.remainder(values[finite], 360)
    angles = np.deg2rad(normalized)
    valid = pd.DataFrame({"key": keys.to_numpy()[finite],
                          "sin": np.sin(angles), "cos": np.cos(angles),
                          "direction": normalized})
    grouped = valid.groupby("key", sort=False)
    counts = grouped.size()
    means = grouped[["sin", "cos"]].mean()
    resultant = np.hypot(means["sin"], means["cos"]).clip(0, 1)
    # Resolve floating-point cancellation at R=0; do not cap STD at 180 degrees.
    resultant = resultant.where(resultant > 1e-15, 0)
    with np.errstate(divide="ignore"):
        spread = np.rad2deg(np.sqrt(-2 * np.log(resultant)))
    spread = spread.where(grouped["direction"].nunique() != 1, 0.0)
    rejected = spread.index[spread > threshold + 1e-10]
    mask = pd.Series(finite, index=frame.index) & ~keys.isin(rejected)
    return mask, {
        "evaluated_multi_matchup_groups": int((counts >= 2).sum()),
        "rejected_imagettes": len(rejected),
        "singleton_groups": int((counts == 1).sum()),
        "all_invalid_groups": int(keys.loc[~missing].nunique() - len(counts)),
        "missing_identity_rows": int(missing.sum()),
        "invalid_direction_rows": int((~np.isfinite(values) & identified).sum()),
        "spread_removed_rows": int((finite & keys.isin(rejected)).sum()),
    }


def curate_frame(frame, rules, context=None):
    """Filter raw source columns cumulatively and retain original row indices."""
    validate_rule_order(rules)
    validate_filter_columns(frame.columns, rules)
    selected = frame.copy()
    steps = []
    for rule in rules:
        before = len(selected)
        diagnostics = {}
        if rule.get("enabled", True):
            mask = pd.Series(True, index=selected.index)
            for clause in rule["clauses"]:
                if clause[1] == "imagette_circstd_le":
                    mask, diagnostics = _imagette_circstd_mask(selected, clause)
                else:
                    mask &= clause_mask(selected, clause, context)
            selected = selected.loc[mask].copy()
            status = "applied"
        else:
            status = "disabled"
        steps.append({"id": rule["id"], "name": rule["name"], "status": status,
                      "clauses": rule["clauses"], "before": before,
                      "remaining": len(selected), "removed": before - len(selected),
                      **diagnostics})
    return selected, steps


def _parse_catalogue_date(value):
    """Return the YYYYMMDD part of a catalogue date (YYYYMMDD or YYYYMMDDTHHMMSS)."""
    if len(value) == 8:
        parsed = datetime.strptime(value, "%Y%m%d")
    elif len(value) == 15 and value[8] == "T":
        parsed = datetime.strptime(value, "%Y%m%dT%H%M%S")
    else:
        raise ValueError(f"invalid catalogue date: {value}")
    return parsed.date().strftime("%Y%m%d")


def curated_sar_bounds(frame):
    """Return the (start, stop) YYYYMMDD SAR dates actually present in a curated frame.

    Per the SOBA naming convention, the curated ``startdate``/``stopdate`` are the
    first and last SAR starting dates *within the curated dataset*, so these are
    derived from the real (post-filter) ``sar_time`` values rather than the source
    catalogue's filename coverage.
    """
    if "sar_time" not in frame.columns:
        raise ValueError("cannot derive curated filename dates: missing sar_time column")
    times = pd.to_datetime(frame["sar_time"], utc=True, errors="coerce").dropna()
    if times.empty:
        raise ValueError("cannot derive curated filename dates: no valid sar_time")
    return times.min().strftime("%Y%m%d"), times.max().strftime("%Y%m%d")


def _curated_filename_parts(source_path):
    """Validate the source catalogue filename and return its identity groups."""
    name = Path(source_path).name
    match = S1_WV_ALTI_NAME_RE.match(name) or re.fullmatch(
        r"(S1[A-D])_coaligned_catalogue_(WV)_(\d{8})_(\d{8})_(\d{8})_"
        r"(SV|DV|SH|DH)_([A-Za-z0-9][A-Za-z0-9.-]*)_(\d+\.\d+)\.parquet",
        name,
    )
    if match is None:
        raise ValueError(f"invalid WV catalogue filename: {name}")
    return match.groups()


def _validate_curated_run(production_date, version):
    if not isinstance(production_date, str) or not re.fullmatch(r"[0-9]{8}", production_date):
        raise ValueError("production_date must use YYYYMMDD")
    try:
        datetime.strptime(production_date, "%Y%m%d")
    except ValueError as error:
        raise ValueError("invalid production_date") from error
    if not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+", version):
        raise ValueError("version must use X.Y")


def curated_filename(source_path, production_date, version, override_dates=None):
    """Build the Curated filename, retaining the source product identity.

    ``override_dates`` (a ``(start, stop)`` YYYYMMDD pair) overrides the source
    catalogue's filename coverage; when omitted the source coverage is used.
    """
    mission, mode, start, stop, source_date, polarization, product, _ = (
        _curated_filename_parts(source_path)
    )
    _validate_curated_run(production_date, version)
    if override_dates is None:
        start_d = _parse_catalogue_date(start)
        stop_d = _parse_catalogue_date(stop)
    else:
        start_d, stop_d = _parse_catalogue_date(override_dates[0]), _parse_catalogue_date(
            override_dates[1]
        )
    _parse_catalogue_date(source_date)
    if start_d > stop_d:
        raise ValueError("catalogue startdate must not exceed stopdate")
    return (
        f"{mission}_curated_{mode}_{start_d}_{stop_d}_{production_date}_"
        f"{polarization}_{product}_{version}.parquet"
    )


def write_curated(source_path, target_path, rules, mission, context=None):
    """Save selected original Arrow rows, fields, metadata and row provenance."""
    source_path, target_path = Path(source_path), Path(target_path)
    if not isinstance(mission, str) or not re.fullmatch(r"S1[A-D]", mission):
        raise ValueError(f"invalid mission: {mission}")
    source = pq.read_table(source_path)
    if {"_curation_mission", "_curation_row"} & set(source.column_names):
        raise ValueError("source contains reserved curation fields")
    kept, steps = curate_frame(source.to_pandas().reset_index(drop=True), rules, context)
    ordinals = kept.index.to_numpy(dtype="int64")
    table = source.take(pa.array(ordinals, type=pa.int64()))
    rename = (context or {}).get("rename_columns")
    if rename:
        table = table.rename_columns(
            [rename.get(name, name) for name in table.column_names]
        )
    table = table.append_column(
        "_curation_mission", pa.array([mission] * len(kept), type=pa.string())
    )
    table = table.append_column("_curation_row", pa.array(ordinals, type=pa.int64()))
    context = context or {}
    meta = dict(table.schema.metadata or {})
    if context.get("recipe_path"):
        meta[b"recipe_path"] = str(Path(context["recipe_path"]).resolve()).encode()
    if context.get("dataset_version"):
        meta[b"dataset_version"] = str(context["dataset_version"]).encode()
    if context.get("production_date"):
        meta[b"production_date"] = str(context["production_date"]).encode()
    table = table.replace_schema_metadata(meta)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, target_path)
    sar_bounds = curated_sar_bounds(kept) if "sar_time" in kept.columns else None
    return {
        "path": target_path, "input_rows": len(source),
        "curated_rows": len(kept), "steps": steps, "sar_bounds": sar_bounds,
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
