"""Extract good-only ESA CCI v5 WV predictions for ordered TEST keys."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict

from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import re
import tomllib

import netCDF4
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq


_FILENAME = re.compile(
    r"ESACCI-SEASTATE-L2P-ISSP-SENTINEL-1([ABCD])_WV_IFR-"
    r"([0-9]{8}T[0-9]{6})-fv[0-9]+\.nc$"
)
_SAFE = re.compile(r"^(S1[ABCD])_WV_OCN__[^_]+_([0-9]{8}T[0-9]{6})_")
_COLUMNS = ["primary_key", "sar_safe_ocn", "sar_time", "sar_lat", "sar_lon"]
_EPOCH = datetime(1990, 1, 1, tzinfo=timezone.utc)


def _read_recipe(path):
    path = Path(path).resolve()
    with path.open("rb") as stream:
        config = tomllib.load(stream)
    required = {
        "test",
        "archive_root",
        "output_dir",
        "production_date",
        "version",
        "max_time_delta_seconds",
        "max_distance_km",
    }
    if config.keys() != required:
        raise ValueError(
            f"recipe keys: missing {sorted(required - config.keys())}; "
            f"unknown {sorted(config.keys() - required)}"
        )
    for key in ("test", "archive_root", "output_dir"):
        if not isinstance(config[key], str) or not config[key].strip():
            raise ValueError(f"{key} must be a nonempty path")
        config[key] = (path.parent / config[key]).resolve()
    if not config["test"].is_file() or not config["archive_root"].is_dir():
        raise ValueError("TEST file or archive root missing")
    if config["output_dir"].exists():
        raise FileExistsError("output_dir already exists")
    date = config["production_date"]
    if not isinstance(date, str) or not re.fullmatch(r"[0-9]{8}", date):
        raise ValueError("production_date must be YYYYMMDD")
    try:
        datetime.strptime(date, "%Y%m%d")
    except ValueError as exc:
        raise ValueError("production_date must be a valid YYYYMMDD date") from exc
    if not isinstance(config["version"], str) or not re.fullmatch(
        r"[0-9]+\.[0-9]+", config["version"]
    ):
        raise ValueError("version must use X.Y")
    for key in ("max_time_delta_seconds", "max_distance_km"):
        value = config[key]
        if type(value) not in (float, int) or not math.isfinite(value) or value <= 0:
            raise ValueError(f"{key} must be a positive finite number")
    return config


def _source_index(root, mission_years):
    """Inventory filenames only, across opaque buckets and adjacent years."""
    index = defaultdict(list)
    base = root / "products/v5/data/satellite/sar"
    for mission, year in sorted(mission_years):
        tree = base / f"sentinel-1{mission[-1].lower()}" / "l2p" / str(year)
        for path in tree.rglob("*.nc"):
            match = _FILENAME.fullmatch(path.name)
            if match and mission == "S1" + match[1]:
                index[(mission, match[2])].append(path)
    return index


def _scalar(value):
    if np.ma.is_masked(value):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError, OverflowError):
        return None


def _distance_km(lat1, lon1, lat2, lon2):
    a, b = math.radians(lat1), math.radians(lat2)
    dlat = b - a
    dlon = math.radians((lon2 - lon1 + 180) % 360 - 180)
    h = math.sin(dlat / 2) ** 2 + math.cos(a) * math.cos(b) * math.sin(dlon / 2) ** 2
    return 6371.0088 * 2 * math.asin(min(1, math.sqrt(h)))


def _observation(row, ds, max_seconds, max_km):
    variables = ds.variables
    for field in ("time", "lat", "lon", "swh", "swh_quality_level", "swh_rejection_flags"):
        if field not in variables:
            return None, {"reason": "invalid_source_schema"}
    if (
        str(getattr(variables["time"], "units", "")).strip().lower()
        != "nanoseconds since 1990-01-01"
    ):
        return None, {"reason": "unsupported_time_units"}
    time = row["sar_time"]
    if isinstance(time, str):
        time = datetime.fromisoformat(time.replace("Z", "+00:00"))
    if time.tzinfo is None:
        time = time.replace(tzinfo=timezone.utc)
    else:
        time = time.astimezone(timezone.utc)
    lat, lon = _scalar(row["sar_lat"]), _scalar(row["sar_lon"])
    if lat is None or lon is None or not -90 <= lat <= 90:
        return None, {"reason": "invalid_test_position"}
    matches = []
    times = variables["time"][:]
    lats = variables["lat"][:]
    lons = variables["lon"][:]
    for i in range(len(times)):
        ns, y, x = _scalar(times[i]), _scalar(lats[i]), _scalar(lons[i])
        if ns is None or y is None or x is None or not -90 <= y <= 90:
            continue
        try:
            seconds = abs((_EPOCH + timedelta(microseconds=ns / 1000) - time).total_seconds())
        except (OverflowError, ValueError):
            continue
        if seconds <= max_seconds:
            distance = _distance_km(lat, lon, y, x)
            if distance <= max_km:
                matches.append((i, seconds, distance))
    if len(matches) != 1:
        return None, {"reason": "missing_observation" if not matches else "ambiguous_observation"}
    i, seconds, distance = matches[0]
    quality = _scalar(variables["swh_quality_level"][i])
    flags = _scalar(variables["swh_rejection_flags"][i])
    value = _scalar(variables["swh"][i])
    details = {
        "observation_index": i,
        "time_delta_seconds": seconds,
        "distance_km": distance,
        "quality": quality,
        "rejection_flags": flags,
    }
    if quality != 3:
        return None, {**details, "reason": "bad_quality"}
    if flags != 0:
        return None, {**details, "reason": "rejection_flags"}
    if value is None:
        return None, {**details, "reason": "invalid_swh"}
    return value, {**details, "reason": "good"}


def produce_challenger(recipe_path):
    """Write ordered nullable predictions and a path-portable audit to a fresh directory."""
    cfg = _read_recipe(recipe_path)
    table = pq.read_table(cfg["test"], columns=_COLUMNS)
    rows = table.to_pylist()
    keys = [row["primary_key"] for row in rows]
    if any(not isinstance(key, str) or not key for key in keys) or len(keys) != len(set(keys)):
        raise ValueError("TEST primary_key must be nonempty and unique")
    parsed = []
    mission_years = set()
    for row in rows:
        match = _SAFE.match(row["sar_safe_ocn"] or "")
        if match is None:
            raise ValueError("TEST sar_safe_ocn must name an S1 WV OCN SAFE with acquisition start")
        mission, start = match.groups()
        year = int(start[:4])
        parsed.append((mission, start, year))
        mission_years.update((mission, y) for y in (year - 1, year, year + 1))
    index = _source_index(cfg["archive_root"], mission_years)
    values, audit = [None] * len(rows), [None] * len(rows)
    base = cfg["archive_root"]
    by_file = defaultdict(list)
    for i, (row, (mission, start, year)) in enumerate(zip(rows, parsed)):
        files = index.get((mission, start), [])
        detail = {"primary_key": row["primary_key"], "mission": mission, "year": year}
        if len(files) == 1:
            file = files[0]
            detail["source_file"] = file.relative_to(base).as_posix()
            by_file[file].append((i, row, detail))
        elif files:
            audit[i] = {**detail, "reason": "ambiguous_source_file"}
        else:
            tree = (
                base
                / "products/v5/data/satellite/sar"
                / f"sentinel-1{mission[-1].lower()}"
                / "l2p"
                / str(year)
            )
            audit[i] = {
                **detail,
                "reason": "missing_source_file" if tree.is_dir() else "missing_archive_year",
            }
    for file, entries in by_file.items():
        with netCDF4.Dataset(file) as ds:
            for i, row, detail in entries:
                values[i], result = _observation(
                    row, ds, cfg["max_time_delta_seconds"], cfg["max_distance_km"]
                )
                audit[i] = {**detail, **result}
    output = cfg["output_dir"]
    output.mkdir(parents=True, exist_ok=False)
    name = f"S1_challenger_ESACCI-SEASTATE_WV_{cfg['production_date']}_{cfg['version']}.parquet"
    result = pa.table(
        {
            "primary_key": pa.array(keys, type=pa.string()),
            "swh": pa.array(values, type=pa.float64()),
        }
    )
    pq.write_table(result, output / name)
    actual = pq.read_table(output / name)
    if (
        actual.column_names != ["primary_key", "swh"]
        or actual.schema.field("swh").type != pa.float64()
        or not actual.schema.field("swh").nullable
        or actual.column("primary_key").to_pylist() != keys
        or actual.column("swh").to_pylist() != values
    ):
        raise RuntimeError("CHALLENGER read-back failed")
    reasons = Counter(item["reason"] for item in audit)
    groups = defaultdict(Counter)
    for item in audit:
        groups[f"{item['mission']}/{item['year']}"][item["reason"]] += 1
    report = {
        "source_product": "ESA CCI Sea State v5 WV",
        "prediction_units": "m",
        "summary": {
            "total": len(keys),
            "good": reasons["good"],
            "null": len(keys) - reasons["good"],
            "reasons": dict(reasons),
            "by_mission_year": {k: dict(v) for k, v in sorted(groups.items())},
            "coverage": "complete" if reasons["good"] == len(keys) else "partial",
        },
        "rows": audit,
    }
    (output / "audit.json").write_text(json.dumps(report, indent=2) + "\n")
    return output / name


def main(argv=None):
    parser = argparse.ArgumentParser(description="Extract good-only ESA CCI WV CHALLENGER")
    parser.add_argument("--recipe", required=True, type=Path)
    args = parser.parse_args(argv)
    print(produce_challenger(args.recipe).name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
