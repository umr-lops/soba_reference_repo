"""Generate the Co-aligned catalogue report: figures, LaTeX and optional PDF.

Documents the raw (pre-curation) co-aligned catalogue Parquet files -- the
concatenated, enriched Sentinel-1 WV / altimeter matchups -- as the sibling
document to the Curated dataset PDF (see :mod:`soba_reference_repo.alti_report`).

A catalogue is produced by the colocation pipeline (one Parquet per SAR unit
and reference database: CCI SeaState V5 or CMEMS ALTI-L3-VAVH) and is the
*input* to the curation step. This module shares almost everything with the
ALTI curated report:

* the SOBA LaTeX engine (:func:`soba_reference_repo.curated_report.build_curated_report`),
  driven here by ``catalogue_template.tex`` (no curation "Production Steps");
* the figure builders ``plot_alti_figures`` / ``plot_alti_split_figures`` and the
  per-database ``_coverage_extra_latex`` / ``_monthly_extra_latex`` blocks;
* the shared ``pdf_support`` (assets staging + PDF compilation) and ``plotting``.

Catalogue-specific content:

* one file per (SAR unit, reference database) is documented (a re-run of the
  same unit+database is deduplicated to the latest production date);
* the column table is split per reference database, because the CCI and CMEMS
  catalogues carry different column sets (described as a union over the SAR
  units of that database);
* a "How the Catalogue Is Produced" section documents the colocation input
  path and every enrichment column the pipeline adds on top of the raw
  colocation variables.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import tomllib
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from .alti_report import (
    VALUE_COLUMN,
    _coverage_extra_latex,
    _database_of,
    _monthly_extra_latex,
    plot_alti_figures,
    plot_alti_split_figures,
)
from .curation import S1_WV_ALTI_NAME_RE, read_recipe
from .curated_report import build_curated_report
from .hscat_report import _tex
from .pdf_support import (ASSET_DIR, _path, compile_pdf, purge_latex_byproducts,
                          stage_latex_assets)
from .plotting import (
    figure_coverage,
    figure_coverage_panels,
    figure_distribution,
    figure_monthly,
    figure_monthly_stacked,
    group_values,
)

DOCUMENT_STEM = "S1_catalogue_WV_ALTI"
# Columns the report reads from each raw catalogue Parquet (``_curation_mission``
# is added here, since the pre-curation catalogue does not carry it).
READ_COLUMNS = ("sar_lon", "sar_lat", "sar_time", "fname", VALUE_COLUMN)

CATALOGUE_FIELD_DESCRIPTIONS = {
    "ref_hs_alti_closest": "Significant wave height from the closest altimeter (m).",
    "ww3_swh": "WW3 model significant wave height (m).",
    "imerg_rainrate": "IMERG rain rate at the reference time (mm/h).",
    "fname": "Reference altimeter source filename, identifying the altimeter.",
    "sar_time": "UTC SAR acquisition time.",
    "sar_lon": "SAR WV imagette longitude in degrees.",
    "sar_lat": "SAR WV imagette latitude in degrees.",
    "ref_time": "UTC altimeter reference acquisition time.",
    "ref_lon": "Altimeter reference longitude in degrees.",
    "ref_lat": "Altimeter reference latitude in degrees.",
    "oswTotalHs": "Significant wave height from the ESA S1 OCN processor (m).",
    "sar_safe_slc": "Sentinel-1 SLC SAFE basename for the WV imagette.",
    "primary_key": "Source matchup identifier retained in the catalogue.",
    "ref_time_delta": "Time difference SAR - altimeter (seconds).",
    "ref_distance_delta": "Great-circle distance from SAR centroid to altimeter (km).",
    "ref_param_count": ("Number of valid altimeter Hs within 30 km / 25 min of the WV imagette "
                        "(colocation hs_alti_count); rows with 0 are removed."),
    "hs_alti_mean": "Mean of the altimeter Hs within 30 km / 25 min of the WV imagette (m).",
    "hs_alti_median": "Median of the altimeter Hs within 30 km / 25 min of the WV imagette (m).",
    "hs_alti_std": "Standard deviation of the altimeter Hs within 30 km / 25 min (m).",
    "sea_ice_concentration": ("Bremen ASI v5.4 sea ice concentration (%) at the altimeter "
                              "point and day; 0 outside the polar grids, NaN on land."),
    "alti_rain_contaminated": ("1 when IMERG rain > 0 (or NaN) at the altimeter point and the "
                               "1 Hz altimeter track shows a raw-Hs spike or gaps within 3 s."),
}

SCOPE_TEXT = (
    "A co-aligned catalogue of Sentinel-1 Wave Mode observations matched to altimeter "
    "significant-wave-height measurements (CCI SeaState V5 and CMEMS ALTI-L3-VAVH), "
    "before quality curation."
)
GENERAL_DESCRIPTION = (
    "Copernicus/ESA Sentinel-1 Level-2 Ocean (OCN), Wave Mode observations are matched "
    "in space and time to altimeter significant wave height (CCI SeaState V5 and "
    "CMEMS ALTI-L3-VAVH), producing a concatenated and enriched catalogue of "
    "co-located matchups."
)
# Colocation input root (``catawvaltioneshot.common.inputd_dir``); one NetCDF per
# day per altimeter, one sub-tree per SAR unit and reference database.
DATAFLOW_INPUT_DIR = (
    "/legacy/project/cersat/public/cache/project/mpc-sentinel1/"
    "analysis/s1_data_analysis/hs_nn/unified_colocs_wv_alti_ocn_v3/"
)
DATAFLOW_INPUT_PATTERN = "{S1A-D}_{cci,cmems}_*/20*/coloc_20*25_min_30_km.nc"


def _catalogue_name(item) -> str:
    return Path(item["path"]).name


def _prod_date(path) -> str:
    """The catalogue production timestamp (3rd date in the filename); '' if absent."""
    match = S1_WV_ALTI_NAME_RE.match(Path(path).name)
    return match.group(5) if match else ""


def _file_date(path) -> str:
    """Production date (YYYY-MM-DD) taken from the 3rd date in the catalogue basename."""
    stamp = _prod_date(path)  # e.g. 20261018T021205
    if len(stamp) < 8:
        return ""
    return f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}"


def _file_version_rows(sources) -> list[str]:
    """One Table-2 row per catalogue, dated with that file's own production date."""
    rows = []
    for item in sources:
        date = _file_date(item["path"]) or "unknown"
        rows.append(" & ".join([
            _path(Path(item["curated_path"]).name),
            _tex(date),
            f"Contains {item['curated_rows']:,} matches (production date from filename).",
        ]) + r" \\")
    return rows


def _dedup_by_mission_db(catalogues) -> list[dict]:
    """Keep one catalogue per (SAR unit, reference database) -- the latest re-run."""
    best: dict[tuple[str, str], dict] = {}
    for item in catalogues:
        key = (item["mission"], _database_of(item["path"]))
        current = best.get(key)
        if current is None or _prod_date(item["path"]) > _prod_date(current["path"]):
            best[key] = item
    return sorted(best.values(), key=lambda it: (it["mission"], _database_of(it["path"])))


def _read_catalogue(sources) -> pd.DataFrame:
    """Concatenate the report columns from every raw catalogue Parquet.

    The raw catalogue has no ``_curation_mission`` column, so the mission is
    tagged from each source; ``_source_db`` (CCI/CMEMS) is derived from the name.
    """
    frames = []
    for item in sources:
        frame = pq.read_table(item["path"], columns=list(READ_COLUMNS)).to_pandas()
        frame["_curation_mission"] = item["mission"]
        frame["_source_db"] = _database_of(item["path"])
        frames.append(frame)
    return pd.concat(frames, ignore_index=True)


def _union_schema(paths) -> pa.Schema:
    """Column union over a list of catalogue files, in first-seen order."""
    order, fields = [], {}
    for path in paths:
        for field in pq.read_schema(path):
            if field.name not in fields:
                fields[field.name] = field
                order.append(field.name)
    return pa.schema([fields[name] for name in order])


def _sources_with_rowcounts(catalogues) -> list[dict]:
    """Build manifest-like sources (input_rows == curated_rows; no curation steps)."""
    sources = []
    for item in catalogues:
        path = Path(item["path"]).resolve()
        row_count = pq.ParquetFile(path).metadata.num_rows
        sources.append({
            "mission": item["mission"],
            "path": str(path),
            "curated_path": str(path),
            "input_rows": row_count,
            "curated_rows": row_count,
            "steps": [],
        })
    return sources


def _column_tables(catalogues) -> list[tuple[str, str, pa.Schema]]:
    """One (caption, label, union schema) per reference database present."""
    tables = []
    for db, label, caption in (
        ("CCI", "tab:cols_cci", "CCI SeaState V5 catalogue columns (union over S1A/S1B/S1C)"),
        ("CMEMS", "tab:cols_cmems", "CMEMS ALTI-L3-VAVH catalogue columns (union over S1A-D)"),
    ):
        paths = [item["path"] for item in catalogues if _database_of(item["path"]) == db]
        if paths:
            tables.append((caption, label, _union_schema(paths)))
    return tables


def _dataflow_latex() -> str:
    """The 'How the Catalogue Is Produced' enrichment table (colocation -> catalogue)."""
    slc_cols = ("sar_safe_slc, sar_safe_ocn, sar_path_slc, sar_pathsafe_slc, "
                "imagette_number, sar_ocn_basename, sar_slc_basename")
    rename_cols = ("oswLon->sar_lon, oswLat->sar_lat, oswIncidenceAngle->sar_incidence_angle, "
                   "hs_alti_closest->ref_hs_alti_closest, time_ALT->ref_time, ...")
    ww3_cols = ("ww3_swh, ww3_peak_wave_period, ww3_mean_wave_period, "
                "ww3_mean_wave_direction, ww3_wave_skewness, ww3_wavenumber_peakdness, "
                "ww3_emb, ww3_mean_wave_period_t0m1")
    rows = [
        ("Input colocation files",
         "Unified WV-OCN / +altimeter colocation NetCDF (one per day per altimeter)",
         "Raw colocation variables (SAR imagette geometry, OCN wave fields, closest altimeter)"),
        ("Altimeter window statistics",
         "all the altimeter Hs within 30 km / 25 min of the imagette (colocations v3)",
         "ref_param_count (hs_alti_count), hs_alti_mean, hs_alti_median, hs_alti_std"),
        ("Empty altimeter window", "rows with ref_param_count == 0 removed (no valid Hs)",
         "--"),
        ("SLC / SAFE resolution", "OCN -> SLC via CDSE daily Parquet + s1ifr", slc_cols),
        ("SOBA rename", "VARIABLE_RENAME_MAP", rename_cols),
        ("Distance to coast", "colocation distance_to_coast (m) -> km (CCI only)",
         "sar_distance_to_coast"),
        ("DL scene classification", "TenGeoP DL_Summary.xml per SAFE",
         "class_1..class_10, prob_1..prob_10"),
        ("IMERG rain rate", "GPM 3B-HHR L3 (mm/h) at the altimeter reference time",
         "imerg_rainrate"),
        ("Along-track rain flag",
         "IMERG rain > 0 (or NaN) AND raw-Hs spike (>= 0.25 m, >= 4 sigma) or >= 3 missing "
         "points within 3 s on the 1 Hz altimeter track",
         "alti_rain_contaminated, alti_track_raw_dev, alti_track_raw_z, alti_track_n_core, "
         "alti_track_n_core_good, alti_track_spike, alti_track_gap"),
        ("Sea ice concentration",
         "Bremen ASI v5.4 daily 6.25 km (AMSR2), nearest cell at the altimeter point",
         "sea_ice_concentration"),
        ("WW3 wave fields",
         "WW3 GLOB-30M association (CMEMS only; CCI carries them in the colocations)", ww3_cols),
        ("Primary key + dedup", "sar_safe_slc + ref_lon + ref_lat (3 dp); drop duplicates",
         "primary_key"),
        ("Mandatory defaults", "SOBA mandatory columns absent from the colocations",
         "ref_flag, ref_id, ref_source, legacy_usage, imerg_rainrate (NaN), "
         "sea_ice_concentration (NaN), alti_rain_contaminated (0)"),
        ("Dropped", "too large / unused", "oswCartSpecRe, oswCartSpecIm, z (GRIB geopotential)"),
    ]
    body = "\n".join(
        f"{_tex(step)} & {_tex(source)} & {_tex(cols)} \\\\" for step, source, cols in rows
    )
    return "\n".join([
        r"\noindent\textbf{Input colocation files (NetCDF).} " + _tex(DATAFLOW_INPUT_DIR) + " , "
        "one per day per altimeter, matched per SAR unit and reference database; file pattern "
        r"\texttt{" + _tex(DATAFLOW_INPUT_PATTERN) + "}. The pipeline "
        r"(\texttt{catawvaltioneshot}) loads, concatenates and enriches them into one Parquet "
        "catalogue per SAR unit and reference database.",
        r"\begin{table}[H]\centering",
        r"\caption{Enrichment applied by the colocation-to-catalogue pipeline.}",
        r"\label{tab:dataflow}",
        r"{\small",
        r"\begin{tabular}{"
        r"@{}>{\raggedright\arraybackslash}p{3.9cm}"
        r">{\raggedright\arraybackslash}p{5.1cm}"
        r">{\raggedright\arraybackslash}p{6.0cm}@{}}",
        r"\toprule",
        r"\textbf{Step} & \textbf{Source} & \textbf{Columns added / renamed} " + chr(92) * 2,
        r"\midrule", body, r"\bottomrule", r"\end{tabular}", "}", r"\end{table}",
    ])


def build_catalogue_report(catalogues, *, version, production_date, output_dir,
                           compile_report=True, miktex_bin=None) -> dict:
    """Build figures + LaTeX (+ optional PDF) describing raw co-aligned catalogues.

    ``catalogues`` is a list of ``{"mission": "S1A", "path": <catalogue parquet>}``.
    Re-runs of the same (SAR unit, reference database) are deduplicated to the latest
    production date. ``output_dir`` receives ``report/S1_catalogue_WV_ALTI.tex`` + the
    versioned PDF and ``manifest.json``.
    """
    output_dir = Path(output_dir)
    report_dir = output_dir / "report"
    report_dir.mkdir(parents=True, exist_ok=True)
    assets_dir = report_dir / f"images_{DOCUMENT_STEM}"
    assets_dir.mkdir(parents=True, exist_ok=True)

    catalogues = _dedup_by_mission_db(catalogues)
    sources = _sources_with_rowcounts(catalogues)
    column_tables = _column_tables(catalogues)
    catalogue = _read_catalogue(sources)
    figures = plot_alti_figures(catalogue, assets_dir)
    split = plot_alti_split_figures(catalogue, assets_dir)
    figures.update(split)

    values = pd.to_numeric(catalogue[VALUE_COLUMN], errors="coerce").to_numpy(dtype=float)
    finite = values[np.isfinite(values)]
    summary = ({"mean": float(finite.mean()), "median": float(np.median(finite))}
               if finite.size else {})

    recipe = {
        "reference": "altimeter",
        "production_date": production_date,
        "version": version,
        "catalogues": [
            {"mission": item["mission"], "path": Path(item["path"]).resolve(), "rules": []}
            for item in catalogues
        ],
    }
    manifest = {"reference": "altimeter", "mode": "WV", "status": "complete",
                "input_rows": sum(source["input_rows"] for source in sources),
                "curated_rows": sum(source["curated_rows"] for source in sources),
                "sources": sources}

    stage_latex_assets(ASSET_DIR / "latex", assets_dir)
    tex = build_curated_report(
        title="Sentinel-1 Co-aligned altimeter catalogue",
        platform="WV | ALTI",
        instrument="Sentinel-1 WV",
        reference="altimeter CCI SeaState V5 + CMEMS ALTI-L3-VAVH",
        variable="significant wave height (m)",
        document_stem=DOCUMENT_STEM,
        recipe=recipe,
        manifest=manifest,
        figures={
            "coverage": figures["coverage"],
            "monthly": figures["monthly_rows"],
            "distribution": figures["ref_distribution"],
        },
        summary=summary,
        observation_path=output_dir / "manifest.json",
        field_descriptions=CATALOGUE_FIELD_DESCRIPTIONS,
        version_extra=True,
        # The shared ALTI helpers emit a raw em-dash in the per-database captions;
        # emit a LaTeX em-dash so it typesets under both pdflatex and tectonic.
        coverage_extra=_coverage_extra_latex(assets_dir, split).replace("—", "---"),
        monthly_extra=_monthly_extra_latex(assets_dir, split).replace("—", "---"),
        scope_text=SCOPE_TEXT,
        general_description=GENERAL_DESCRIPTION,
        column_caption="Columns of the co-aligned catalogue Parquet files",
        column_tables=column_tables,
        dataflow_extra=_dataflow_latex(),
        file_version_rows=_file_version_rows(sources),
        template="catalogue_template.tex",
    )
    tex_path = report_dir / f"{DOCUMENT_STEM}.tex"
    tex_path.write_text(tex, encoding="utf-8")

    pdf_path = None
    if compile_report:
        pdf_path = compile_pdf(report_dir, DOCUMENT_STEM, miktex_bin)
        purge_latex_byproducts(report_dir, DOCUMENT_STEM)
        versioned = report_dir / f"{DOCUMENT_STEM}_{version}.pdf"
        pdf_path.replace(versioned)
        pdf_path = versioned

    manifest.update({"report_tex": str(tex_path),
                     "pdf": str(pdf_path) if pdf_path else None,
                     "figures": {name: str(path) for name, path in figures.items()}})
    (output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return {"tex": str(tex_path), "pdf": str(pdf_path) if pdf_path else None,
            "figures": {name: str(path) for name, path in figures.items()}}


def run_catalogue_recipe(recipe_path, *, compile_report=None, miktex_bin=None,
                         html=False) -> int:
    """Catalogue report from a recipe (``reference = "altimeter"``)."""
    recipe_path = Path(recipe_path).resolve()
    recipe = read_recipe(recipe_path)
    if recipe["reference"] != "altimeter":
        raise ValueError(
            f"catalogue recipes require reference = 'altimeter', got {recipe['reference']!r}"
        )
    if compile_report is None:
        compile_report = recipe.get("compile", False)
    root = recipe["output_dir"]
    root.mkdir(parents=True)
    shutil.copyfile(recipe_path, root / "recipe.toml")
    catalogues = [{"mission": item["mission"], "path": item["path"]}
                  for item in recipe["catalogues"]]
    result = build_catalogue_report(
        catalogues, version=recipe["version"], production_date=recipe["production_date"],
        output_dir=root, compile_report=compile_report, miktex_bin=miktex_bin,
    )
    print(f"REPORT: {result['tex']}")
    if result["pdf"]:
        print(f"PDF:    {result['pdf']}")
    if html:
        from .html_report import build_html_report

        print(f"HTML:   {build_html_report(root)}")
    return 0


def run_catalogue_run_dir(run_dir, *, compile_report=True, miktex_bin=None,
                          html=False) -> int:
    """Rebuild the catalogue report from an existing run dir (recipe.toml + catalogues)."""
    run_dir = Path(run_dir)
    with (run_dir / "recipe.toml").open("rb") as stream:
        recipe = tomllib.load(stream)
    catalogues = [
        {"mission": item["mission"], "path": (run_dir / item["path"]).resolve()}
        for item in recipe.get("catalogues", [])
    ]
    result = build_catalogue_report(
        catalogues, version=recipe["version"], production_date=recipe["production_date"],
        output_dir=run_dir, compile_report=compile_report, miktex_bin=miktex_bin,
    )
    print(f"REPORT: {result['tex']}")
    if result["pdf"]:
        print(f"PDF:    {result['pdf']}")
    if html:
        from .html_report import build_html_report

        print(f"HTML:   {build_html_report(run_dir)}")
    return 0


# ---------------------------------------------------------------------------
# Catalogues written by ``soba_build_coalign``: any reference (scatterometer, altimeter, SWOT).
# Same engine (``build_curated_report``) and template (``catalogue_template.tex``) as above.
# ---------------------------------------------------------------------------

COALIGN_NAME = re.compile(
    r"(?P<sat>S1[A-D])_coaligned_catalogue_(?P<mode>WV)_(?P<start>\d{8})_(?P<stop>\d{8})_"
    r"(?P<production>\d{8})_(?P<pol>SV|DV|SH|DH)_(?P<product>[A-Za-z0-9][A-Za-z0-9._-]*)_"
    r"(?P<version>\d+\.\d+)\.parquet$")
COALIGN_POLARIZATION = {"SV": "single VV (SV)", "SH": "single HH (SH)",
                        "DV": "dual VV+VH (DV)", "DH": "dual HH+HV (DH)"}
# Reference parameter column (named after its dataset, spec ``ref_param_N``) -> (label, unit, kind)
COALIGN_PARAMETERS = {
    "windspeed_scat": ("Scatterometer wind speed", "m/s", "HSCAT"),
    "winddirection_scat": ("Scatterometer wind direction (towards)", "degrees", "HSCAT"),
    "swh_alti": ("Altimeter significant wave height", "m", "ALTI"),
    "swh_swot": ("SWOT KaRIn significant wave height", "m", "SWOT"),
}
COALIGN_KINDS = {
    "HSCAT": ("scatterometer", "scatterometer winds",
              "HY-2 HSCAT 25 km ocean surface winds, processed by KNMI for EUMETSAT OSI SAF"),
    "ALTI": ("altimeter", "altimeter significant wave heights",
             "significant wave height (CMEMS wave L3 altimetry)"),
    "SWOT": ("SWOT", "SWOT significant wave heights",
             "SWOT KaRIn Level-2 WindWave (PGD0/PID0), distributed by NASA/JPL PO.DAAC"),
}
# The groupings of the maps and monthly counts: (title, frame column).
COALIGN_GROUPS = (("Sentinel-1 satellite", "satellite"), ("reference mission", "mission"),
                  ("reference product", "product"))
COALIGN_PANELS_PER_FIGURE = 3
COALIGN_DESCRIPTIONS = {
    "primary_key": ("SLC SAFE name, imagette number, ref_lon and ref_lat (four decimals); "
                    "unique in the file."),
    "sar_time": "UTC start time of the SAR measurement.",
    "sar_lat": "Latitude of the centre of the imagette footprint, in degrees.",
    "sar_lon": "Longitude of the centre of the imagette footprint, in degrees.",
    "sar_incidence_angle": "SAR incidence angle in degrees, as given with the matched point.",
    "sar_elevation_angle": "SAR elevation angle in degrees, as given with the matched point.",
    "sar_ground_heading": "SAR ground heading in degrees, as given with the matched point.",
    "sar_ground_heading_uncertainty": "Uncertainty of the SAR ground heading, in degrees.",
    "sar_distance_to_coast": "Distance to the coast in km (NASA GSFC grid), from the STAC Item.",
    "sar_path_ocn": "Full path of the Sentinel-1 OCN measurement file of the imagette.",
    "sar_path_slc": "Full path of the Sentinel-1 SLC measurement file of the imagette.",
    "sar_safe_slc": "Sentinel-1 SLC SAFE name followed by :WV_ and the imagette number.",
    "sar_safe_ocn": "Sentinel-1 OCN SAFE name followed by :WV_ and the imagette number.",
    "ref_lon": "Longitude of the reference point, in degrees.",
    "ref_lat": "Latitude of the reference point, in degrees.",
    "ref_time": "UTC time of the reference point.",
    "ref_time_delta_min": "Reference time minus SAR time, in minutes.",
    "ref_flag": ("Integer quality flag of the reference product (bit mask); empty when the "
                 "product has none."),
    "ref_id": "Name of the reference product file the point comes from.",
    "legacy_usage": "Marks a case already used for training in a previous exercise; empty here.",
    "ref_source": "Name of the reference product.",
    "windspeed_scat": "Scatterometer wind speed in m/s.",
    "winddirection_scat": "Scatterometer wind direction, towards, in degrees clockwise from north.",
    "swh_alti": "Altimeter significant wave height in m.",
    "swh_swot": "SWOT KaRIn significant wave height (swh_karin) in m.",
    "class_1": "Most likely TenGeoP surface roughness class of the imagette.",
    "pos_1": "Probability (0 to 1) of class_1.",
    "ECMWF_Wind_Speed": ("ECMWF operational 10 m wind speed at the reference point and time, "
                         "in m/s."),
    "ECMWF_Wind_Direction": ("ECMWF operational 10 m wind direction, towards, in degrees "
                             "clockwise from north."),
}


def _coalign_kind(columns) -> str:
    for column, (_, _, kind) in COALIGN_PARAMETERS.items():
        if column in columns:
            return kind
    return "OTHER"


def _coalign_date(text: str) -> str:
    return f"{text[:4]}-{text[4:6]}-{text[6:8]}"


def read_coalign_catalogues(folder) -> list:
    """One entry per catalogue file of ``folder``: name parts, attributes, schema, row count."""
    entries = []
    for path in sorted(Path(folder).glob("S1*_coaligned_catalogue_WV_*.parquet")):
        match = COALIGN_NAME.match(path.name)
        if match is None:
            raise ValueError(f"not a coalign catalogue name: {path.name}")
        parquet = pq.ParquetFile(path)
        attrs = {k.decode(): v.decode("utf-8", errors="replace")
                 for k, v in (parquet.schema_arrow.metadata or {}).items()}
        schema = parquet.schema_arrow
        entries.append({**match.groupdict(), "path": path, "attrs": attrs, "schema": schema,
                        "rows": parquet.metadata.num_rows, "kind": _coalign_kind(schema.names)})
    if not entries:
        raise FileNotFoundError(f"no coalign catalogue in {folder}")
    return entries


def read_coalign_frame(entries) -> pd.DataFrame:
    """The report columns of every file, with the satellite, mission, product and kind of rows."""
    wanted = ["sar_lon", "sar_lat", "sar_time", "ref_source", *COALIGN_PARAMETERS]
    frames = []
    for entry in entries:
        columns = [c for c in wanted if c in entry["schema"].names]
        frame = pq.read_table(entry["path"], columns=columns).to_pandas()
        frame["mission"] = frame.pop("ref_source").astype("category")
        frame["satellite"], frame["product"], frame["kind"] = (
            entry["sat"], entry["product"], entry["kind"])
        frames.append(frame)
    return pd.concat(frames, ignore_index=True)


def _coalign_figure(name, caption, label, width="0.9"):
    return "\n".join([
        r"\begin{figure}[H]\centering",
        rf"\includegraphics[width={width}\textwidth]{{{name}}}",
        rf"\caption{{{caption}}}", rf"\label{{{label}}}", r"\end{figure}"])


def coalign_coverage_blocks(frame, assets, figures) -> str:
    """Maps per Sentinel-1 satellite, reference mission and reference product (LaTeX)."""
    blocks = []
    for title, column in COALIGN_GROUPS:
        values = group_values(frame, column)
        if len(values) < 2:
            blocks.append(rf"\noindent\textbf{{Coverage by {title}.}} All rows have the same "
                          rf"{title}: {_tex(values[0])}, shown in the global map above.")
            continue
        blocks.append(rf"\noindent\textbf{{Coverage by {title}.}}")
        for number, start in enumerate(range(0, len(values), COALIGN_PANELS_PER_FIGURE), 1):
            chunk = values[start:start + COALIGN_PANELS_PER_FIGURE]
            name = f"coverage_{column}_{number}.png"
            figure_coverage_panels(frame, column, chunk, figures / name)
            blocks.append(_coalign_figure(
                f"{assets}/{name}", f"Coverage per {title}: " + ", ".join(_tex(v) for v in chunk)
                + ".", f"fig:coverage_{column}_{number}", width="0.88"))
    return "\n\n".join(blocks)


def coalign_monthly_blocks(frame, assets, figures) -> str:
    """Monthly counts by reference mission and reference product (LaTeX)."""
    blocks = []
    for title, column in COALIGN_GROUPS[1:]:
        values = group_values(frame, column)
        if len(values) < 2:
            blocks.append(rf"\noindent\textbf{{Matchups by {title}.}} All rows have the same "
                          rf"{title}: {_tex(values[0])}.")
            continue
        name = f"monthly_{column}.png"
        figure_monthly_stacked(frame, column, title, figures / name)
        blocks.append(_coalign_figure(
            f"{assets}/{name}", f"Monthly count of matches, stacked by {title}.",
            f"fig:monthly_{column}", width="0.85"))
    return "\n\n".join(blocks)


def coalign_column_tables(entries) -> list:
    """One ``(caption, label, schema)`` per reference product, columns of all its files."""
    by_product = defaultdict(list)
    for entry in entries:
        by_product[entry["product"]].append(entry)
    tables = []
    for index, (product, group) in enumerate(sorted(by_product.items())):
        fields = {}
        for entry in group:
            for field in entry["schema"]:
                fields.setdefault(field.name, field)
        satellites = "/".join(sorted({e["sat"] for e in group}))
        tables.append((f"{_tex(product)} catalogue columns ({satellites})", f"tab:cols_{index}",
                       pa.schema(list(fields.values()))))
    return tables


def coalign_completeness_table(entries) -> str:
    """Share of empty values per column and reference product (LaTeX table)."""
    by_product = defaultdict(list)
    for entry in entries:
        by_product[entry["product"]].append(entry)
    products = sorted(by_product)
    shares = {}
    for product in products:
        total, empty = 0, defaultdict(int)
        for entry in by_product[product]:
            table = pq.read_table(entry["path"])
            total += table.num_rows
            for name in table.column_names:
                empty[name] += table.column(name).null_count
        shares[product] = {name: count / total for name, count in empty.items()}
    names = [n for n in dict.fromkeys(n for p in products for n in shares[p])
             if any(shares[p].get(n, 0) > 0 for p in products)]
    if not names:
        return r"\noindent No column of the catalogues holds an empty value."
    rows = [" & ".join([_tex(name), *(f"{shares[p][name]:.1%}".replace("%", r"\%")
                                      if name in shares[p] else "--" for p in products)]) + r" \\"
            for name in names]
    spec = r"@{}>{\raggedright\arraybackslash}p{5cm}" + "r" * len(products) + "@{}"
    header = " & ".join(rf"\textbf{{{h}}}" for h in ["Column", *[_tex(p) for p in products]])
    return "\n".join([
        r"\begin{table}[H]\centering",
        r"\caption{Share of empty values per column and reference product}",
        r"\label{tab:completeness}", r"{\small", rf"\begin{{tabular}}{{{spec}}}", r"\toprule",
        header + r" \\", r"\midrule", *rows, r"\bottomrule", r"\end{tabular}", "}",
        r"\end{table}"])


def coalign_metadata_label(entry, several_products) -> str:
    """Short unique name of a file for the attribute table."""
    product = f" {entry['product']}" if several_products else ""
    return f"{entry['sat']}{product} {entry['start']}-{entry['stop']}"


def _coalign_catalogue_text(polarizations, completeness, product_text) -> dict:
    """The wording of the template placeholders for the coalign catalogues (LaTeX)."""
    return {
        "REF_PRODUCT": product_text,
        "COLUMNS_INTRO": (
            "The co-aligned catalogue carries the SAR observation, the matched reference "
            "measurement, and the\nancillary values. The reference parameters are named after "
            "their dataset, so each\nreference product has its own table."),
        "COLUMNS_EXTRA": completeness,
        "POSITION_TEXT": (
            "This document describes the \\textbf{co-aligned catalogue}: the matchups between "
            "Sentinel-1 Wave\nMode imagettes and the reference products, with the ancillary "
            "values added, before any quality\ncuration. It is the input to the curation step "
            "that yields the \\emph{curated} dataset."),
        "POLARIZATION": _tex(polarizations) + ".",
        "FILE_DESCRIPTION": (
            "A catalogue file holds one satellite, one polarization and one reference product. "
            "Each row is\none reference point matched to one Sentinel-1 WV imagette. The "
            "primary key is the SLC SAFE name,\nthe imagette number, \\texttt{ref\\_lon} and "
            "\\texttt{ref\\_lat} (four decimals). One imagette can have several rows,\nso row "
            "counts are not counts of distinct images."),
        "PRODUCTION_SECTION": "",
    }


def build_coalign_document(entries, kind, output_dir, *, compile_report=True,
                           miktex_bin=None) -> dict:
    """The document of the catalogues of one reference kind (SWOT, HSCAT or ALTI).

    Written as ``<output_dir>/S1_coalign_catalogue_WV_<KIND>.tex`` and, compiled,
    ``S1_coalign_catalogue_WV_<KIND>_<version>.pdf``.
    """
    versions = sorted({e["version"] for e in entries})
    if len(versions) != 1:
        raise ValueError(f"{kind} catalogues of several versions in one report: {versions}")
    version = versions[0]
    noun, scope, product_text = COALIGN_KINDS[kind]
    stem = f"S1_coalign_catalogue_WV_{kind}"
    output_dir = Path(output_dir)
    assets = f"images_{stem}"
    figures = output_dir / assets
    figures.mkdir(parents=True, exist_ok=True)
    stage_latex_assets(ASSET_DIR / "latex", figures)

    frame = read_coalign_frame(entries)
    parameters = [column for column, (_, _, k) in COALIGN_PARAMETERS.items()
                  if k == kind and column in frame and frame[column].notna().any()]
    figure_coverage(frame, figures / "coverage.png")
    figure_monthly(frame, figures / "monthly.png")
    chosen = {"coverage": figures / "coverage.png", "monthly": figures / "monthly.png"}
    for column in parameters:
        label, unit, _ = COALIGN_PARAMETERS[column]
        figure_distribution(frame, column, label, unit, figures / f"distribution_{column}.png")
        chosen[column] = figures / f"distribution_{column}.png"
    several_products = len({e["product"] for e in entries}) > 1
    sources = [{"mission": coalign_metadata_label(e, several_products), "path": str(e["path"]),
                "curated_path": str(e["path"]), "input_rows": e["rows"],
                "curated_rows": e["rows"], "steps": []} for e in entries]
    manifest = {"reference": noun, "mode": "WV", "status": "complete",
                "input_rows": sum(e["rows"] for e in entries),
                "curated_rows": sum(e["rows"] for e in entries), "sources": sources}
    production_dates = sorted({e["production"] for e in entries})
    polarizations = ", ".join(sorted({COALIGN_POLARIZATION[e["pol"]] for e in entries}))
    notes = [f", {int(e['attrs']['rows_dropped_duplicate_key'])} repeated key(s) dropped"
             if "rows_dropped_duplicate_key" in e["attrs"] else "" for e in entries]
    tex = build_curated_report(
        title=f"Sentinel-1 co-aligned {noun} catalogue", platform=f"WV | {kind}",
        instrument="Sentinel-1 WV", reference=f"{noun} {product_text}",
        variable=COALIGN_PARAMETERS[parameters[0]][0].lower() if parameters else "reference value",
        document_stem=stem,
        recipe={"production_date": production_dates[-1], "version": version,
                "reference_variable": "windspeed", "catalogues": []},
        manifest=manifest, figures=chosen, summary={},
        observation_path=output_dir / "manifest.json",
        field_descriptions=COALIGN_DESCRIPTIONS,
        scope_text=_tex("A co-aligned catalogue of Sentinel-1 Wave Mode observations matched to "
                        f"{scope}, with ancillary values, before quality curation."),
        general_description=(
            "Copernicus/ESA Sentinel-1 Level-2 Ocean (OCN), Wave Mode observations are matched "
            "to the reference product; the matched points are merged into one row per "
            "reference point, with the ancillary values added."),
        column_caption="Columns of the co-aligned catalogue Parquet files",
        column_tables=coalign_column_tables(entries), column_widths=(5.0, 7.2),
        coverage_extra=coalign_coverage_blocks(frame, assets, figures),
        monthly_extra=coalign_monthly_blocks(frame, assets, figures),
        file_version_rows=[
            " & ".join([_path(e["path"].name), _tex(_coalign_date(e["production"])),
                        f"Contains {e['rows']:,} matches{note}."]) + r" \\"
            for e, note in zip(entries, notes)],
        template="catalogue_template.tex",
        catalogue_text=_coalign_catalogue_text(
            polarizations, coalign_completeness_table(entries), product_text),
        value_column=parameters[0] if parameters else None,
        release_date=_coalign_date(production_dates[-1]))
    tex_path = output_dir / f"{stem}.tex"
    tex_path.write_text(tex, encoding="utf-8")
    pdf_path = None
    if compile_report:
        pdf_path = compile_pdf(output_dir, stem, miktex_bin)
        purge_latex_byproducts(output_dir, stem)
        versioned = output_dir / f"{stem}_{version}.pdf"
        pdf_path.replace(versioned)
        pdf_path = versioned
    return {"kind": kind, "tex": str(tex_path), "pdf": str(pdf_path) if pdf_path else None,
            "files": len(entries), "rows": sum(e["rows"] for e in entries)}


def build_coalign_report(catalogue_dir, output_dir, *, compile_report=True,
                         miktex_bin=None) -> list:
    """One document per reference kind found in ``catalogue_dir``; one result dict each."""
    entries = read_coalign_catalogues(catalogue_dir)
    unknown = sorted({e["path"].name for e in entries if e["kind"] not in COALIGN_KINDS})
    if unknown:
        raise ValueError(f"catalogues of an unknown reference kind: {unknown}")
    return [build_coalign_document([e for e in entries if e["kind"] == kind], kind, output_dir,
                                   compile_report=compile_report, miktex_bin=miktex_bin)
            for kind in COALIGN_KINDS if any(e["kind"] == kind for e in entries)]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--coalign", type=Path, metavar="CATALOGUES",
                       help="folder of catalogues written by soba_build_coalign (one document "
                            "per reference kind, written to --output)")
    group.add_argument("--recipe", type=Path, help="catalogue recipe to run")
    group.add_argument("--run-dir", type=Path,
                       help="catalogue run dir (recipe.toml + catalogues) to rebuild")
    parser.add_argument("--no-compile", action="store_true", help="skip the PDF step")
    parser.add_argument("--miktex-bin", type=Path, default=None)
    parser.add_argument("--html", action="store_true",
                        help="also write the interactive HTML producer report")
    parser.add_argument("--output", type=Path, default=None,
                        help="output folder for --coalign (default: <catalogues>/report)")
    args = parser.parse_args(argv)
    if args.coalign is not None:
        output = args.output or args.coalign / "report"
        for result in build_coalign_report(args.coalign, output,
                                           compile_report=not args.no_compile,
                                           miktex_bin=args.miktex_bin):
            print(f"{result['kind']}: {result['tex']} ({result['files']} files, "
                  f"{result['rows']:,} rows)")
            if result["pdf"]:
                print(f"  PDF: {result['pdf']}")
        return 0
    if args.recipe is not None:
        return run_catalogue_recipe(
            args.recipe,
            compile_report=None if not args.no_compile else False,
            miktex_bin=args.miktex_bin,
            html=args.html,
        )
    return run_catalogue_run_dir(args.run_dir, compile_report=not args.no_compile,
                                 miktex_bin=args.miktex_bin, html=args.html)


if __name__ == "__main__":
    raise SystemExit(main())
