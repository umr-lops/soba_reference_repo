"""Build the standard SOBA report and figures for HSCAT reference datasets."""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
from matplotlib import pyplot as plt

from .pdf_support import ASSET_DIR, _path, stage_latex_assets
from .plotting import MISSION_COLORS, plot_mission_distribution

MISSION_RE = re.compile(r"^(S1[A-D])")


def _mission_series(frame):
    missions = frame["sar_safe_slc"].astype("string").str.extract(MISSION_RE, expand=False)
    if missions.isna().any():
        raise ValueError("could not identify Sentinel-1 mission from sar_safe_slc")
    return missions


def _annual_tick_indices(months):
    """Return readable January ticks plus the final month for a monthly series."""
    indices = list(range(0, len(months), 12))
    if months and indices[-1] != len(months) - 1:
        indices.append(len(months) - 1)
    return indices


def plot_hscat_figures(test_path, variable, output_dir, batch_size=65_536):
    """Create coverage, monthly-count, and reference-value figures by Parquet batch."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    value_column = {"windspeed": "scat_windspeed", "winddirection": "scat_winddirection"}.get(
        variable
    )
    if value_column is None:
        raise ValueError(f"unsupported HSCAT variable: {variable}")
    parquet = pq.ParquetFile(test_path)
    required = ["sar_lon", "sar_lat", "sar_time", "sar_safe_slc", value_column]
    missing = set(required) - set(parquet.schema_arrow.names)
    if missing:
        raise ValueError(f"HSCAT TEST file lacks figure columns: {sorted(missing)}")

    coverage_path = output_dir / "sar_coverage.png"
    monthly_path = output_dir / "monthly_rows.png"
    distribution_path = output_dir / f"scat_{variable}_distribution.png"
    monthly_counts = Counter()
    mission_counts = Counter()
    min_value, max_value = np.inf, -np.inf
    figure, axis = plt.subplots(figsize=(10, 5))
    for batch in parquet.iter_batches(batch_size=batch_size, columns=required):
        frame = batch.to_pandas()
        missions = _mission_series(frame)
        lon = pd.to_numeric(frame["sar_lon"], errors="coerce").to_numpy(dtype=float)
        lat = pd.to_numeric(frame["sar_lat"], errors="coerce").to_numpy(dtype=float)
        values = pd.to_numeric(frame[value_column], errors="coerce").to_numpy(dtype=float)
        if not (np.isfinite(lon).all() and np.isfinite(lat).all() and np.isfinite(values).all()):
            raise ValueError("HSCAT TEST figure inputs contain non-finite values")
        axis.scatter(lon, lat, s=3, alpha=0.55, color="#526F85", linewidths=0)
        mission_counts.update(missions.tolist())
        months = pd.to_datetime(frame["sar_time"], utc=True).dt.strftime("%Y-%m")
        monthly_counts.update(zip(months.tolist(), missions.tolist(), strict=True))
        min_value = min(min_value, float(values.min()))
        max_value = max(max_value, float(values.max()))
    if not mission_counts:
        plt.close(figure)
        raise ValueError("HSCAT TEST file has no rows for report figures")
    axis.set(
        xlim=(-180, 180),
        ylim=(-90, 90),
        xlabel="Longitude (degrees)",
        ylabel="Latitude (degrees)",
        title="Sentinel-1 WV coverage",
    )
    axis.grid(alpha=0.2)
    figure.tight_layout()
    figure.savefig(coverage_path, dpi=150)
    plt.close(figure)

    present = sorted(mission_counts)
    months = sorted({month for month, _ in monthly_counts})
    figure, axis = plt.subplots(figsize=(10, 5))
    month_positions = np.arange(len(months))
    bottom = np.zeros(len(months), dtype=np.int64)
    for mission in present:
        heights = np.asarray([monthly_counts[(month, mission)] for month in months], dtype=np.int64)
        axis.bar(
            month_positions,
            heights,
            bottom=bottom,
            width=0.85,
            color=MISSION_COLORS[mission],
            label=mission,
        )
        bottom += heights
    axis.legend()
    axis.set(xlabel="SAR month", ylabel="TEST rows", title="TEST rows by month")
    tick_indices = _annual_tick_indices(months)
    axis.set_xticks(tick_indices, [months[index] for index in tick_indices])
    axis.tick_params(axis="x", labelrotation=45)
    axis.grid(axis="y", alpha=0.2)
    figure.tight_layout()
    figure.savefig(monthly_path, dpi=150)
    plt.close(figure)

    if variable == "winddirection":
        edges = np.linspace(0, 360, 37)
    else:
        if min_value == max_value:
            min_value, max_value = min_value - 0.5, max_value + 0.5
        edges = np.linspace(min_value, max_value, 41)
    histogram = {mission: np.zeros(len(edges) - 1, dtype=np.int64) for mission in present}
    for batch in parquet.iter_batches(
        batch_size=batch_size, columns=["sar_safe_slc", value_column]
    ):
        frame = batch.to_pandas()
        missions = _mission_series(frame)
        values = pd.to_numeric(frame[value_column], errors="coerce").to_numpy(dtype=float)
        for mission in present:
            selected = values[missions.to_numpy() == mission]
            if selected.size:
                histogram[mission] += np.histogram(selected, bins=edges)[0]
    figure, axis = plt.subplots(figsize=(9, 5.0))
    plot_mission_distribution(axis, histogram, edges, MISSION_COLORS)
    if variable == "windspeed":
        xlabel, title = "HSCAT wind speed (m/s)", "Reference wind-speed distribution"
    else:
        xlabel, title = "HSCAT wind direction (degrees)", "Reference wind-direction distribution"
    axis.set(xlabel=xlabel, title=title)
    axis.grid(axis="y", alpha=0.2)
    figure.tight_layout(rect=(0, 0.18, 1, 1))
    figure.savefig(distribution_path, dpi=150)
    plt.close(figure)
    return {
        "coverage": coverage_path,
        "monthly_rows": monthly_path,
        "reference_distribution": distribution_path,
        "mission_counts": dict(sorted(mission_counts.items())),
    }


def _tex(value):
    replacements = {
        "\\": r"\textbackslash{}",
        "&": r"\&",
        "%": r"\%",
        "$": r"\$",
        "#": r"\#",
        "_": r"\_",
        "{": r"\{",
        "}": r"\}",
    }
    return "".join(replacements.get(character, character) for character in str(value))


def _rows(items):
    return "\n".join(f"{item} \\\\" for item in items)


def _filter_expression(clauses):
    expressions = []
    symbols = {
        "eq": "==",
        "gt": "greater than",
        "ge": "at least",
        "lt": "less than",
        "le": "at most",
    }
    for clause in clauses:
        column, operation = clause[:2]
        if operation in {"date_gt", "date_lt"}:
            direction = "later" if operation == "date_gt" else "earlier"
            expressions.append(
                f"{_tex(column)} is strictly {direction} than 00:00 UTC on {_tex(clause[2])}"
            )
        elif operation in {"abs_diff_le", "angle_diff_le"}:
            other, threshold = clause[2]
            left, right = _tex(column), _tex(other)
            if operation == "abs_diff_le":
                expressions.append(f"abs({left} - {right}) {symbols['le']} {threshold}")
            else:
                expressions.append(
                    f"circular difference ({left}, {right}) {symbols['le']} "
                    f"{threshold} degrees"
                )
        elif operation in symbols:
            expressions.append(f"{_tex(column)} {symbols[operation]} {clause[2]}")
        elif operation == "present":
            expressions.append(f"{_tex(column)} is present")
        else:
            expressions.append(_tex(str(clause)))
    return " and ".join(expressions)


def _filter_tables(sources):
    missions = [source["mission"] for source in sources]
    rule_steps = {source["mission"]: source.get("quality_filter_steps", []) for source in sources}
    ids = list(dict.fromkeys(step["id"] for steps in rule_steps.values() for step in steps))
    row_end = " " + chr(92) * 2
    tables = [r"\textbf{Common quality filters (same rule for every mission):}"]
    rows = []
    mission_specific_rows = []
    for rule_id in ids:
        steps = [
            next((step for step in rule_steps[mission] if step["id"] == rule_id), None)
            for mission in missions
        ]
        available = [step for step in steps if step is not None]
        if not available:
            continue
        signatures = {
            (step["name"], repr(step.get("clauses", []))) for step in available
        }
        if len(available) == len(missions) and len(signatures) == 1:
            name = available[0]["name"]
            expression = _filter_expression(available[0].get("clauses", []))
            rule = r"\texttt{" + expression + "}"
            rows.append(f"    {_tex(name)} & {rule}{row_end}")
        else:
            for mission, step in zip(missions, steps, strict=True):
                if step is None:
                    continue
                expression = _filter_expression(step.get("clauses", []))
                rule = r"\texttt{" + expression + "}"
                mission_specific_rows.append(
                    f"    {mission} & {_tex(step['name'])} & {rule}{row_end}"
                )
    if rows:
        tables.extend(
            [
                r"\begin{longtable}{@{}p{4.1cm}p{10.2cm}@{}}",
                r"\caption{Common quality-filter rules.\label{tab:common_filters}}" + row_end,
                r"\toprule",
                r"\textbf{Filter} & \textbf{Rule on source catalogue}" + row_end,
                r"\midrule",
                r"\endfirsthead",
                r"\toprule",
                r"\textbf{Filter} & \textbf{Rule on source catalogue}" + row_end,
                r"\midrule",
                r"\endhead",
                r"\bottomrule",
                r"\endlastfoot",
                *rows,
                r"\end{longtable}",
            ]
        )
        if any("angle_diff_le" in repr(step.get("clauses", []))
               for steps in rule_steps.values() for step in steps):
            tables.append(
                r"\emph{Circular direction difference:} the shortest angular distance "
                r"on a 0--360 degree circle."
            )
    if mission_specific_rows:
        tables.extend(
            [
                r"\newpage",
                r"\begin{table}[H]",
                r"\centering",
                r"\caption{Mission-specific quality filters.}",
                r"\label{tab:mission_filters}",
                r"{\small",
                r"\begin{tabular}{@{}p{1.6cm}p{4.1cm}p{10.3cm}@{}}",
                r"\toprule",
                r"\textbf{Mission} & \textbf{Filter} & \textbf{Rule on source catalogue}"
                + row_end,
                r"\midrule",
                *mission_specific_rows,
                r"\bottomrule",
                r"\end{tabular}",
                r"}",
                r"\end{table}",
            ]
        )
    tables.append(r"\textbf{Sequential counts by mission (Remaining / removed):}")
    header = " & ".join(
        [r"\textbf{Filter}", *(r"\textbf{" + mission + "}" for mission in missions)]
    )
    count_rows = []
    for rule_id in ids:
        steps = [
            next((step for step in rule_steps[mission] if step["id"] == rule_id), None)
            for mission in missions
        ]
        varying_rule = (
            len({(step["name"], repr(step.get("clauses", []))) for step in steps if step}) > 1
            or any(step is None for step in steps)
        )
        name = (
            "SAR acquisition start-date cutoff (mission-specific)"
            if rule_id == "sar_start" and varying_rule
            else next((step["name"] for step in steps if step), rule_id)
        )
        counts = [
            f"{step['remaining']:,} / {step['removed']:,}" if step else "--" for step in steps
        ]
        count_rows.append("    " + " & ".join([_tex(name), *counts]) + row_end)
    widths = "".join("p{3cm}" for _ in missions)
    tables.extend(
        [
            rf"\begin{{longtable}}{{@{{}}p{{5cm}}{widths}@{{}}}}",
            r"\caption{Sequential quality-filter counts by mission.\label{tab:quality_filters}}"
            + row_end,
            r"\toprule",
            header + row_end,
            r"\midrule",
            r"\endfirsthead",
            r"\toprule",
            header + row_end,
            r"\midrule",
            r"\endhead",
            r"\bottomrule",
            r"\endlastfoot",
            *count_rows,
            r"\end{longtable}",
        ]
    )
    return "\n".join(tables)


def build_hscat_report(test_path, target_path, manifest, summary, figures, report_dir):
    """Fill the standard SOBA reference-report template without a merge export."""
    report_dir = Path(report_dir)
    stage_latex_assets(ASSET_DIR / "latex", report_dir)
    template_path = ASSET_DIR / "latex" / "hscat_test_template.tex"
    text = template_path.read_text(encoding="utf-8")
    schema = pq.read_schema(test_path)
    metadata = schema.metadata or {}
    variable = manifest["reference_variable"]
    value_column = "scat_windspeed" if variable == "windspeed" else "scat_winddirection"
    units = "m/s" if variable == "windspeed" else "degrees"
    figure_paths = {
        "COVERAGE_FIGURE": Path(figures["coverage"]).relative_to(report_dir).as_posix(),
        "MONTHLY_FIGURE": Path(figures["monthly_rows"]).relative_to(report_dir).as_posix(),
        "WINDSPEED_FIGURE": Path(figures["reference_distribution"])
        .relative_to(report_dir)
        .as_posix(),
    }
    row_end = r" \\"
    version_rows = []
    for source in manifest["sources"]:
        curated_name = Path(source["curated_path"]).name
        details = f"Saves {source['curated_rows']:,} rows after the configured curation rule(s)."
        version_rows.append(
            f"{_path(curated_name)} & {manifest['production_date']} & "
            f"{_tex(details)} {row_end}"
        )
    mission_rows = [
        f"{mission} & {count:,} \\\\" for mission, count in figures["mission_counts"].items()
    ]
    metadata_rows = []
    for key in (
        b"source scat",
        b"source ancillary datasets",
        b"library used to produce the parquet",
        b"library version",
        b"creation date",
    ):
        value = metadata.get(key, b"")
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        metadata_rows.append(f"\\texttt{{{_tex(key.decode())}}} & {_tex(value)} \\\\")

    descriptions = {
        "primary_key": "SLC SAFE identifier, imagette suffix, and reference coordinates.",
        "sar_time": "UTC SAR acquisition time, rounded down to whole seconds.",
        "sar_lat": "SAR WV imagette latitude in degrees.",
        "sar_lon": "SAR WV imagette longitude in degrees.",
        "sar_incidence_angle": "SAR incidence angle in degrees.",
        "sar_elevation_angle": "SAR elevation angle in degrees.",
        "sar_ground_heading": "SAR ground heading in degrees.",
        "sar_safe_slc": "SLC SAFE identifier, including the WV imagette suffix.",
        "sar_safe_ocn": "OCN SAFE identifier, including the WV imagette suffix.",
        "scat_lon": "HSCAT reference longitude in degrees.",
        "scat_lat": "HSCAT reference latitude in degrees.",
        "scat_time": "UTC HSCAT reference time, rounded down to whole seconds.",
        "scat_windspeed": "HSCAT wind speed in metres per second.",
        "scat_winddirection": "HSCAT wind direction in degrees clockwise from north.",
    }
    groups = [
        (
            "Identification",
            [
                name
                for name in ("primary_key", "sar_safe_slc", "sar_safe_ocn")
                if name in schema.names
            ],
        ),
        (
            "SAR geometry",
            [
                name
                for name in schema.names
                if name.startswith("sar_") and name not in {"sar_safe_slc", "sar_safe_ocn"}
            ],
        ),
        ("Reference (HSCAT)", [name for name in schema.names if name.startswith("scat_")]),
    ]
    column_rows = []
    for group, names in groups:
        if not names:
            continue
        column_rows.append(r"\multicolumn{3}{l}{\textbf{" + _tex(group) + "}}" + row_end)
        for name in names:
            column_rows.append(
                rf"\texttt{{{_tex(name)}}} & {_tex(schema.field(name).type)} & "
                rf"{_tex(descriptions.get(name, 'Source catalogue value.'))} \\"
            )
    incomplete_rows = manifest.get("excluded_incomplete_rows", 0)
    duplicate_rows = manifest.get("excluded_duplicate_key_rows", 0)
    excluded = [
        f"Incomplete or non-finite required output fields & {incomplete_rows:,}" + row_end,
        f"Excess rows after first-per-imagette/satellite selection & {duplicate_rows:,}" + row_end,
    ]
    stats = [
        ("Minimum", f"{summary['min']:.4g} {units}"),
        ("Maximum", f"{summary['max']:.4g} {units}"),
        ("Mean", f"{summary['mean']:.4g} {units}"),
        ("Median", f"{summary['median']:.4g} {units}"),
        ("Rows", f"{summary['rows']:,}"),
    ]
    stats_table = "\n".join(
        [
            r"\begin{table}[H]\centering",
            r"\caption{Summary statistics for " + _tex(value_column) + f" ({units})" + r"}",
            r"\label{tab:reference_stats}",
            r"\begin{tabular}{lr}",
            r"\toprule",
            r"\textbf{Statistic} & \textbf{Value}" + row_end,
            r"\midrule",
            *(f"{_tex(label)} & {_tex(value)} \\\\" for label, value in stats),
            r"\bottomrule",
            r"\end{tabular}",
            r"\end{table}",
        ]
    )
    target_path = Path(target_path)
    source_lines = "\n".join(
        rf"        \item \path|{Path(item['path']).name}| — {item['mission']}: "
        rf"{item['input_rows']:,} input rows, {item['curated_rows']:,} curated rows"
        for item in manifest["sources"]
    )
    process_steps = "\n".join(
        [
            r"    \item Apply the configured HSCAT curation rule independently "
            "to each source catalogue.",
            r"    \item Save one Curated Parquet per Sentinel-1 mission; "
            "retain each separate file in the run output.",
            r"    \item Combine Curated sources in temporary storage in sorted catalogue "
            "mission order, preserving original row order. Curated records remain native "
            "and quality-selected; remove the temporary file after export.",
            r"    \item After required-field integrity checks, retain the "
            "first acceptable matchup per SAR imagette and HSCAT satellite; "
            "no time, distance or angle ranking is applied. Strip the coordinate suffix "
            "from the native key basename to identify the imagette; append the satellite parsed "
            "from the reference identifier to produce globally unique output keys.",
            r"    \item Write TEST and TARGET in batches; verify their ordered, "
            "unique primary keys match.",
            r"    \item Generate the three figures and compile this report when requested.",
        ]
    )
    config_text = "\n".join(
        [
            "reference: HSCAT",
            f"variable: {variable}",
            f"production_date: {manifest['production_date']}",
            f"version: {manifest['version']}",
            f"target_file: {Path(target_path).name}",
            "curated_outputs: one separate Parquet per mission",
            "merged_curated_output: not exported",
            "temporary_merge: removed after TEST/TARGET generation",
            "duplicate_primary_key_policy: keep first acceptable row "
            "per SAR imagette and HSCAT satellite",
            f"accepted_rows: {summary['rows']:,}",
        ]
    )
    mapping = {
        "DATASET_NAME": "HSCAT windspeed" if variable == "windspeed" else "HSCAT winddirection",
        "SCOPE_TEXT": (
            "This document describes a Sentinel-1 WV TEST dataset paired with independent "
            "HSCAT reference observations. It documents the dataset contents and the method "
            "used to produce TEST and TARGET files from the co-aligned catalogues."
        ),
        "FILE_VERSION_ROW": "\n".join(version_rows),
        "GENERAL_DESCRIPTION": (
            "Sentinel-1 WV observations paired with KNMI HSCAT HY-2 25 km reference data. "
            "The run applies mission-specific SAR time bounds alongside configured wind-speed, "
            "circular wind-direction, ice-probability and reference-flag rules."
        ),
        "COVERAGE_DESCRIPTION": (
            "The map shows every retained Sentinel-1 WV matchup location in the TEST dataset."
        ),
        **figure_paths,
        "COVERAGE_CAPTION": (
            "Global spatial distribution of all retained Sentinel-1 WV matchups in the TEST "
            "dataset, shown in one color."
        ),
        "MONTHLY_DESCRIPTION": (
            "This figure shows retained TEST rows by SAR acquisition month, stacked by mission."
        ),
        "MONTHLY_CAPTION": "Monthly count of retained HSCAT matchups, stacked by mission.",
        "REFERENCE_DESCRIPTION": (
            f"Colored step lines compare HSCAT {variable} by mission using shared bins. "
            "Each mission is normalized to a percentage of its TEST rows, and the legend "
            "shows each sample size."
        ),
        "REFERENCE_CAPTION": (
            f"Normalized HSCAT {variable} step histograms by mission."
        ),
        "REFERENCE_STATS_TABLE": stats_table,
        "TEST_COLUMNS": "\n".join(column_rows),
        "SAMPLING_NOTE": (
            r"\noindent\textbf{Sampling point.} SAR geometry and HSCAT reference values "
            r"come from the co-aligned source catalogue."
        ),
        "METADATA_ROWS": "\n".join(metadata_rows),
        "SELECTED_PRODUCTS": (
            "The run uses the HSCAT reference product listed for each mission below."
        ),
        "SOURCE_FILES": source_lines,
        "REFERENCE_PRODUCT": (
            "KNMI-HSCAT-HY2-25km, HSCAT wind speed in metres per second."
            if variable == "windspeed"
            else "KNMI-HSCAT-HY2-25km, HSCAT wind direction in degrees."
        ),
        "SELECTION_INTRO": (
            "The catalogues use mission-specific SAR date bounds; no additional "
            "spatial selection is applied."
        ),
        "GEO_CRITERION": "No additional geographic filter is applied.",
        "TIME_CRITERION": (
            "SAR acquisition timestamps must satisfy the mission-specific UTC "
            "date bounds listed below."
        ),
        "KEY_FORMULA": r"the source \texttt{primary\_key} field",
        "FILTER_INTRO": (
            "The configured quality filters are applied independently to each mission catalogue."
        ),
        "QUALITY_FILTER_TABLES": _filter_tables(manifest["sources"]),
        "MISSION_ROWS": "\n".join(mission_rows),
        "ROW_COUNT": f"{summary['rows']:,}",
        "EXCLUSION_ROWS": "\n".join(excluded),
        "PROCESS_STEPS": process_steps,
        "COMMAND_INTRO": "The dataset was generated with the repository recipe-based command.",
        "COMMAND": r"python -m soba_reference_repo.create_test_dataset --recipe recipe.toml",
        "CONFIG_INTRO": (
            "Run settings follow. Curated inputs remain separate; the merged file is not exported."
        ),
        "RUN_CONFIG": config_text,
    }
    for key, value in mapping.items():
        text = text.replace(f"@@{key}@@", str(value))
    leftovers = re.findall(r"@@[A-Z_]+@@", text)
    if leftovers:
        raise ValueError(f"unfilled HSCAT report template fields: {sorted(set(leftovers))}")
    tex_path = report_dir / f"{Path(test_path).stem}.tex"
    tex_path.write_text(text, encoding="utf-8")
    return tex_path
