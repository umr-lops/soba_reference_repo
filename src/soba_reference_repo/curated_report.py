"""Fill the established SOBA document structure with Curated catalogue contents."""
from __future__ import annotations

import json
import re
import shutil
from graphlib import CycleError, TopologicalSorter
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

from .hscat_report import _tex
from .pdf_support import ASSET_DIR, _path, library_version


def _rows(rows):
    return "\n".join(" & ".join(row) + r" \\" for row in rows)


def build_curated_report(*, title, platform, instrument, reference, variable,
                         document_stem, recipe, manifest, figures, summary, observation_path,
                         field_descriptions):
    """Preserve the supplied PDF's cover, section order, tables, and SOBA style."""
    sources = sorted(manifest["sources"], key=lambda item: item["mission"])
    schemas = {item["mission"]: pq.read_schema(item["curated_path"]) for item in sources}
    metadata = {}
    for mission, schema in schemas.items():
        attrs = {key.decode(): value.decode("utf-8", errors="replace")
                 for key, value in (schema.metadata or {}).items()
                 if key not in {b"pandas", b"ARROW:schema", b"utils_meta"}}
        if b"utils_meta" in (schema.metadata or {}):
            attrs.update(json.loads(schema.metadata[b"utils_meta"]).get("attrs", {}))
        metadata[mission] = attrs
    native_names = set().union(*(set(schema.names) for schema in schemas.values()))
    observation_schema = pq.read_schema(observation_path)
    names = observation_schema.names
    hscat = "HSCAT" in reference
    product = "HSCAT" if hscat else "SWOT"
    selected_variable = recipe.get("reference_variable", "swh")
    value_column = (
        ("ref_param_1" if selected_variable == "winddirection" else "ref_param_2")
        if hscat else "ref_mean_hs_karin"
    )
    units = ("degrees" if selected_variable == "winddirection" else "m/s") if hscat else "m"
    reference_label = (
        ("HSCAT wind directions" if selected_variable == "winddirection" else "HSCAT wind speeds")
        if hscat else "SWOT significant wave heights"
    )
    dataset_name = f"HSCAT {selected_variable}" if hscat else "SWOT significant wave height"
    row_count = sum(item["curated_rows"] for item in sources)
    values = np.concatenate([
        pq.read_table(item["curated_path"], columns=[value_column]).column(0)
        .to_numpy(zero_copy_only=False).astype(float)
        for item in sources
    ])
    finite = values[np.isfinite(values)]
    stats = {"Minimum": finite.min() if len(finite) else np.nan,
             "Maximum": finite.max() if len(finite) else np.nan,
             "Mean": summary.get("mean", finite.mean() if len(finite) else np.nan),
             "Median": summary.get("median", np.median(finite) if len(finite) else np.nan)}
    stats_rows = [(key, f"{value:.4g} {units}") for key, value in stats.items()]
    stats_rows.append(("Rows", f"{row_count:,}"))
    stats_table = "\n".join([
        r"\begin{table}[H]\centering",
        r"\caption{Summary statistics for " + _tex(reference_label) + f" ({units})" + "}",
        r"\label{tab:reference_stats}", r"\begin{tabular}{lr}", r"\toprule",
        r"\textbf{Statistic} & \textbf{Value} \\", r"\midrule",
        _rows([(_tex(key), _tex(value)) for key, value in stats_rows]),
        r"\bottomrule", r"\end{tabular}", r"\end{table}",
    ])
    descriptions = {
        **{name: str(value) for attrs in metadata.values() for name, value in attrs.items()
           if name in names},
        "sar_incidence_angle": "SAR incidence angle in degrees.",
        "sar_elevation_angle": "SAR elevation angle in degrees.",
        "sar_ground_heading": "SAR ground heading in degrees.",
        "ref_lon": f"{product} reference longitude in degrees.",
        "ref_lat": f"{product} reference latitude in degrees.",
        "ref_time": f"UTC {product} reference acquisition time.",
        **field_descriptions,
    }
    descriptions.update({
        "scat_lon": "HSCAT reference longitude in degrees.",
        "scat_lat": "HSCAT reference latitude in degrees.",
        "scat_time": "UTC HSCAT reference acquisition time.",
        "scat_windspeed": "HSCAT wind speed in metres per second.",
        "scat_winddirection": "HSCAT wind direction in degrees clockwise from north.",
        "swot_lon": "SWOT reference longitude in degrees.",
        "swot_lat": "SWOT reference latitude in degrees.",
        "swot_time": "UTC SWOT reference acquisition time.",
        "swot_waveheight": "SWOT significant wave height in metres.",
        "swot_source": "Identifier of the source SWOT observation product.",
        "primary_key": ("Source matchup identifier retained in the observation file."
                        if hscat else
                        "SLC SAFE and reference longitude/latitude at one decimal degree."),
    })
    column_lines = [_rows([
        (_tex(field.name), _tex(field.type),
         _tex(descriptions.get(field.name, "Value associated with this matched observation.")
              .replace("co-aligned", "matched")))
    ]) for field in observation_schema]
    keys = sorted(set().union(*(set(attrs) for attrs in metadata.values())) - native_names)
    metadata_rows = []
    for key in keys:
        by_value = {}
        for mission, attrs in metadata.items():
            if key in attrs:
                by_value.setdefault(str(attrs[key]), []).append(mission)
        value = "; ".join(
            (f"{', '.join(missions)}: " if len(by_value) > 1 else "") + text
            for text, missions in by_value.items()
        )
        metadata_rows.append((_tex(key), _tex(value)))
    if not metadata_rows:
        metadata_rows = [("Global attributes", "No scientific attributes in these sources.")]
    paths = list(figures.values())
    assets = f"images_{document_stem}"
    sar_product = "Copernicus/ESA Sentinel-1 Level-2 Ocean (OCN), Wave Mode"
    ref_product = (
        "HY-2 HSCAT 25 km ocean surface winds, processed by KNMI for EUMETSAT OSI SAF"
        if hscat else
        "SWOT KaRIn Level-2 WindWave D0 (PGD0/PID0), distributed by NASA/JPL PO.DAAC"
    )
    disabled = {
        (item["mission"], rule["id"])
        for item in recipe.get("catalogues", [])
        for rule in item.get("resolved_rules", item.get("rules", []))
        if not rule.get("enabled", True)
    }
    active = [
        (source["mission"], step)
        for source in sources
        for step in source.get("quality_filter_steps", source.get("steps", []))
        if step.get("status") not in {"disabled", "skipped"}
        and (source["mission"], step["id"]) not in disabled
    ]
    public_checks = {
        "ecmwf_scat_speed_difference": "wind-speed agreement with meteorological estimates",
        "ecmwf_scat_direction_difference": "wind-direction agreement with meteorological estimates",
        "zero_ice_probability": "ice-free reference observations",
        "zero_ref_flag": "reference quality checks",
        "imagette_direction_consistency": "consistent reference directions within each SAR image",
        "sea_proxy": "open-water observations away from the coast",
        "no_dynamic_ice": "ice-free observations",
        "time_present": "valid time differences",
        "karin_positive": "positive SWOT wave heights",
        "ocn_positive": "positive Sentinel-1 ocean-product wave heights",
        "altimeter_positive": "positive nearby altimeter wave heights",
        "ww3_positive": "positive model wave heights",
        "classification_present": "available scene classifications",
        "time_under_two_hours": "observations separated by less than two hours",
        "full_overlap": "complete footprint overlap",
        "imerg_rain": "rain screening using IMERG",
        "no_swot_rain_flag": "SWOT rain-quality screening",
        "accepted_classes": "accepted ocean-scene classes",
    }
    checks = list(dict.fromkeys(public_checks[step["id"]] for _, step in active
                               if step["id"] in public_checks))
    if any(clause[1] == "imagette_circstd_le"
           for _, step in active for clause in step.get("clauses", [])):
        checks.append("consistent reference directions within each SAR image")
    checks = list(dict.fromkeys(checks))
    selection = ("The additional selection checks " + "; ".join(checks) + "."
                 if checks else "No additional quality screening is enabled.")
    dates = []
    for source in sources:
        windows = []
        for mission, step in active:
            if mission != source["mission"]:
                continue
            for clause in step.get("clauses", []):
                if clause[0] == "sar_time" and clause[1] in {"date_gt", "date_lt"}:
                    boundary = "after" if clause[1] == "date_gt" else "before"
                    windows.append(f"strictly {boundary} {clause[2]} at 00:00 UTC")
        dates.append((source["mission"], "; ".join(windows) or "no additional date restriction"))
    provenance = {
        "sar_product_provider": sar_product, "reference_product_provider": ref_product,
        "software_version": library_version(), "recipe_snapshot": "recipe.toml",
        "command": "python -m soba_reference_repo.create_test_dataset --recipe recipe.toml",
        "date_selection_rationale": "Supplied evaluation windows; no further rationale recorded.",
        "observation_schema_file": Path(observation_path).name,
        "source_catalogues": [
            {"mission": item["mission"], "filename": Path(item["path"]).name,
             "version": Path(item["path"]).stem.rsplit("_", 1)[-1]}
            for item in sources
        ],
    }
    manifest["report_provenance"] = provenance
    metadata_rows = [
        ("SAR product / provider", _tex(sar_product)),
        ("Reference product / provider", _tex(ref_product)),
        ("Dataset version", _tex(recipe["version"])),
        ("Dataset production date", _tex(recipe["production_date"])),
        ("Processing software version", _tex(provenance["software_version"])),
        ("Processing recipe", _path("recipe.toml")),
        *[(f"{item['mission']} source catalogue", _path(Path(item["path"]).name))
          for item in sources],
        *metadata_rows,
    ]
    source_rows = [
        (item["mission"], f"{item['input_rows']:,}", f"{item['curated_rows']:,}",
         f"{item['input_rows'] - item['curated_rows']:,}")
        for item in sources
    ]
    # Keep each mission's filter order, including mission-specific date rules.
    steps_by_mission = {source["mission"]: {} for source in sources}
    predecessors = {}
    for source in sources:
        previous = None
        for mission, step in active:
            if mission != source["mission"]:
                continue
            rule_id = step["id"]
            steps_by_mission[mission][rule_id] = step
            predecessors.setdefault(rule_id, set())
            if previous is not None:
                predecessors[rule_id].add(previous)
            previous = rule_id
    try:
        step_groups = [
            [steps_by_mission[source["mission"]].get(rule_id) for source in sources]
            for rule_id in TopologicalSorter(predecessors).static_order()
        ]
    except CycleError:
        # Repeat conflicting rows rather than reorder either mission's filters.
        pending = [list(steps_by_mission[source["mission"]].values()) for source in sources]
        step_groups = []
        while any(pending):
            rule_id = next(steps[0]["id"] for steps in pending if steps)
            step_groups.append([
                steps.pop(0) if steps and steps[0]["id"] == rule_id else None
                for steps in pending
            ])
    count_rows = []
    for steps in step_groups:
        rule_id = next(step["id"] for step in steps if step)
        name = next(step["name"] for step in steps if step)
        if rule_id in {"sar_start", "sar_end"}:
            name = "SAR acquisition " + ("start" if rule_id == "sar_start" else "end")
            name += "-date selection (mission-specific)"
        for technical, explicit in {
            "ref_param_1": "HSCAT wind direction", "ref_param_2": "HSCAT wind speed",
            "ref_ice_prob": "HSCAT ice probability", "ref_flag": "HSCAT quality flag",
            "ecmwf_wind_speed": "ECMWF wind speed",
            "ecmwf_wind_dir_360": "ECMWF wind direction",
        }.items():
            name = name.replace(technical, explicit)
        name = name.replace("==", "equals").replace("<=", "at most")
        count_rows.append((_tex(name), *(
            f"{step['remaining']:,} / {step['removed']:,}" if step else "--"
            for step in steps
        )))
    count_spec = r"@{}>{\raggedright\arraybackslash}p{4.8cm}" + (
        r">{\raggedright\arraybackslash}p{2.7cm}" * len(sources)) + "@{}"
    count_header = _rows([("Filter", *(source["mission"] for source in sources))])
    sequential_counts = "\n".join([
        r"\begin{table}[H]\centering",
        r"\caption{Sequential quality-filter counts by mission (remaining / removed).}",
        r"\label{tab:quality_filters}",
        r"{\small\setlength{\tabcolsep}{4pt}",
        r"\begin{tabular}{" + count_spec + "}",
        r"\toprule", count_header, r"\midrule", _rows(count_rows),
        r"\bottomrule", r"\end{tabular}", "}", r"\end{table}",
    ])
    # Owner-supplied image is vendored verbatim; reports use their local asset bundle.
    dataflow_dir = Path(paths[0]).parent
    shutil.copyfile(ASSET_DIR / "latex" / "sobadataflow.png", dataflow_dir / "sobadataflow.png")
    references = (
        r"\bibitem{soba_format} SOBA Consortium. "
        r"Dataset description for Parquet catalogues. "
        r"\url{https://soba.ifremer.fr/documents/dataset_description/"
        r"SOBA_dataset_description_parquet_catalogue.pdf}"
    )
    mapping = {
        "SEQUENTIAL_COUNTS": sequential_counts,
        "DATAFLOW_FIGURE": f"{assets}/sobadataflow.png",
        "STYLE_FILE": f"{assets}/soba", "GRAPHICS_DIRECTORY": assets,
        "DATASET_NAME": _tex(dataset_name), "DOCUMENT_PRODUCT": product,
        "REFERENCE_LABEL": reference_label,
        "DISTRIBUTION_TITLE": (
            ("HSCAT Wind-Direction Distribution" if selected_variable == "winddirection"
             else "HSCAT Wind-Speed Distribution") if hscat
            else "SWOT Significant Wave-Height Distribution"
        ),
        "SCOPE_TEXT": f"A curated set of Sentinel-1 Wave Mode observations matched to "
                      f"{product} reference measurements for independent algorithm evaluation.",
        "FILE_VERSION_ROW": _rows([
            (_path(Path(item["curated_path"]).name), recipe["production_date"],
             f"Contains {item['curated_rows']:,} retained matches.") for item in sources
        ]),
        "GENERAL_DESCRIPTION": _tex(
            f"{sar_product} observations are first matched in space and time to {ref_product}, "
            "then additional filters select reliable, consistent matches for evaluation."
        ),
        "COVERAGE_DESCRIPTION": "The map shows the locations of all retained Sentinel-1 matches.",
        "COVERAGE_FIGURE": f"{assets}/{Path(paths[0]).name}",
        "COVERAGE_CAPTION": "Global spatial distribution of retained Sentinel-1 matches.",
        "MONTHLY_DESCRIPTION": "Retained matches by SAR acquisition month, stacked by mission.",
        "MONTHLY_FIGURE": f"{assets}/{Path(paths[1]).name}",
        "MONTHLY_CAPTION": f"Monthly count of retained {product} matches, stacked by mission.",
        "REFERENCE_DESCRIPTION": f"Colored lines show {_tex(variable)} by mission. "
                                 "Each mission is normalized to its own sample count, "
                                 "shown in the legend; percentages do not compare absolute counts.",
        "WINDSPEED_FIGURE": f"{assets}/{Path(paths[2]).name}",
        "REFERENCE_CAPTION": f"Normalized {product} distributions of retained matches by mission.",
        "REFERENCE_STATS_TABLE": stats_table, "TEST_COLUMNS": "\n".join(column_lines),
        "SAMPLING_NOTE": r"\noindent\textbf{Sampling point.} SAR geometry and " + product +
                         " values describe observations matched in space and time.",
        "METADATA_ROWS": _rows(metadata_rows), "SAR_PRODUCT": _tex(sar_product),
        "REFERENCE_PRODUCT": _tex(ref_product), "SELECTION_SUMMARY": _tex(selection),
        "DATE_SELECTION": "\n".join(r"\item " + _tex(f"{mission}: {window}.")
                                      for mission, window in dates),
        "BEFORE_AFTER_ROWS": _rows(source_rows), "ROW_COUNT": f"{row_count:,}",
        "INPUT_COUNT": f"{sum(item['input_rows'] for item in sources):,}",
        "REMOVED_COUNT": f"{sum(item['input_rows'] for item in sources)-row_count:,}",
        "PRODUCT_REFERENCES": references,
    }
    text = (ASSET_DIR / "latex" / "curated_template.tex").read_text()
    for key, value in mapping.items():
        text = text.replace(f"@@{key}@@", value)
    leftovers = re.findall(r"@@[A-Z_]+@@", text)
    if leftovers:
        raise ValueError(f"Unfilled Curated report fields: {leftovers}")
    return text
