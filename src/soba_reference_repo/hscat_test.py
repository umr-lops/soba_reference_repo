"""Create HSCAT reference TEST/TARGET datasets."""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from .curation import merge_curated, read_recipe, resolve_rules, write_curated
from .hscat_report import plot_hscat_figures
from .pdf_support import (
    compile_pdf,
    library_version,
    purge_latex_byproducts,
)

SOURCE_COLUMNS = (
    "primary_key",
    "sar_time",
    "sar_lat",
    "sar_lon",
    "sar_incidence_angle",
    "sar_elevation_angle",
    "sar_ground_heading",
    "sar_safe_slc",
    "sar_safe_ocn",
    "ref_lon",
    "ref_lat",
    "ref_param_1",
    "ref_param_2",
    "ref_time",
    "ref_id",
)
VARIABLE_COLUMNS = {
    "windspeed": ("ref_param_2", "scat_windspeed", "m/s"),
    "winddirection": ("ref_param_1", "scat_winddirection", "degrees"),
}
HSCAT_DEFAULT_RULES = (
    {
        "id": "ecmwf_scat_speed_difference",
        "name": "abs(ECMWF wind speed - HSCAT wind speed) <= 2 m/s",
        "clauses": [["ecmwf_wind_speed", "abs_diff_le", ["ref_param_2", 2]]],
        "enabled": True,
    },
)
BASE_TEST_COLUMNS = [
    "primary_key",
    "sar_time",
    "sar_lat",
    "sar_lon",
    "sar_incidence_angle",
    "sar_elevation_angle",
    "sar_ground_heading",
    "sar_safe_slc",
    "sar_safe_ocn",
    "scat_lon",
    "scat_lat",
]
BASE_TARGET_COLUMNS = ["primary_key", "sar_safe_slc", "sar_safe_ocn", "scat_lon", "scat_lat"]
SCAT_METADATA_KEYS = (
    b"source scat",
    b"source ancillary datasets",
    b"library used to produce the parquet",
    b"library version",
    b"creation date",
)


def _attrs(schema):
    for key in (b"pandas", b"utils_meta"):
        try:
            document = json.loads((schema.metadata or {})[key])
        except (KeyError, TypeError, json.JSONDecodeError):
            continue
        attrs = document.get("attributes", document.get("attrs", {}))
        if attrs:
            return attrs
    return {}


def _product(path):
    match = re.match(
        r"^(S1[A-D])_coaligned_catalogue_WV_(\d{8})_(\d{8})_(\d{8})_"
        r"(SV|DV|SH|DH)_KNMI-HSCAT-HY2-25km_(\d+\.\d+)\.parquet$",
        Path(path).name,
    )
    if not match:
        raise ValueError(f"unsupported SCAT catalogue filename: {Path(path).name}")
    return {
        "mission": match.group(1),
        "start": match.group(2),
        "stop": match.group(3),
        "production": match.group(4),
        "polarization": match.group(5),
        "product": "HSCAT",
        "refproduct": "KNMI-HSCAT-HY2-25km",
        "source_version": match.group(6),
    }


def validate_scat_input(path, expected_product):
    """Check the concrete SCAT source contract and its embedded reference metadata."""
    path = Path(path)
    info = _product(path)
    if info["product"] != expected_product:
        raise ValueError(f"{path.name}: expected {expected_product}, got {info['product']}")
    schema = pq.read_schema(path)
    missing = set(SOURCE_COLUMNS) - set(schema.names)
    if missing:
        raise ValueError(f"{path.name}: missing SCAT columns: {sorted(missing)}")
    attrs = _attrs(schema)
    direction_description = str(attrs.get("ref_param_1", ""))
    speed_description = str(attrs.get("ref_param_2", ""))
    if "wind_direction" not in direction_description or "wind_speed" not in speed_description:
        raise ValueError(
            f"{path.name}: reference metadata does not identify wind direction and speed"
        )
    source_ref = str(attrs.get("source ref", ""))
    if not source_ref or "HY-2" not in source_ref:
        raise ValueError(f"{path.name}: source ref metadata does not match HSCAT")
    if info["product"] == "HSCAT":
        float_filter_columns = (
            "ecmwf_wind_speed",
            "ecmwf_wind_dir_360",
            "ref_ice_prob",
        )
        filter_columns = (*float_filter_columns, "ref_flag")
        missing_filters = set(filter_columns) - set(schema.names)
        if missing_filters:
            raise ValueError(
                f"{path.name}: HSCAT recipe filters require columns: "
                f"{sorted(missing_filters)}"
            )
        invalid_filter_types = [
            name
            for name in float_filter_columns
            if not pa.types.is_floating(schema.field(name).type)
        ]
        ref_flag_type = schema.field("ref_flag").type
        if not (pa.types.is_integer(ref_flag_type) or pa.types.is_floating(ref_flag_type)):
            invalid_filter_types.append("ref_flag")
        if invalid_filter_types:
            raise ValueError(
                f"{path.name}: HSCAT recipe filter fields must be numeric: "
                f"{invalid_filter_types}"
            )
    for column in SOURCE_COLUMNS:
        field = schema.field(column)
        if column in {"primary_key", "sar_safe_slc", "sar_safe_ocn", "ref_id"}:
            valid = pa.types.is_string(field.type) or pa.types.is_large_string(field.type)
        elif column in {"sar_time", "ref_time"}:
            valid = pa.types.is_timestamp(field.type)
        else:
            valid = pa.types.is_floating(field.type)
        if not valid:
            raise ValueError(f"{path.name}: unsupported type {field.type} for {column}")
    return info, attrs


def build_scat_frames(frame, variable, duplicate_keys=None):
    """Map curated source fields to aligned SCAT TEST/TARGET tables."""
    if variable not in VARIABLE_COLUMNS:
        raise ValueError(f"unsupported SCAT reference variable: {variable}")
    source_variable, output_variable, _ = VARIABLE_COLUMNS[variable]
    missing = set(SOURCE_COLUMNS) - set(frame.columns)
    if missing:
        raise ValueError(f"Curated SCAT rows lack required fields: {sorted(missing)}")
    data = frame.copy()
    for column in ("sar_safe_slc", "sar_safe_ocn"):
        data[column] = (
            data[column]
            .astype("string")
            .str.rsplit("/", n=1)
            .str[-1]
            .replace({"nan": pd.NA, "None": pd.NA, "<NA>": pd.NA, "": pd.NA})
        )
    data = data.rename(
        columns={
            "ref_lon": "scat_lon",
            "ref_lat": "scat_lat",
            "ref_time": "scat_time",
            source_variable: output_variable,
        }
    )
    test_columns = [*BASE_TEST_COLUMNS, "scat_time", output_variable]
    target_columns = [
        "primary_key",
        "sar_safe_slc",
        "sar_safe_ocn",
        "scat_lon",
        "scat_lat",
        "scat_time",
        output_variable,
    ]
    required = test_columns
    data["sar_time"] = pd.to_datetime(data["sar_time"], utc=True, errors="coerce")
    data["scat_time"] = pd.to_datetime(data["scat_time"], utc=True, errors="coerce")
    numeric = [
        name
        for name in required
        if name not in {"primary_key", "sar_time", "scat_time", "sar_safe_slc", "sar_safe_ocn"}
    ]
    for column in numeric:
        data[column] = pd.to_numeric(data[column], errors="coerce")
    missing = data[required].isna().any(axis=1)
    finite = np.isfinite(data[numeric].to_numpy(dtype=float)).all(axis=1)
    excluded = int((missing | ~finite).sum())
    data = data.loc[~missing & finite].copy()
    keys = data["primary_key"].astype("string")
    if keys.str.strip().eq("").any():
        raise ValueError("SCAT primary_key values must be nonempty")
    duplicate_mask = (
        keys.duplicated(keep=False) if duplicate_keys is None else keys.isin(duplicate_keys)
    )
    duplicate_key_rows_by_mission = {}
    if duplicate_mask.any() and "_curation_mission" in data:
        duplicate_key_rows_by_mission = {
            str(mission): int(count)
            for mission, count in data.loc[duplicate_mask, "_curation_mission"]
            .value_counts()
            .sort_index()
            .items()
        }
    excluded_duplicate_key_rows = int(duplicate_mask.sum())
    data = data.loc[~duplicate_mask].copy()
    keys = keys.loc[~duplicate_mask]
    data["primary_key"] = keys
    for column in numeric:
        data[column] = data[column].astype("float32")
    data["sar_time"] = data["sar_time"].dt.tz_convert(None).dt.floor("s").astype("datetime64[ns]")
    data["scat_time"] = data["scat_time"].dt.tz_convert(None).dt.floor("s").astype("datetime64[ns]")
    test = data[test_columns].reset_index(drop=True)
    target = data[target_columns].reset_index(drop=True)
    return (
        test,
        target,
        {
            "accepted_rows": len(test),
            "excluded_incomplete_rows": excluded,
            "excluded_duplicate_key_rows": excluded_duplicate_key_rows,
            "duplicate_key_rows_by_mission": duplicate_key_rows_by_mission,
            "test_columns": test_columns,
            "target_columns": target_columns,
            "reference_column": output_variable,
        },
    )


def write_scat_pair_chunked(
    source, test_path, target_path, variable, batch_size=65_536, metadata=None
):
    """Write aligned SCAT files in bounded-memory batches with global key checks."""
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    source, test_path, target_path = map(Path, (source, test_path, target_path))
    test_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    parquet = pq.ParquetFile(source)
    key_column = "primary_key"
    with tempfile.TemporaryDirectory(prefix="hscat-keys-") as work:
        db_path = Path(work) / "keys.sqlite"
        with sqlite3.connect(db_path) as db:
            db.execute("CREATE TABLE keys (key TEXT NOT NULL, mission TEXT NOT NULL)")
            for batch in parquet.iter_batches(
                batch_size=batch_size, columns=[key_column, "_curation_mission"]
            ):
                keys = batch.column(key_column).to_pylist()
                missions = batch.column("_curation_mission").to_pylist()
                db.executemany(
                    "INSERT INTO keys VALUES (?, ?)",
                    (
                        (key, mission)
                        for key, mission in zip(keys, missions)
                        if key is not None and str(key).strip()
                    ),
                )
            db.execute("CREATE INDEX key_index ON keys(key)")
            db.execute("CREATE TEMP TABLE batch_keys (key TEXT PRIMARY KEY)")
            duplicate_key_count = db.execute(
                "SELECT COUNT(*) FROM (SELECT key FROM keys GROUP BY key HAVING COUNT(*) > 1)"
            ).fetchone()[0]
            duplicate_rows_by_mission = {}
            duplicate_rows = 0
            test_writer = target_writer = None
            value_chunks = []
            accepted = excluded_incomplete = 0
            try:
                for batch in parquet.iter_batches(batch_size=batch_size):
                    frame = batch.to_pandas()
                    keys = frame[key_column].dropna().astype(str).unique().tolist()
                    if keys:
                        db.execute("DELETE FROM batch_keys")
                        db.executemany(
                            "INSERT OR IGNORE INTO batch_keys VALUES (?)", ((key,) for key in keys)
                        )
                        duplicates = {
                            row[0]
                            for row in db.execute(
                                "SELECT batch_keys.key FROM batch_keys JOIN keys USING (key) "
                                "GROUP BY batch_keys.key HAVING COUNT(*) > 1"
                            )
                        }
                    else:
                        duplicates = set()
                    test, target, part = build_scat_frames(frame, variable, duplicates)
                    excluded_incomplete += part["excluded_incomplete_rows"]
                    accepted += len(test)
                    duplicate_rows += part["excluded_duplicate_key_rows"]
                    for mission, count in part["duplicate_key_rows_by_mission"].items():
                        duplicate_rows_by_mission[mission] = (
                            duplicate_rows_by_mission.get(mission, 0) + count
                        )
                    if not test.empty:
                        value_chunks.append(
                            test[part["reference_column"]].to_numpy(dtype=np.float64)
                        )
                    for current, path, writer in (
                        (test, test_path, test_writer),
                        (target, target_path, target_writer),
                    ):
                        if current.empty:
                            continue
                        table = pa.Table.from_pandas(current, preserve_index=False)
                        if writer is None:
                            table = table.replace_schema_metadata(
                                metadata
                                or {
                                    b"source scat": b"KNMI-HSCAT-HY2-25km",
                                    b"source ancillary datasets": b"Configured HSCAT recipe curation filters applied",
                                    b"library used to produce the parquet": b"soba_reference_repo",
                                    b"library version": library_version().encode(),
                                    b"creation date": b"",
                                }
                            )
                            writer = pq.ParquetWriter(path, table.schema)
                            if path == test_path:
                                test_writer = writer
                            else:
                                target_writer = writer
                        else:
                            table = table.cast(writer.schema)
                        writer.write_table(table)
            finally:
                if test_writer is not None:
                    test_writer.close()
                if target_writer is not None:
                    target_writer.close()
            if accepted == 0:
                raise ValueError("no HSCAT rows remain after output-integrity checks")
            test_output, target_output = pq.ParquetFile(test_path), pq.ParquetFile(target_path)
            if test_output.schema_arrow.names != output_columns("test", variable):
                raise ValueError("chunked TEST output has an invalid schema")
            if target_output.schema_arrow.names != output_columns("target", variable):
                raise ValueError("chunked TARGET output has an invalid schema")
            test_batches = test_output.iter_batches(batch_size=batch_size, columns=[key_column])
            target_batches = target_output.iter_batches(batch_size=batch_size, columns=[key_column])
            for test_batch, target_batch in zip(test_batches, target_batches, strict=True):
                test_keys = test_batch.column(0).to_pylist()
                target_keys = target_batch.column(0).to_pylist()
                if test_keys != target_keys or len(test_keys) != len(set(test_keys)):
                    raise ValueError("chunked TEST/TARGET keys differ or are not unique")
    values = np.concatenate(value_chunks) if value_chunks else np.array([], dtype=np.float64)
    return {
        "accepted_rows": accepted,
        "excluded_incomplete_rows": excluded_incomplete,
        "excluded_duplicate_key_rows": duplicate_rows,
        "duplicate_key_rows_by_mission": duplicate_rows_by_mission,
        "duplicate_primary_keys": duplicate_key_count,
        "reference_values": values,
        "reference_column": VARIABLE_COLUMNS[variable][1],
    }


def output_columns(file_type, variable):
    if file_type not in {"test", "target"}:
        raise ValueError(f"unsupported SCAT file type: {file_type}")
    if variable not in VARIABLE_COLUMNS:
        raise ValueError(f"unsupported SCAT reference variable: {variable}")
    output_variable = VARIABLE_COLUMNS[variable][1]
    if file_type == "test":
        return [*BASE_TEST_COLUMNS, "scat_time", output_variable]
    return [*BASE_TARGET_COLUMNS, "scat_time", output_variable]


def validate_scat_file(path, file_type):
    """Validate one exported SCAT TEST or TARGET Parquet against its exact contract."""
    table = pq.read_table(path)
    variable_columns = [
        name for name in ("scat_windspeed", "scat_winddirection") if name in table.column_names
    ]
    if len(variable_columns) != 1:
        raise ValueError("SCAT Parquet must contain exactly one reference variable column")
    variable = "windspeed" if variable_columns[0] == "scat_windspeed" else "winddirection"
    expected = output_columns(file_type, variable)
    if table.column_names != expected:
        raise ValueError(f"SCAT {file_type.upper()} schema/order must be {expected}")
    metadata = table.schema.metadata or {}
    missing_metadata = set(SCAT_METADATA_KEYS) - set(metadata)
    if missing_metadata:
        raise ValueError(f"SCAT metadata missing required attributes: {sorted(missing_metadata)}")
    for name in expected:
        dtype = table.schema.field(name).type
        if name in {"primary_key", "sar_safe_slc", "sar_safe_ocn"}:
            valid = pa.types.is_string(dtype) or pa.types.is_large_string(dtype)
        elif name in {"sar_time", "scat_time"}:
            valid = pa.types.is_timestamp(dtype) and dtype.unit == "ns"
        else:
            valid = dtype == pa.float32()
        if not valid:
            raise ValueError(f"invalid SCAT {file_type.upper()} field type: {name}: {dtype}")
    if any(table.column(name).null_count for name in expected):
        raise ValueError("SCAT required TEST/TARGET values must be non-null")
    keys = table.column("primary_key").to_pylist()
    if any(not isinstance(key, str) or not key.strip() for key in keys):
        raise ValueError("SCAT primary_key values must be nonempty strings")
    if len(keys) != len(set(keys)):
        raise ValueError("SCAT primary_key values must be unique")
    numeric = [
        name
        for name in expected
        if name not in {"primary_key", "sar_time", "scat_time", "sar_safe_slc", "sar_safe_ocn"}
    ]
    for name in numeric:
        values = table.column(name).to_numpy(zero_copy_only=False)
        if not np.isfinite(values).all():
            raise ValueError(f"SCAT numeric values must be finite: {name}")
    return True


def _write_pair(
    test, target, root, recipe, product, product_description, product_name, polarization
):
    stem = (
        f"S1_reference_test_dataset_WV_{recipe['production_date']}_"
        f"{recipe['reference_variable']}_{recipe['version']}"
    )
    target_stem = stem.replace("reference_test_dataset", "target_dataset", 1)
    directory = root / "datasets"
    directory.mkdir(parents=True, exist_ok=True)
    test_path, target_path = directory / f"{stem}.parquet", directory / f"{target_stem}.parquet"
    metadata = {
        b"source scat": product_description.encode(),
        b"source ancillary datasets": b"Configured HSCAT recipe curation filters applied",
        b"library used to produce the parquet": b"soba_reference_repo",
        b"library version": library_version().encode(),
        b"creation date": recipe["production_date"].encode(),
    }
    for frame, path in ((test, test_path), (target, target_path)):
        table = pa.Table.from_pandas(frame, preserve_index=False).replace_schema_metadata(metadata)
        pq.write_table(table, path)
    if test.primary_key.tolist() != target.primary_key.tolist():
        raise ValueError("SCAT TEST/TARGET primary_key values differ")
    if not test.primary_key.is_unique:
        raise ValueError("SCAT primary_key values must be unique")
    return test_path, target_path


def _report(test_path, target_path, manifest, summary, figures, report_dir, compile_report):
    from .hscat_report import build_hscat_report

    tex_path = build_hscat_report(test_path, target_path, manifest, summary, figures, report_dir)
    pdf_path = compile_pdf(report_dir, test_path.stem) if compile_report else None
    if pdf_path:
        purge_latex_byproducts(report_dir, test_path.stem)
    return tex_path, pdf_path


def run_hscat_recipe(recipe_path):
    recipe = read_recipe(recipe_path)
    product = "HSCAT"
    catalogues = sorted(recipe["catalogues"], key=lambda item: item["mission"])
    for item in catalogues:
        item["info"], item["attrs"] = validate_scat_input(item["path"], product)
        item["resolved_rules"] = resolve_rules(
            item.get("rules", []), defaults=HSCAT_DEFAULT_RULES if product == "HSCAT" else ()
        )
    polarizations = {item["info"]["polarization"] for item in catalogues}
    if len(polarizations) != 1:
        raise ValueError("one SCAT run requires a single common polarization across catalogues")
    source_values = {item["attrs"].get("source ref", "") for item in catalogues}
    source_value = "; ".join(sorted(source_values))
    root = Path(recipe["output_dir"])
    if root.exists():
        raise FileExistsError(f"refusing existing run directory: {root}")
    root.mkdir(parents=True)
    shutil.copyfile(recipe_path, root / "recipe.toml")
    manifest = {
        "status": "running",
        "reference": "scat",
        "product": product,
        "product_description": source_value,
        "reference_variable": recipe["reference_variable"],
        "production_date": recipe["production_date"],
        "version": recipe["version"],
        "filters_applied": [],
        "sources": [],
    }
    stage = "curation"
    merge_work = None
    try:
        curated = {}
        for item in catalogues:
            mission = item["mission"]
            destination = (
                root
                / "curated"
                / mission
                / (
                    f"{mission}_curated_coaligned_dataset_WV_{recipe['production_date']}_"
                    f"{recipe['reference_variable']}_{recipe['version']}.parquet"
                )
            )
            info = write_curated(item["path"], destination, item["resolved_rules"], mission)
            curated[mission] = destination
            source = {
                "mission": mission,
                "product": product,
                **item["info"],
                "path": str(item["path"]),
                "curated_path": str(destination),
                "input_rows": info["input_rows"],
                "curated_rows": info["curated_rows"],
                "quality_filter_steps": info["steps"],
            }
            manifest["sources"].append(source)
            manifest["filters_applied"].extend(
                {
                    "mission": mission,
                    "id": step["id"],
                    "name": step["name"],
                    "clauses": step["clauses"],
                }
                for step in info["steps"]
                if step["status"] == "applied"
            )
        stage = "temporary_merge"
        merge_work = tempfile.TemporaryDirectory(prefix="hscat-curated-")
        merged_path = Path(merge_work.name) / f"S1_WV_{product.lower()}_curated.parquet"
        merge_curated(curated, merged_path, normalize_timestamp_units=True)
        stage = "pair"
        stem = (
            f"S1_reference_test_dataset_WV_{recipe['production_date']}_"
            f"{recipe['reference_variable']}_{recipe['version']}"
        )
        test_path = root / "datasets" / f"{stem}.parquet"
        target_path = (
            root
            / "datasets"
            / f"{stem.replace('reference_test_dataset', 'target_dataset', 1)}.parquet"
        )
        metadata = {
            b"source scat": source_value.encode(),
            b"source ancillary datasets": b"Configured HSCAT recipe curation filters applied",
            b"library used to produce the parquet": b"soba_reference_repo",
            b"library version": library_version().encode(),
            b"creation date": recipe["production_date"].encode(),
        }
        summary = write_scat_pair_chunked(
            merged_path, test_path, target_path, recipe["reference_variable"], metadata=metadata
        )
        manifest.update({key: value for key, value in summary.items() if key != "reference_values"})
        manifest["input_rows"] = sum(item["input_rows"] for item in manifest["sources"])
        stage = "report"
        report_dir = root / "report"
        report_dir.mkdir(parents=True, exist_ok=True)
        values = summary["reference_values"]
        figures = plot_hscat_figures(
            test_path, recipe["reference_variable"], report_dir
        )
        if sum(figures["mission_counts"].values()) != summary["accepted_rows"]:
            raise ValueError("report mission counts do not equal accepted TEST rows")
        stats = {
            "rows": len(values),
            "min": float(values.min()),
            "max": float(values.max()),
            "mean": float(values.mean()),
            "median": float(np.median(values)),
        }
        if recipe["reference_variable"] == "winddirection":
            radians = np.deg2rad(values)
            mean_direction = (
                np.degrees(np.arctan2(np.sin(radians).mean(), np.cos(radians).mean())) % 360
            )
            offsets = (values - mean_direction + 180) % 360 - 180
            stats["mean"] = float(mean_direction)
            stats["median"] = float((mean_direction + np.median(offsets)) % 360)
        tex_path, pdf_path = _report(
            test_path,
            target_path,
            manifest,
            stats,
            figures,
            report_dir,
            recipe.get("compile", False),
        )
        manifest.update(
            {
                "status": "complete",
                "test_parquet": str(test_path),
                "target_parquet": str(target_path),
                "report_tex": str(tex_path),
                "pdf": str(pdf_path) if pdf_path else None,
                "figures": {
                    key: str(value) for key, value in figures.items() if key != "mission_counts"
                },
                "summary": stats,
            }
        )
    except Exception as exc:
        if merge_work is not None:
            merge_work.cleanup()
        manifest.update({"status": "failed", "failed_stage": stage, "error": str(exc)})
        (root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        raise
    if merge_work is not None:
        merge_work.cleanup()
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    filter_summary = (
        f"{len(manifest['filters_applied'])} quality filters applied"
        if manifest["filters_applied"]
        else "no filters applied"
    )
    print(
        f"SCAT {product} {recipe['reference_variable']}: {summary['accepted_rows']:,} rows; "
        f"{filter_summary}"
    )
    print(f"TEST: {test_path}\nTARGET: {target_path}\nREPORT: {tex_path}")
    return 0
