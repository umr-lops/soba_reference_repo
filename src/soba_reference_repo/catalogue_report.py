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
import shutil
import tomllib
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
    "analysis/s1_data_analysis/hs_nn/unified_colocs_wv_alti_ocn_v2/"
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
        ("SLC / SAFE resolution", "OCN -> SLC via CDSE daily Parquet + s1ifr", slc_cols),
        ("SOBA rename", "VARIABLE_RENAME_MAP", rename_cols),
        ("Distance to coast", "colocation distance_to_coast (m) -> km (CCI only)",
         "sar_distance_to_coast"),
        ("DL scene classification", "TenGeoP DL_Summary.xml per SAFE",
         "class_1..class_10, prob_1..prob_10"),
        ("IMERG rain rate", "GPM 3B-HHR L3 (mm/h) at the altimeter reference time",
         "imerg_rainrate"),
        ("WW3 wave fields",
         "WW3 GLOB-30M association (CMEMS only; CCI carries them in the colocations)", ww3_cols),
        ("Primary key + dedup", "sar_safe_slc + ref_lon + ref_lat (3 dp); drop duplicates",
         "primary_key"),
        ("Mandatory defaults", "SOBA mandatory columns absent from the colocations",
         "ref_param_count, ref_flag, ref_id, ref_source, legacy_usage, imerg_rainrate (NaN)"),
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


def run_catalogue_recipe(recipe_path, *, compile_report=None, miktex_bin=None) -> int:
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
    return 0


def run_catalogue_run_dir(run_dir, *, compile_report=True, miktex_bin=None) -> int:
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
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--recipe", type=Path, help="catalogue recipe to run")
    group.add_argument("--run-dir", type=Path,
                       help="catalogue run dir (recipe.toml + catalogues) to rebuild")
    parser.add_argument("--no-compile", action="store_true", help="skip the PDF step")
    parser.add_argument("--miktex-bin", type=Path, default=None)
    args = parser.parse_args(argv)
    if args.recipe is not None:
        return run_catalogue_recipe(
            args.recipe,
            compile_report=None if not args.no_compile else False,
            miktex_bin=args.miktex_bin,
        )
    return run_catalogue_run_dir(args.run_dir, compile_report=not args.no_compile,
                                 miktex_bin=args.miktex_bin)


if __name__ == "__main__":
    raise SystemExit(main())
