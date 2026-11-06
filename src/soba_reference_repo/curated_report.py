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


# Documented definitions: supplied S1D SWOT catalogue PDF, Table 4, printed pages 7-8.
# Measurement-file paths were also checked against actual source-column values.
COMMON_COLUMN_DESCRIPTIONS = {
    "sar_path_ocn": "Path to the Sentinel-1 OCN measurement file.",
    "sar_path_slc": "Path to the Sentinel-1 SLC measurement file.",
    "s1_sensor": "SAR mission identifier (e.g., S1D).",
    "s1_wv_mode": "WV sub-swath (wv1 or wv2).",
    "sar_distance_to_coast": "Distance from centroid to nearest coastline (km, from NASA data).",
    "ww3_hs": "WW3 model significant wave height (m).",
    "oswTotalHs": "SWH from ESA S1 OCN processor (m).",
    "oswTotalHsStdev": "Standard deviation of SWH (m).",
    "oswWindSpeed": "Wind speed from OCN processor (m/s).",
    "oswEcmwfWindSpeed": "ECMWF wind speed (m/s).",
    "oswSkew": "Wave skewness.",
    "oswKurt": "Wave kurtosis.",
    "oswNv": "Number of valid waves.",
    "mean_rainrate_IMERG": "Mean rain rate from IMERG (mm/h).",
    "max_rainrate_IMERG": "Maximum rain rate from IMERG (mm/h).",
}
SWOT_COLUMN_DESCRIPTIONS = {
    "ref_time_delta": "Time difference SAR - SWOT (seconds).",
    "ref_distance_delta": "Great-circle distance SAR centroid to SWOT pixel (km).",
    "ref_hs_alti_closest": "SWH from SWOT Karin nadir altimeter (m).",
    "ref_mean_hs_karin": "Mean SWH from SWOT Karin across the segment (m).",
    "ref_param_count": "Number of SWOT pixels in the overlap (estimated from overlap_pct).",
    "ref_flag": "Quality flag: clear/rain/ice.",
    "overlap_pct": "Percentage of SAR imagette overlapped by SWOT (0-100).",
    "swot_dynamic_ice_flag": "SWOT dynamic ice flag.",
    "swot_rain_flag": "SWOT rain flag.",
    "swot_cycle": "SWOT cycle number.",
    "swot_pass": "SWOT pass number.",
    "swot_crid": "SWOT CRID (PGD0/PID0).",
}
# Field-name interpretations requested by the owner; do not invent units or flag codes.
INFERRED_COLUMN_DESCRIPTIONS = {
    "sar_path_safe_slc": "Path to the Sentinel-1 SLC SAFE product (inferred).",
    "swot_path": "Path to the SWOT source product (inferred).",
    "swot_path_hpc": "SWOT product path in the processing environment (inferred).",
    "oswLandCoverage": "Land coverage reported by the OCN processor (inferred; scale unverified).",
    "oswDepth": "Water depth reported by the OCN processor (inferred; units unverified).",
    "s1_subpath": "Sentinel-1 product subpath (inferred).",
    "s1_path": "Path to the Sentinel-1 source product (inferred).",
    "s1_path_hpc": "Sentinel-1 product path in the processing environment (inferred).",
    "std_KR_swh": "Standard deviation of KaRIn significant wave height (inferred).",
    "median_KR_swh": "Median KaRIn significant wave height (inferred).",
    "mad_KR_swh": "Median absolute deviation of KaRIn significant wave height (inferred).",
    "imagette_number": "Imagette number within the Sentinel-1 product (inferred).",
    "legacy_usage": "Legacy-use indicator retained from the source (inferred; codes unverified).",
    "swh_swot_l3_20km": "SWOT Level-3 SWH over a 20 km neighborhood (inferred).",
    "swh_swot_l3_20km_std": "SWH standard deviation over that 20 km neighborhood (inferred).",
    "swh_swot_l3_20km_nvalid": "Valid-sample count in that 20 km neighborhood (inferred).",
    "swh_swot_l3_center_i": "First grid index of the Level-3 extraction center (inferred).",
    "swh_swot_l3_center_j": "Second grid index of the Level-3 extraction center (inferred).",
    "swh_swot_l3_status": "Level-3 extraction status (inferred; codes unverified).",
    "ref_distance_km": "Separation between SAR and reference locations (inferred; km).",
    "ecmwf_source": "Source identifier or path for ECMWF ancillary data (inferred).",
    **{f"class_{i}": f"Scene-class label {i} (inferred)." for i in (1, 2, 3)},
    **{f"prob_{i}": f"Probability associated with class_{i} (inferred; scale unverified)."
        for i in (1, 2, 3)},
}

# Human-readable rationale for each curation filter: the scientific goal
# ("why this filter exists") and the mechanism ("how it is applied").
# Kept independent of the recipe so the same documentation applies to every
# release that enables the corresponding rule. Keys are shared across the
# HSCAT, altimeter and SWOT curated documents (all use build_curated_report),
# so a given filter is described identically wherever it appears.
# ``sar_start``/``sar_end`` are rendered by the generic date-selection text
# because their window is mission-specific.
FILTER_DETAILS = {
    "ecmwf_scat_speed_difference": (
        "retain only matches for which the independent scatterometer and the "
        "meteorological (ECMWF) analysis agree on wind strength, so the reference used "
        "for evaluation is not an outlier relative to the large-scale wind field.",
        "keep a match when the absolute difference between the ECMWF wind "
        "speed and the HSCAT wind speed is at most the configured threshold (2 m/s); "
        "matches with a larger disagreement are removed.",
    ),
    "ecmwf_scat_direction_difference": (
        "retain only matches for which the independent scatterometer and the "
        "ECMWF analysis agree on wind direction, avoiding directions that a single "
        "measurement could not determine reliably.",
        "compute the circular (360-degree-wrapped) difference between the "
        "ECMWF wind direction and the HSCAT wind direction; keep a match when it is at "
        "most the configured threshold (30 degrees).",
    ),
    "zero_ice_probability": (
        "keep open-water observations only, because the wind retrieval is not "
        "valid over ice.",
        "keep a match when the HSCAT ice-probability field for the "
        "collocated pixel is exactly zero.",
    ),
    "zero_ref_flag": (
        "drop HSCAT pixels that the KNMI processor has flagged as doubtful, while "
        "keeping the reliable low-wind cases.",
        "keep a match when the HSCAT quality flag is one of the accepted "
        "values (0 or 2048; the 2048 bit marks small winds at most 3 m/s that are still "
        "valid). Any other combination of doubt bits is removed.",
    ),
    "quality_disclaimer_filter": (
        "exclude Sentinel-1 observations that ESA has formally documented as "
        "impacted by a degradation, so that known-bad acquisitions never enter the "
        "curated set.",
        "load the ESA Sentinel-1 quality-disclaimer impacted-product "
        "database (collected at Ifremer from the ESA SAR-MPC disclaimers). A match is "
        "removed when the bare name of its SLC SAFE or of its OCN SAFE is listed in that "
        "database. Only explicit product names are used; glob patterns and period-only "
        "disclaimers are ignored.",
    ),
    "imagette_direction_consistency": (
        "ensure the reference wind direction is unambiguous for each SAR imagette, "
        "so the evaluation target is not an artefact of mixing several HSCAT "
        "observations.",
        "for every WV imagette, compute the circular standard deviation of "
        "the HSCAT wind directions of its eligible references; when it exceeds the "
        "configured threshold (10 degrees), all of the imagette's matches are removed. "
        "The aim is reference consistency; the threshold is a chosen setting whose "
        "scientific validation is not documented here.",
    ),
    # -- altimeter reference filters (WV x ALTI) --
    "no_rain": (
        "exclude rain-contaminated observations, because rain degrades both the "
        "SAR wave measurement and the altimeter retrieval.",
        "keep a match when the IMERG rain rate at the reference time is "
        "exactly zero.",
    ),
    "imerg_rain": (
        "exclude rain-contaminated observations, because rain degrades the "
        "reference retrieval.",
        "keep a match when the maximum IMERG rain rate associated with the "
        "match is at most the configured threshold.",
    ),
    "alti_ww3_30pct": (
        "remove altimeter wave heights that are physically inconsistent with the "
        "WW3 model, which catches isolated altimeter outliers without discarding the "
        "independent SAR product.",
        "keep a match when the altimeter significant wave height deviates "
        "from the WW3 model Hs by at most the configured relative tolerance (30 %); "
        "larger deviations are removed.",
    ),
    "cfosat_pre2024": (
        "exclude CFOSAT observations from the mission commissioning period, when "
        "the data quality is not representative of normal operations.",
        "remove rows whose reference file is a CFOSAT product (CCI or "
        "CMEMS) and whose acquisition time falls in the configured pre-2024 window.",
    ),
    "hy2b_frozen_20230219": (
        "exclude a known altimeter data anomaly so a frozen feed does not bias "
        "the curated set.",
        "remove rows whose reference file is a HY2B product and whose "
        "acquisition time falls in the configured anomaly window (2023-02-19, when the "
        "CMEMS L3 VAVH feed was frozen).",
    ),
    # -- SWOT reference filters (WV x SWOT) --
    "sea_proxy": (
        "keep open-water observations away from the coast, where the wave "
        "retrieval is valid.",
        "keep a match when the OCN land coverage is zero and the distance "
        "from the coast is strictly positive.",
    ),
    "no_dynamic_ice": (
        "exclude ice-contaminated observations.",
        "keep a match when the SWOT dynamic-ice flag is zero.",
    ),
    "time_present": (
        "ensure a time difference between the SAR and reference acquisitions "
        "exists, so the colocation can be evaluated.",
        "keep a match when the reference time difference is present (not "
        "missing).",
    ),
    "karin_positive": (
        "keep only physically meaningful SWOT wave heights.",
        "keep a match when the SWOT KaRIn significant wave height is "
        "strictly positive.",
    ),
    "ocn_positive": (
        "keep only physically meaningful Sentinel-1 wave heights.",
        "keep a match when the Sentinel-1 OCN significant wave height is "
        "strictly positive.",
    ),
    "altimeter_positive": (
        "keep only physically meaningful altimeter wave heights.",
        "keep a match when the nearest altimeter significant wave height is "
        "strictly positive.",
    ),
    "ww3_positive": (
        "keep only physically meaningful model wave heights.",
        "keep a match when the WW3 model significant wave height is "
        "strictly positive.",
    ),
    "classification_present": (
        "ensure a scene classification is available for the match.",
        "keep a match when at least one scene-class probability is present "
        "(not missing).",
    ),
    "time_under_two_hours": (
        "keep only contemporaneous SAR and reference observations.",
        "keep a match when the absolute time difference between the "
        "acquisitions is less than the configured maximum (two hours).",
    ),
    "full_overlap": (
        "keep only matches with complete spatial overlap between the SAR "
        "imagette and the reference footprint.",
        "keep a match when the overlap percentage is at least 100 %.",
    ),
    "no_swot_rain_flag": (
        "exclude SWOT observations flagged as affected by rain.",
        "keep a match when the SWOT rain flag is zero.",
    ),
    "accepted_classes": (
        "keep only ocean scene classes for which the wave retrieval is "
        "reliable.",
        "keep a match when the S1 scene-class label is one of the accepted "
        "classes (AF, BS, MCC, OF, POS, RC, WS).",
    ),
}


def _rows(rows):
    return "\n".join(" & ".join(row) + r" \\" for row in rows)


def _source_label(source):
    """Short unique label when a mission has several catalogues (ALTI CCI + CMEMS)."""
    name = Path(source["curated_path"]).name
    if "CMEMS-ALTI-L3-VAVH" in name:
        return f"{source['mission']} CMEMS-VAVH"
    if "CCI-SEATSTATE" in name:
        return f"{source['mission']} CCI-SeaState"
    return source["mission"]


def _doc_release_date(product, production_date):
    """Real release date for the Version History table (was a hardcoded ``\\DTMtoday``).

    The HSCAT and SWOT curated documents were first published in
    ``/scale/.../dataset_description/`` on 2026-10-16 (their own history rows carry that
    date), so they keep it. The ALTI document is new, so its release date is the run's
    production date, formatted ``YYYY-MM-DD``.
    """
    if product != "ALTI":
        return "2026-10-16"
    date = str(production_date)
    return f"{date[:4]}-{date[4:6]}-{date[6:8]}"


def render_column_table(schema, descriptions, *, caption, label, longtable_threshold=35):
    """Render one Column/Type/Description table for a single (per-database) schema.

    Shares the curated doc's grouping (Identification / SAR geometry / Reference /
    Ancillary) and description fallback, and switches to a ``longtable`` for wide
    schemas. Used by the catalogue report to describe the CCI and CMEMS catalogues
    separately (their column sets differ).
    """
    groups = {g: [] for g in ("Identification", "SAR geometry", "Reference", "Ancillary")}
    for field in schema:
        name = field.name
        if name == "primary_key" or name.startswith(
            ("sar_safe_", "sar_path_", "_curation_", "__index_")
        ):
            group = "Identification"
        elif name.startswith("sar_"):
            group = "SAR geometry"
        elif name.startswith(("ref_", "scat_", "swot_")):
            group = "Reference"
        else:
            group = "Ancillary"
        groups[group].append(field)
    rows = []
    for group, fields in groups.items():
        if not fields:
            continue
        rows.append(r"\multicolumn{3}{l}{\textbf{" + group + "}} " + chr(92) * 2)
        rows.append(r"\midrule")
        for field in fields:
            description = descriptions.get(
                field.name, "Value associated with this matched observation.")
            rows.append(" & ".join([
                _tex(field.name), _tex(str(field.type)),
                _tex(description.replace("co-aligned", "matched")),
            ]) + r" \\")
    spec = (r"@{}>{\raggedright\arraybackslash}p{4.1cm}"
            r">{\raggedright\arraybackslash}p{2.6cm}"
            r">{\raggedright\arraybackslash}p{8.1cm}@{}")
    header = r"\textbf{Column} & \textbf{Type} & \textbf{Description} " + chr(92) * 2
    if len(schema) <= longtable_threshold:
        return "\n".join([
            r"\begin{table}[H]\centering",
            r"\caption{" + caption + "}",
            r"\label{" + label + "}",
            r"{\footnotesize",
            r"\begin{tabular}{" + spec + "}",
            r"\toprule", header, r"\midrule", *rows, r"\bottomrule",
            r"\end{tabular}", "}", r"\end{table}",
        ])
    return "\n".join([
        r"{\small\setlength{\tabcolsep}{4pt}",
        r"\begin{longtable}{" + spec + "}",
        r"\caption{" + caption + r"\label{" + label + "}}" + chr(92) * 2,
        r"\toprule", header, r"\midrule", r"\endfirsthead",
        r"\caption[]{" + caption + " (continued).}" + chr(92) * 2,
        r"\toprule", header, r"\midrule", r"\endhead",
        r"\midrule", r"\multicolumn{3}{r}{Continued on next page.}" + chr(92) * 2,
        r"\endfoot", r"\bottomrule", r"\endlastfoot",
        *[line.rstrip() + "*" if line.startswith(r"\multicolumn") else line for line in rows],
        r"\end{longtable}", "}",
    ])


def build_curated_report(*, title, platform, instrument, reference, variable,
                         document_stem, recipe, manifest, figures, summary, observation_path,
                         field_descriptions, version_extra=None,
                         coverage_extra="", monthly_extra="", distribution_extra="",
                         filter_removal_figures="", template="curated_template.tex",
                         scope_text=None, general_description=None,
                         column_caption="Columns of the Curated Parquet files",
                         column_tables=None, dataflow_extra="", file_version_rows=None):
    """Preserve the supplied PDF's cover, section order, tables, and SOBA style.

    ``version_extra`` adds a per-database source-version note (e.g. the distinct
    CCI and CMEMS catalogue versions) after the Catalogue Columns table; the
    ``*_extra`` figure blocks insert additional ALTI figures. All default to the
    empty/None no-op so the SWOT and HSCAT documents are unchanged.
    """
    sources = sorted(manifest["sources"],
                     key=lambda item: (item["mission"], Path(item["curated_path"]).name))
    source_schemas = [pq.read_schema(item["curated_path"]) for item in sources]
    duplicate_missions = len({item["mission"] for item in sources}) != len(sources)
    source_labels = [_source_label(item) if duplicate_missions else item["mission"]
                     for item in sources]
    metadata = {}
    for label, schema in zip(source_labels, source_schemas):
        attrs = {key.decode(): value.decode("utf-8", errors="replace")
                 for key, value in (schema.metadata or {}).items()
                 if key not in {b"pandas", b"ARROW:schema", b"utils_meta"}}
        if b"utils_meta" in (schema.metadata or {}):
            attrs.update(json.loads(schema.metadata[b"utils_meta"]).get("attrs", {}))
        metadata[label] = attrs
    native_names = set().union(*(set(schema.names) for schema in source_schemas))
    catalogue_fields = {
        (field.name, str(field.type)): field
        for schema in source_schemas for field in schema
    }
    names = list(dict.fromkeys(field.name for field in catalogue_fields.values()))
    hscat = "HSCAT" in reference
    alti = not hscat and reference.lower().startswith("alti")
    product = "HSCAT" if hscat else ("ALTI" if alti else "SWOT")
    doc_release_date = _doc_release_date(product, recipe["production_date"])
    selected_variable = recipe.get("reference_variable", "swh")
    rename = recipe.get("rename_columns") or {}
    if hscat:
        native = "ref_param_1" if selected_variable == "winddirection" else "ref_param_2"
        value_column = rename.get(native, native)
    elif alti:
        value_column = "ref_hs_alti_closest"
    else:
        value_column = "ref_mean_hs_karin"
    units = ("degrees" if selected_variable == "winddirection" else "m/s") if hscat else "m"
    if hscat:
        reference_label = ("HSCAT wind directions" if selected_variable == "winddirection"
                           else "HSCAT wind speeds")
    elif alti:
        reference_label = "altimeter significant wave heights"
    else:
        reference_label = "SWOT significant wave heights"
    dataset_name = product
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
        "primary_key": "Source matchup identifier retained in the Curated catalogue.",
        "__index_level_0__": "Original source DataFrame index, retained as supplied.",
    })
    for name, description in {
        **INFERRED_COLUMN_DESCRIPTIONS, **COMMON_COLUMN_DESCRIPTIONS,
        **({} if hscat else SWOT_COLUMN_DESCRIPTIONS),
    }.items():
        descriptions.setdefault(name, description)
    groups = {label: [] for label in ("Identification", "SAR geometry", "Reference", "Ancillary")}
    for field in catalogue_fields.values():
        name = field.name
        if name == "primary_key" or name.startswith(
            ("sar_safe_", "sar_path_", "_curation_", "__index_")
        ):
            group = "Identification"
        elif name.startswith("sar_"):
            group = "SAR geometry"
        elif name.startswith(("ref_", "scat_", "swot_")):
            group = "Reference"
        else:
            group = "Ancillary"
        groups[group].append(field)
    column_lines = []
    for group, fields in groups.items():
        if not fields:
            continue
        column_lines.append(r"\multicolumn{3}{l}{\textbf{" + group + "}} " + chr(92) * 2)
        column_lines.append(r"\midrule")
        column_lines.extend(_rows([
            (_tex(field.name), _tex(field.type),
             _tex(descriptions.get(field.name, "Value associated with this matched observation.")
                  .replace("co-aligned", "matched")))
        ]) for field in fields)
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
        ("altimeter significant wave height (CCI SeaState V5 and CMEMS ALTI-L3-VAVH)"
         if alti else
         "SWOT KaRIn Level-2 WindWave D0 (PGD0/PID0), distributed by NASA/JPL PO.DAAC")
    )
    disabled = {(rule["id"]) for item in recipe.get("catalogues", [])
                for rule in item.get("resolved_rules", item.get("rules", []))
                if not rule.get("enabled", True)}
    active = [
        (label, step)
        for label, source in zip(source_labels, sources)
        for step in source.get("quality_filter_steps", source.get("steps", []))
        if step.get("status") not in {"disabled", "skipped"}
        and step["id"] not in disabled
    ]
    public_checks = {
        "ecmwf_scat_speed_difference": "wind-speed agreement with meteorological estimates",
        "ecmwf_scat_direction_difference": "wind-direction agreement with meteorological estimates",
        "zero_ice_probability": "ice-free reference observations",
        "zero_ref_flag": "reference quality checks",
        "quality_disclaimer_filter": "excluded products with a documented quality disclaimer",
        "imagette_direction_consistency": "consistent reference directions within each SAR image",
        "sea_proxy": "open-water observations away from the coast",
        "no_rain": "rain screening using IMERG",
        "no_dynamic_ice": "ice-free observations",
        "time_present": "valid time differences",
        "karin_positive": "positive SWOT wave heights",
        "ocn_positive": "positive Sentinel-1 ocean-product wave heights",
        "altimeter_positive": "positive nearby altimeter wave heights",
        "alti_ww3_30pct": "altimeter Hs within 30 % of the WW3 model Hs",
        "cfosat_pre2024": "CFOSAT observations before 2024-01-01 excluded",
        "hy2b_frozen_20230219": "HY2B 2023-02-19 frozen-value rows excluded",
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
    for label in source_labels:
        windows = []
        for step_label, step in active:
            if step_label != label:
                continue
            for clause in step.get("clauses", []):
                if clause[0] == "sar_time" and clause[1] in {"date_gt", "date_lt"}:
                    boundary = "after" if clause[1] == "date_gt" else "before"
                    windows.append(f"strictly {boundary} {clause[2]} at 00:00 UTC")
        dates.append((label, "; ".join(windows) or "no additional date restriction"))
    provenance = {
        "sar_product_provider": sar_product, "reference_product_provider": ref_product,
        "software_version": library_version(), "recipe_snapshot": "recipe.toml",
        "command": "soba_curate_dataset --recipe recipe.toml",
        "date_selection_rationale": "Supplied evaluation windows; no further rationale recorded.",
        "curated_schema_files": [Path(item["curated_path"]).name for item in sources],
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
        *[(f"{label} source catalogue", _path(Path(item["path"]).name))
          for label, item in zip(source_labels, sources)],
        *metadata_rows,
    ]
    source_rows = [
        (label, f"{item['input_rows']:,}", f"{item['curated_rows']:,}",
         f"{item['input_rows'] - item['curated_rows']:,}")
        for label, item in zip(source_labels, sources)
    ]
    # Per-database source versions (ALTI carries distinct CCI + CMEMS catalogues,
    # so a single "Dataset version" row is ambiguous). Group sources by the
    # catalogue product token in their filename and print each product's version.
    catalogue_versions = ""
    if version_extra:
        product_re = re.compile(r"_(SV|DV|SH|DH)_([A-Za-z0-9._-]+)_(\d+\.\d+)$")
        by_product = {}  # token -> (version, {missions})
        for item in sources:
            match = product_re.search(Path(item["path"]).stem)
            if not match:
                continue
            token, version = match.group(2), match.group(3)
            entry = by_product.setdefault(token, (version, set()))
            entry[1].add(item["mission"])
        rows = []
        for token, (version, missions) in by_product.items():
            rows.append((
                _tex(f"{token} (source catalogue)"),
                _tex(", ".join(sorted(missions))),
                f"{version} (source) / {recipe['version']} (curated)",
            ))
        if rows:
            catalogue_versions = "\n".join([
                r"\noindent\textbf{Source catalogue versions.}",
                "Each reference database ships its own version; the curated-set",
                "version is shared across all catalogues.",
                r"\begin{table}[H]\centering",
                r"\caption{Source catalogue versions by reference database.}",
                r"\label{tab:catalogue_versions}",
                r"{\small",
                r"\begin{tabular}{"
                r"@{}>{\raggedright\arraybackslash}p{6.0cm}"
                r">{\raggedright\arraybackslash}p{3.2cm}"
                r">{\raggedright\arraybackslash}p{5.0cm}@{}}",
                r"\toprule",
                (r"\textbf{Reference database} & \textbf{Catalogues}"
                 r" & \textbf{Version} " + chr(92) * 2),
                r"\midrule",
                _rows(rows),
                r"\bottomrule",
                r"\end{tabular}",
                "}",
                r"\end{table}",
            ])
    # Keep each source's filter order, including mission-specific date rules.
    steps_by_label = {label: {} for label in source_labels}
    predecessors = {}
    for label, source in zip(source_labels, sources):
        previous = None
        for step in source.get("quality_filter_steps", source.get("steps", [])):
            if step.get("status") in {"disabled", "skipped"} or step["id"] in disabled:
                continue
            rule_id = step["id"]
            steps_by_label[label][rule_id] = step
            predecessors.setdefault(rule_id, set())
            if previous is not None:
                predecessors[rule_id].add(previous)
            previous = rule_id
    try:
        step_groups = [
            [steps_by_label[label].get(rule_id) for label in source_labels]
            for rule_id in TopologicalSorter(predecessors).static_order()
        ]
    except CycleError:
        # Repeat conflicting rows rather than reorder either source's filters.
        pending = [list(steps_by_label[label].values()) for label in source_labels]
        step_groups = []
        while any(pending):
            rule_id = next(steps[0]["id"] for steps in pending if steps)
            step_groups.append([
                steps.pop(0) if steps and steps[0]["id"] == rule_id else None
                for steps in pending
            ])
    def _count_cell(step):
        """A sequential-count cell. ALTI adds the percent removed; the shared
        SWOT/HSCAT documents keep the plain ``remaining / removed`` wording."""
        if not step:
            return "--"
        if not alti:
            return f"{step['remaining']:,} / {step['removed']:,}"
        before = step.get("before", step["remaining"] + step["removed"])
        pct = step["removed"] / before if before else 0.0
        pct_str = f"{pct:.1%}".replace("%", r"\%")
        return f"{step['remaining']:,} / {step['removed']:,} ({pct_str})"

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
        count_rows.append((_tex(name), *(_count_cell(step) for step in steps)))
    if len(sources) > 4:
        # ALTI release: one column per catalogue (CCI + CMEMS per mission).
        count_spec = (r"@{}>{\raggedright\arraybackslash}p{3.6cm}"
                      + r">{\raggedright\arraybackslash}p{1.7cm}" * len(sources) + r"@{}")
    else:
        count_spec = r"@{}>{\raggedright\arraybackslash}p{4.8cm}" + (
            r">{\raggedright\arraybackslash}p{2.7cm}" * len(sources)) + "@{}"
    count_header = _rows([("Filter", *source_labels)])
    sequential_counts = "\n".join([
        r"\begin{table}[H]\centering",
        r"\caption{Sequential quality-filter counts by mission (remaining / removed).}",
        r"\label{tab:quality_filters}",
        r"{\small\setlength{\tabcolsep}{4pt}",
        r"\begin{tabular}{" + count_spec + "}",
        r"\toprule", count_header, r"\midrule", _rows(count_rows),
        r"\bottomrule", r"\end{tabular}", "}", r"\end{table}",
    ])
    # One descriptive sub-section per active filter: its goal and mechanism.
    # The sequential counts table above already carries the per-mission
    # remaining/removed figures, so the details below stay qualitative.
    detail_blocks = []
    date_block_added = False
    for steps in step_groups:
        present = [step for step in steps if step]
        if not present:
            continue
        rule_id = present[0]["id"]
        name = present[0]["name"]
        if rule_id in {"sar_start", "sar_end"}:
            # sar_start and sar_end are both date windows: one merged sub-section.
            if date_block_added:
                continue
            date_block_added = True
            windows = "; ".join(
                f"{label}: {window}"
                for label, window in dates
                if window != "no additional date restriction"
            )
            detail_blocks.append((
                _tex("Acquisition-date selection (mission-specific)"),
                "restrict each mission to the acquisition window supplied for this "
                "release, so the curated set reflects the intended evaluation period "
                "rather than the full satellite lifetimes.",
                "keep a match when its SAR acquisition time falls inside the "
                "mission's window. Windows: " + windows + ".",
            ))
            continue
        detail = FILTER_DETAILS.get(rule_id)
        if not detail:
            # Rules without a curated-documentation entry: describe generically.
            detail = (
                "select reliable reference observations for evaluation.",
                _tex(name) + ".",
            )
        detail_blocks.append((_tex(name), detail[0], detail[1]))
    filter_details = "\n\n".join(
        "\n".join([
            r"\subsubsection{" + title + "}",
            r"\noindent\textbf{Goal.} " + _tex(goal) + "\n",
            r"\noindent\textbf{Mechanism.} " + _tex(mechanism),
        ])
        for title, goal, mechanism in detail_blocks
    )
    if detail_blocks:
        filter_details = (
            r"\subsection{Details of the applied filters}"
            + "\nEach filter below lists its goal (the quality property it protects) and "
              "its mechanism (the rule applied to the matched observations). "
              "Per-mission remaining/removed counts are given in "
              r"Table~\ref{tab:quality_filters}."
            + "\n\n" + filter_details
        )
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
        "DIRECTION_STD_NOTE": (
            r"\noindent The direction-STD filter excludes WV imagettes whose eligible HSCAT "
            "references disagree, avoiding an ambiguous wind-direction target. All remaining "
            "matchups for an imagette are rejected when their circular STD exceeds the "
            "configured threshold. The aim is reference consistency; the threshold is a chosen "
            "setting whose scientific validation is not documented here."
            if hscat and any(clause[1] == "imagette_circstd_le"
                             for _, step in active for clause in step.get("clauses", []))
            else ""
        ),
        "DATAFLOW_FIGURE": f"{assets}/sobadataflow.png",
        "STYLE_FILE": f"{assets}/soba", "GRAPHICS_DIRECTORY": assets,
        "DATASET_NAME": _tex(dataset_name), "DOCUMENT_PRODUCT": product,
        "DOC_RELEASE_DATE": _tex(doc_release_date),
        "REFERENCE_LABEL": reference_label,
        "DISTRIBUTION_TITLE": (
            "HSCAT Wind-Speed and Wind-Direction Distributions" if hscat
            else ("Altimeter Significant Wave-Height Distribution" if alti
                  else "SWOT Significant Wave-Height Distribution")
        ),
        "SCOPE_TEXT": scope_text or (
            "A curated set of Sentinel-1 Wave Mode observations matched to altimeter "
            "significant-wave-height measurements (CCI SeaState V5 and CMEMS ALTI-L3-VAVH) "
            "for independent algorithm evaluation."
            if alti else
            f"A curated set of Sentinel-1 Wave Mode observations matched to "
            f"{product} reference measurements for independent algorithm evaluation."
        ),
        "FILE_VERSION_ROW": (
            "\n".join(file_version_rows)
            if file_version_rows is not None
            else _rows([
                (_path(Path(item["curated_path"]).name), recipe["production_date"],
                 f"Contains {item['curated_rows']:,} retained matches.") for item in sources
            ])
        ),
        "GENERAL_DESCRIPTION": _tex(
            general_description or
            f"{sar_product} observations are first matched in space and time to {ref_product}, "
            "then additional filters select reliable, consistent matches for evaluation."
        ),
        "COVERAGE_DESCRIPTION": (
            "The heatmap shows retained matches per 1-degree-wide hexagonal cell on a logarithmic "
            "color scale. Matches without finite coordinates are omitted."
        ),
        "COVERAGE_FIGURE": f"{assets}/{Path(paths[0]).name}",
        "COVERAGE_CAPTION": (
            f"Global density of retained Sentinel-1 / {product} matches."
        ),
        "COVERAGE_EXTRA": coverage_extra,
        "MONTHLY_DESCRIPTION": "Retained matches by SAR acquisition month, stacked by mission.",
        "MONTHLY_FIGURE": f"{assets}/{Path(paths[1]).name}",
        "MONTHLY_CAPTION": f"Monthly count of retained {product} matches, stacked by mission.",
        "MONTHLY_EXTRA": monthly_extra,
        "DISTRIBUTION_EXTRA": distribution_extra,
        "FILTER_REMOVAL_FIGURES": filter_removal_figures,
        "CATALOGUE_VERSIONS": catalogue_versions,
        "REFERENCE_DESCRIPTION": (
            "Colored lines show HSCAT wind speed (top) and wind direction (bottom) by mission. "
            if hscat else f"Colored lines show {_tex(variable)} by mission. "
        ) + "Each mission is normalized to its own finite sample count, shown in the legend; "
            "percentages do not compare absolute counts.",
        "WINDSPEED_FIGURE": f"{assets}/{Path(paths[2]).name}",
        "REFERENCE_CAPTION": f"Normalized {product} distributions of retained matches by mission.",
        "DISTRIBUTION_FIGURES": "\n".join(
            r"\begin{figure}[H]\centering" + "\n"
            + r"\includegraphics[width=0.9\textwidth]{" + assets + "/" + Path(path).name + "}\n"
            + r"\caption{" + caption + "}\n"
            + r"\label{fig:ref_distribution" + ("" if index == 0 else "_direction") + "}\n"
            + r"\end{figure}"
            for index, (path, caption) in enumerate(
                [(paths[2], "Normalized HSCAT wind-speed distribution by mission."),
                 (paths[3], "Normalized HSCAT wind-direction distribution by mission.")]
                if hscat else [(paths[2], f"Normalized {product} distributions by mission.")]
            )
        ),
        "REFERENCE_STATS_TABLE": stats_table, "TEST_COLUMNS": "\n".join(column_lines),
        "SAMPLING_NOTE": r"\noindent\textbf{Sampling point.} SAR geometry and " + product +
                         " values describe observations matched in space and time.",
        "METADATA_ROWS": _rows(metadata_rows), "SAR_PRODUCT": _tex(sar_product),
        "REFERENCE_PRODUCT": _tex(ref_product), "SELECTION_SUMMARY": _tex(selection),
        "FILTER_DETAILS": filter_details,
        "DATE_SELECTION": "\n".join(r"\item " + _tex(f"{mission}: {window}.")
                                      for mission, window in dates),
        "BEFORE_AFTER_ROWS": _rows(source_rows), "ROW_COUNT": f"{row_count:,}",
        "INPUT_COUNT": f"{sum(item['input_rows'] for item in sources):,}",
        "REMOVED_COUNT": f"{sum(item['input_rows'] for item in sources)-row_count:,}",
        "PRODUCT_REFERENCES": references,
        # Per-database column tables (catalogue report). ``column_tables`` is a list of
        # (caption, label, schema); each is rendered with the shared description set.
        "COLUMN_TABLES": "\n\n".join(
            render_column_table(schema, descriptions, caption=caption, label=label)
            for caption, label, schema in (column_tables or [])
        ),
        "DATAFLOW_EXTRA": dataflow_extra,
    }
    text = (ASSET_DIR / "latex" / template).read_text()
    # Optional placeholder lines that vanish entirely when empty, so the shared
    # SWOT/HSCAT documents (which leave them blank) stay byte-identical.
    optional_lines = {"COVERAGE_EXTRA", "MONTHLY_EXTRA", "DISTRIBUTION_EXTRA",
                      "CATALOGUE_VERSIONS", "FILTER_REMOVAL_FIGURES", "DATAFLOW_EXTRA",
                      "FILTER_DETAILS"}
    kept = []
    for line in text.split("\n"):
        match = re.fullmatch(r"@@([A-Z_]+)@@", line.strip())
        if (match and match.group(1) in optional_lines
                and mapping.get(match.group(1), "") == ""):
            continue
        kept.append(line)
    text = "\n".join(kept)
    for key, value in mapping.items():
        text = text.replace(f"@@{key}@@", value)
    leftovers = re.findall(r"@@[A-Z_]+@@", text)
    if leftovers:
        raise ValueError(f"Unfilled Curated report fields: {leftovers}")
    # The catalogue template replaces the single table with per-database tables
    # (``column_tables``); the curated single-table longtable swap applies otherwise.
    if column_tables is None and len(catalogue_fields) > 35:
        # Long native schemas need repeated headers, not a clipped unbreakable table.
        anchor = r"\label{tab:columns}"
        position = text.index(anchor)
        start = text.rfind(r"\begin{table}[H]\centering", 0, position)
        end = text.index(r"\end{table}", position) + len(r"\end{table}")
        block = text[start:end]
        spec = next(line for line in block.splitlines() if line.startswith(r"\begin{tabular}"))
        header = r"\textbf{Column} & \textbf{Type} & \textbf{Description} " + chr(92) * 2
        long_rows = [line.rstrip() + "*" if line.startswith(r"\multicolumn") else line
                      for line in column_lines]
        long_table = "\n".join([
            r"{\small", spec.replace(r"\begin{tabular}", r"\begin{longtable}"),
            r"\caption{" + column_caption + r".\label{tab:columns}}" + chr(92) * 2,
            r"\toprule", header, r"\midrule", r"\endfirsthead",
            r"\caption[]{" + column_caption + " (continued).}" + chr(92) * 2,
            r"\toprule", header, r"\midrule", r"\endhead",
            r"\midrule", r"\multicolumn{3}{r}{Continued on next page.}" + chr(92) * 2,
            r"\endfoot", r"\bottomrule", r"\endlastfoot",
            *long_rows, r"\end{longtable}", "}",
        ])
        text = text[:start] + long_table + text[end:]
    return text
