"""Generate the Curated WV-altimeter report: figures, LaTeX and optional PDF.

The altimeter curation (``reference = "altimeter"``) covers seven Curated Parquet
files -- CCI SeaState V5 (S1A/B/C) plus CMEMS ALTI-L3-VAVH (S1A/B/C/D) -- which
share a mission label across two different reference databases. This module builds
a single document describing all of them, reading the Curated Parquet and manifest
written by :func:`soba_reference_repo.curate_dataset.run_curate_recipe`.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
from matplotlib import pyplot as plt

from .curated_report import build_curated_report
from .hscat_report import _annual_tick_indices
from .pdf_support import ASSET_DIR, compile_pdf, purge_latex_byproducts, stage_latex_assets
from .plotting import MISSION_COLORS, finish_density_map, plot_mission_distribution

DOCUMENT_STEM = "S1_curated_WV_ALTI"
VALUE_COLUMN = "ref_hs_alti_closest"
READ_COLUMNS = ("sar_lon", "sar_lat", "sar_time", "_curation_mission", VALUE_COLUMN)

FIELD_DESCRIPTIONS = {
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
    "_curation_mission": "Sentinel-1 mission identifier recorded during curation.",
    "_curation_row": "Zero-based row number in the original source Parquet.",
}


def _read_curated(sources) -> pd.DataFrame:
    """Concatenate the report columns from every Curated Parquet in the run."""
    frames = [
        pq.read_table(item["curated_path"], columns=list(READ_COLUMNS)).to_pandas()
        for item in sources
    ]
    return pd.concat(frames, ignore_index=True)


def plot_alti_figures(curated: pd.DataFrame, figures_dir) -> dict[str, Path]:
    """Write coverage, monthly-count and altimeter-SWH-distribution figures."""
    figures_dir = Path(figures_dir)
    figures_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "coverage": figures_dir / "sar_coverage.png",
        "monthly_rows": figures_dir / "monthly_rows.png",
        "ref_distribution": figures_dir / "alti_swh_distribution.png",
    }
    missions = curated["_curation_mission"].astype("string")
    present = [m for m in MISSION_COLORS if missions.eq(m).any()]

    figure, axis = plt.subplots(figsize=(9, 4.5))
    lon = pd.to_numeric(curated["sar_lon"], errors="coerce").to_numpy(dtype=float)
    lat = pd.to_numeric(curated["sar_lat"], errors="coerce").to_numpy(dtype=float)
    valid = np.isfinite(lon) & np.isfinite(lat)
    cells = axis.hexbin(lon[valid], lat[valid], gridsize=360,
                        extent=(-180, 180, -90, 90), mincnt=0, linewidths=0)
    axis.set(xlim=(-180, 180), ylim=(-90, 90),
             xlabel="Longitude (degrees)", ylabel="Latitude (degrees)",
             title="Sentinel-1 WV coverage")
    axis.grid(alpha=0.2)
    figure.tight_layout()
    finish_density_map(figure, axis, cells, reference="altimeter")
    figure.savefig(paths["coverage"], dpi=150)
    plt.close(figure)

    figure, axis = plt.subplots(figsize=(9, 4.5))
    times = pd.to_datetime(curated["sar_time"], errors="coerce", utc=True)
    periods = times.dt.tz_localize(None).dt.to_period("M")
    monthly = (
        pd.DataFrame({"month": periods, "mission": missions})
        .dropna()
        .groupby(["month", "mission"]).size()
        .unstack(fill_value=0)
        .sort_index()
    )
    bottom = pd.Series(0, index=monthly.index)
    for mission in present:
        counts = monthly[mission] if mission in monthly else pd.Series(0, index=monthly.index)
        axis.bar(monthly.index.astype(str), counts, bottom=bottom,
                 color=MISSION_COLORS[mission], label=mission)
        bottom += counts
    if present:
        axis.legend()
    axis.set(xlabel="SAR month", ylabel="Curated rows", title="Curated rows by month")
    ticks = _annual_tick_indices(monthly.index.astype(str).tolist())
    axis.set_xticks(ticks, monthly.index.astype(str)[ticks])
    axis.tick_params(axis="x", labelrotation=45)
    axis.grid(axis="y", alpha=0.2)
    figure.tight_layout()
    figure.savefig(paths["monthly_rows"], dpi=150)
    plt.close(figure)

    values = pd.to_numeric(curated[VALUE_COLUMN], errors="coerce").to_numpy(dtype=float)
    finite = np.isfinite(values)
    if finite.any():
        low, high = float(values[finite].min()), float(values[finite].max())
        if low == high:
            low, high = low - 0.5, high + 0.5
        edges = np.linspace(low, high, 41)
    else:
        edges = np.linspace(0, 1, 41)
    histogram = {
        mission: np.histogram(values[missions.eq(mission).to_numpy() & finite], bins=edges)[0]
        for mission in present
    }
    figure, axis = plt.subplots(figsize=(9, 5.0))
    plot_mission_distribution(axis, histogram, edges, MISSION_COLORS)
    axis.set(xlabel="Altimeter significant wave height (m)",
             title="Reference altimeter SWH distribution")
    axis.set_ylabel("Curated rows per mission (%)")
    axis.grid(axis="y", alpha=0.2)
    figure.tight_layout(rect=(0, 0.18, 1, 1))
    figure.savefig(paths["ref_distribution"], dpi=150)
    plt.close(figure)
    return paths


def build_alti_run_report(run_dir, *, compile_report=True, miktex_bin=None) -> dict:
    """Build figures + LaTeX (+ optional PDF) from an existing altimeter curated run."""
    run_dir = Path(run_dir)
    manifest = json.loads((run_dir / "manifest.json").read_text())
    if manifest.get("reference") != "altimeter":
        raise ValueError(f"not an altimeter curated run: {manifest.get('reference')!r}")
    # Parse the run's copied recipe directly (no read_recipe validation: this run
    # already exists, so its output_dir check would wrongly fail).
    with (run_dir / "recipe.toml").open("rb") as stream:
        recipe = tomllib.load(stream)
    recipe_by_name = {Path(item["path"]).name: item for item in recipe.get("catalogues", [])}
    # Reorder rules to match the manifest so the report's per-source steps align.
    recipe["catalogues"] = [
        recipe_by_name.get(Path(item["path"]).name, {"mission": item["mission"], "rules": []})
        for item in manifest["sources"]
    ]
    report_dir = run_dir / "report"
    report_dir.mkdir(parents=True, exist_ok=True)
    assets_dir = report_dir / f"images_{DOCUMENT_STEM}"
    assets_dir.mkdir(parents=True, exist_ok=True)

    curated = _read_curated(manifest["sources"])
    figures = plot_alti_figures(curated, assets_dir)
    values = pd.to_numeric(curated[VALUE_COLUMN], errors="coerce").to_numpy(dtype=float)
    finite = values[np.isfinite(values)]
    if finite.size:
        summary = {"mean": float(finite.mean()), "median": float(np.median(finite))}
    else:
        summary = {}

    stage_latex_assets(ASSET_DIR / "latex", assets_dir)
    tex = build_curated_report(
        title="Sentinel-1 Curated altimeter dataset",
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
        observation_path=run_dir / "manifest.json",
        field_descriptions=FIELD_DESCRIPTIONS,
    )
    tex_path = report_dir / f"{DOCUMENT_STEM}.tex"
    tex_path.write_text(tex, encoding="utf-8")

    pdf_path = None
    if compile_report:
        pdf_path = compile_pdf(report_dir, DOCUMENT_STEM, miktex_bin)
        purge_latex_byproducts(report_dir, DOCUMENT_STEM)
        versioned = report_dir / f"{DOCUMENT_STEM}_{recipe['version']}.pdf"
        pdf_path.replace(versioned)
        pdf_path = versioned

    manifest.update({"report_tex": str(tex_path), "pdf": str(pdf_path) if pdf_path else None,
                     "figures": {name: str(path) for name, path in figures.items()}})
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return {"tex": str(tex_path), "pdf": str(pdf_path) if pdf_path else None,
            "figures": {name: str(path) for name, path in figures.items()}}


def main(argv=None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path,
                        help="altimeter curated run directory (contains manifest.json)")
    parser.add_argument("--no-compile", action="store_true", help="skip the PDF step")
    parser.add_argument("--miktex-bin", type=Path, default=None)
    args = parser.parse_args(argv)
    result = build_alti_run_report(args.run_dir, compile_report=not args.no_compile,
                                   miktex_bin=args.miktex_bin)
    print(f"REPORT: {result['tex']}")
    if result["pdf"]:
        print(f"PDF:    {result['pdf']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
