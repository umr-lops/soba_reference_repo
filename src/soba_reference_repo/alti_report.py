"""Generate the Curated WV-altimeter report: figures, LaTeX and optional PDF.

The altimeter curation (``reference = "altimeter"``) covers seven Curated Parquet
files -- CCI SeaState V5 (S1A/B/C) plus CMEMS ALTI-L3-VAVH (S1A/B/C/D) -- which
share a mission label across two different reference databases. This module builds
a single document describing all of them, reading the Curated Parquet and manifest
written by :func:`soba_reference_repo.curate_dataset.run_curate_recipe`.

Owner review additions (2026-11-01):
  * per-database (CCI / CMEMS) and per-SAR-unit coverage + monthly figures, with
    one panel per SAR-unit / altimeter pair;
  * one map per quality filter showing the points removed at that step (the
    source catalogues are re-read and the filters replayed);
  * an initial (source) vs final (curated) altimeter Hs distribution;
  * a per-database source-version table in the Catalogue Columns section.
"""

from __future__ import annotations

import json
import math
import re
import tomllib
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
from matplotlib import colors
from matplotlib import pyplot as plt
from matplotlib.patches import Polygon

from .curation import clause_mask, validate_rule_order
from .curated_report import build_curated_report
from .hscat_report import _annual_tick_indices, _tex
from .pdf_support import ASSET_DIR, compile_pdf, purge_latex_byproducts, stage_latex_assets
from .plotting import MISSION_COLORS, finish_density_map, plot_mission_distribution

DOCUMENT_STEM = "S1_curated_WV_ALTI"
VALUE_COLUMN = "ref_hs_alti_closest"
READ_COLUMNS = ("sar_lon", "sar_lat", "sar_time", "fname", "_curation_mission", VALUE_COLUMN)

# Reference filenames identify the altimeter that produced the row.
CMEMS_ALTIMETER_NAMES = {
    "cfo": "CFOSAT", "h2b": "HaiYang-2B", "al": "SARAL", "j3": "Jason-3",
    "c2": "CryoSat-2", "h2c": "HaiYang-2C", "swot": "SWOT",
    "s3a": "Sentinel-3A", "s3b": "Sentinel-3B", "s6a": "Sentinel-6",
}
_CCI_ALTI_RE = re.compile(r"SWH-(.+?)-\d{8}T")
_CMEMS_ALTI_RE = re.compile(r"global_vavh_l3_rt_([a-z0-9]+)_")

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

_LAND_POLYGONS = None


def _land_polygons():
    """Cached continent polygons for the small map panels."""
    global _LAND_POLYGONS
    if _LAND_POLYGONS is None:
        land = json.loads((ASSET_DIR / "maps/ne_110m_land.geojson").read_text())
        polygons = []
        for feature in land["features"]:
            geometry = feature["geometry"]
            polys = ([geometry["coordinates"]] if geometry["type"] == "Polygon"
                     else geometry["coordinates"])
            polygons.extend(polygon[0] for polygon in polys)
        _LAND_POLYGONS = polygons
    return _LAND_POLYGONS


def _database_of(curated_path):
    name = Path(curated_path).name
    if "CMEMS-ALTI-L3-VAVH" in name:
        return "CMEMS"
    if "CCI-SEATSTATE" in name:
        return "CCI"
    return "OTHER"


def _altimeter_name(fname: pd.Series) -> pd.Series:
    """Map a reference ``fname`` column to a readable altimeter name."""
    names = fname.astype("string").fillna("")
    cci = names.str.extract(_CCI_ALTI_RE, expand=False)
    cmems = names.str.extract(_CMEMS_ALTI_RE, expand=False)
    cmems = cmems.map(lambda tok: CMEMS_ALTIMETER_NAMES.get(tok, tok)
                      if isinstance(tok, str) and tok else None)
    return cci.fillna(cmems).fillna("unknown")


def _read_curated(sources) -> pd.DataFrame:
    """Concatenate the report columns from every Curated Parquet in the run."""
    frames = []
    for item in sources:
        frame = pq.read_table(item["curated_path"], columns=list(READ_COLUMNS)).to_pandas()
        frame["_source_db"] = _database_of(item["curated_path"])
        frames.append(frame)
    return pd.concat(frames, ignore_index=True)


def _hexbin_panel(axis, lon, lat, norm):
    """Draw one density panel with the shared colour norm and land overlay."""
    cells = axis.hexbin(lon, lat, gridsize=360, extent=(-180, 180, -90, 90),
                        mincnt=0, linewidths=0)
    counts = cells.get_array()
    cells.set_array(np.ma.masked_where(counts == 0, counts))
    cells.set_norm(norm)
    cells.set_cmap(colors.LinearSegmentedColormap.from_list(
        "soba_density", ["#d3e8e5", "#123a40"]))
    for polygon in _land_polygons():
        axis.add_patch(Polygon(polygon, facecolor="#e8e7e1", edgecolor="#b5b4ae",
                               linewidth=0.35, zorder=3))
    axis.set(xlim=(-180, 180), ylim=(-90, 90), aspect="equal")
    axis.set_xticks([])
    axis.set_yticks([])
    return cells


def plot_alti_figures(curated: pd.DataFrame, figures_dir) -> dict[str, Path]:
    """Write the global coverage, monthly-count and altimeter-SWH figures."""
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


def plot_alti_split_figures(curated: pd.DataFrame, figures_dir) -> dict[str, Path]:
    """Per-database, per-SAR-unit coverage + monthly figures (altimeter panels)."""
    figures_dir = Path(figures_dir)
    figures_dir.mkdir(parents=True, exist_ok=True)
    if "_source_db" not in curated.columns or "fname" not in curated.columns:
        return {}
    altimeter = _altimeter_name(curated["fname"])
    curated = curated.assign(_altimeter=altimeter)
    lon = pd.to_numeric(curated["sar_lon"], errors="coerce")
    lat = pd.to_numeric(curated["sar_lat"], errors="coerce")
    times = pd.to_datetime(curated["sar_time"], errors="coerce", utc=True)
    months = times.dt.tz_localize(None).dt.to_period("M")

    paths: dict[str, Path] = {}
    for (db, mission), subset in curated.groupby(["_source_db", "_curation_mission"],
                                                 sort=True):
        subset = subset[subset["_altimeter"].ne("unknown")]
        if subset.empty:
            continue
        altimeters = [a for a in subset["_altimeter"].value_counts().index]
        # --- coverage: one hexbin panel per altimeter, shared colour norm --
        panel = {
            a: (lon[subset.index][subset["_altimeter"] == a].to_numpy(dtype=float),
                lat[subset.index][subset["_altimeter"] == a].to_numpy(dtype=float))
            for a in altimeters
        }
        vmax = 1
        for a_lon, a_lat in panel.values():
            valid = np.isfinite(a_lon) & np.isfinite(a_lat)
            if not valid.any():
                continue
            figure, axis = plt.subplots()
            cells = axis.hexbin(a_lon[valid], a_lat[valid], gridsize=360,
                                extent=(-180, 180, -90, 90), mincnt=0, linewidths=0)
            vmax = max(vmax, int(np.asarray(cells.get_array()).max()))
            plt.close(figure)
        norm = colors.LogNorm(vmin=1, vmax=max(2, vmax))
        ncols = min(3, len(altimeters))
        nrows = math.ceil(len(altimeters) / ncols)
        figure, axes = plt.subplots(nrows, ncols, figsize=(4.6 * ncols, 4.1 * nrows))
        axes = np.atleast_1d(axes).ravel()
        for axis, a in zip(axes, altimeters):
            a_lon, a_lat = panel[a]
            valid = np.isfinite(a_lon) & np.isfinite(a_lat)
            if not valid.any():
                axis.set_axis_off()
            else:
                _hexbin_panel(axis, a_lon[valid], a_lat[valid], norm)
            axis.set_title(f"{mission} / {a}", fontsize=9)
        for axis in axes[len(altimeters):]:
            axis.set_axis_off()
        figure.suptitle(f"Retained coverage — {mission}, {db}", fontsize=11)
        figure.tight_layout(rect=(0, 0, 1, 0.95))
        coverage_path = figures_dir / f"cov_{db}_{mission}.png"
        figure.savefig(coverage_path, dpi=150)
        plt.close(figure)
        paths[f"coverage_{db}_{mission}"] = coverage_path
        # --- monthly: stacked bars by altimeter ----------------------------
        monthly = (
            pd.DataFrame({"month": months[subset.index], "altimeter": subset["_altimeter"]})
            .dropna().groupby(["month", "altimeter"]).size().unstack(fill_value=0).sort_index()
        )
        figure, axis = plt.subplots(figsize=(9, 4.2))
        bottom = pd.Series(0, index=monthly.index)
        palette = plt.get_cmap("tab10")
        for i, a in enumerate(altimeters):
            counts = monthly[a] if a in monthly else pd.Series(0, index=monthly.index)
            axis.bar(monthly.index.astype(str), counts, bottom=bottom,
                     color=palette(i % 10), label=f"{a} (n={int(counts.sum()):,})")
            bottom += counts
        axis.legend(loc="upper center", bbox_to_anchor=(0.5, -0.18), ncol=min(4, len(altimeters)),
                    frameon=False, fontsize=8)
        ticks = _annual_tick_indices(monthly.index.astype(str).tolist())
        axis.set_xticks(ticks, monthly.index.astype(str)[ticks])
        axis.tick_params(axis="x", labelrotation=45)
        axis.set(xlabel="SAR month", ylabel="Curated rows",
                 title=f"Curated rows by month — {mission}, {db}")
        axis.grid(axis="y", alpha=0.2)
        figure.tight_layout()
        monthly_path = figures_dir / f"monthly_{db}_{mission}.png"
        figure.savefig(monthly_path, dpi=150)
        plt.close(figure)
        paths[f"monthly_{db}_{mission}"] = monthly_path
    return paths


def _rule_columns(rules) -> set[str]:
    cols = set()
    for rule in rules:
        if not rule.get("enabled", True):
            continue
        for clause in rule["clauses"]:
            cols.add(clause[0])
            if clause[1] in {"abs_diff_le", "angle_diff_le", "rel_diff_le",
                             "imagette_circstd_le", "not_in_window_for"} and len(clause) > 2:
                operand = clause[2]
                if isinstance(operand, list):
                    cols.add(operand[0])
    return cols


def _replay_removed(frame: pd.DataFrame, rules) -> dict[str, pd.Index]:
    """Replay the curation rules and return the original indices removed per rule."""
    validate_rule_order(rules)
    selected = frame
    removed: dict[str, pd.Index] = {}
    for rule in rules:
        if not rule.get("enabled", True):
            continue
        mask = pd.Series(True, index=selected.index)
        for clause in rule["clauses"]:
            mask &= clause_mask(selected, clause)
        removed[rule["id"]] = selected.index[~mask.to_numpy(dtype=bool)]
        selected = selected.loc[mask]
    return removed


def _load_source_frames(sources, recipe_by_name) -> list[tuple[dict, pd.DataFrame]]:
    """Read each source catalogue (only the columns the report needs)."""
    frames = []
    for item in sources:
        catalogue = recipe_by_name.get(Path(item["path"]).name, {})
        rules = catalogue.get("rules", [])
        columns = sorted(_rule_columns(rules) | {"sar_lon", "sar_lat", "sar_time",
                                                  "fname", VALUE_COLUMN})
        available = pq.read_schema(item["path"]).names
        columns = [c for c in columns if c in available]
        frame = pq.read_table(item["path"], columns=columns).to_pandas().reset_index(drop=True)
        frames.append((item, frame))
    return frames


def plot_filter_removal_maps(source_frames, recipe_by_name, sources, figures_dir) -> dict:
    """One map per filter showing the points removed at that step (all catalogues)."""
    figures_dir = Path(figures_dir)
    figures_dir.mkdir(parents=True, exist_ok=True)
    by_rule: dict[str, tuple[list, list]] = {}
    order: list[str] = []
    for item, frame in source_frames:
        catalogue = recipe_by_name.get(Path(item["path"]).name, {})
        rules = catalogue.get("rules", [])
        for rule_id, idx in _replay_removed(frame, rules).items():
            if rule_id not in by_rule:
                by_rule[rule_id] = ([], [])
                order.append(rule_id)
            by_rule[rule_id][0].extend(frame["sar_lon"].loc[idx].to_numpy())
            by_rule[rule_id][1].extend(frame["sar_lat"].loc[idx].to_numpy())
    rule_names = {}
    for item in sources:
        catalogue = recipe_by_name.get(Path(item["path"]).name, {})
        for rule in catalogue.get("rules", []):
            rule_names[rule["id"]] = rule["name"]

    result: dict = {}
    for rule_id in order:
        lon = np.asarray(by_rule[rule_id][0], dtype=float)
        lat = np.asarray(by_rule[rule_id][1], dtype=float)
        valid = np.isfinite(lon) & np.isfinite(lat)
        if not valid.any():
            continue
        # Subsample very dense removals so the raw points stay legible.
        cap = 120_000
        if valid.sum() > cap:
            choice = np.random.default_rng(0).choice(valid.sum(), cap, replace=False)
            plot_lon = lon[valid][choice]
            plot_lat = lat[valid][choice]
        else:
            plot_lon, plot_lat = lon[valid], lat[valid]
        figure, axis = plt.subplots(figsize=(9, 4.5))
        axis.scatter(plot_lon, plot_lat, s=0.6, alpha=0.25, color="#b2182b", linewidths=0)
        for polygon in _land_polygons():
            axis.add_patch(Polygon(polygon, facecolor="#e8e7e1", edgecolor="#b5b4ae",
                                   linewidth=0.35, zorder=3))
        axis.set(xlim=(-180, 180), ylim=(-90, 90), aspect="equal",
                 xlabel="Longitude (degrees)", ylabel="Latitude (degrees)",
                 title=f"Points removed — {rule_names.get(rule_id, rule_id)} "
                       f"({int(valid.sum()):,} points)")
        axis.tick_params(labelsize=8)
        figure.tight_layout()
        path = figures_dir / f"removed_{rule_id}.png"
        figure.savefig(path, dpi=150)
        plt.close(figure)
        result[rule_id] = {"path": path, "removed": int(valid.sum()),
                           "name": rule_names.get(rule_id, rule_id)}
    return result


def plot_initial_vs_final(values_initial, values_final, figures_dir) -> Path | None:
    """Overlay the source (pre-curation) and curated (final) altimeter Hs."""
    figures_dir = Path(figures_dir)
    figures_dir.mkdir(parents=True, exist_ok=True)
    initial = np.asarray(values_initial, dtype=float)
    final = np.asarray(values_final, dtype=float)
    initial = initial[np.isfinite(initial)]
    final = final[np.isfinite(final)]
    if initial.size == 0 and final.size == 0:
        return None
    low = min([float(initial.min()) if initial.size else 0.0,
               float(final.min()) if final.size else 0.0])
    high = max([float(initial.max()) if initial.size else 1.0,
                float(final.max()) if final.size else 1.0])
    if low == high:
        low, high = low - 0.5, high + 0.5
    edges = np.linspace(low, high, 61)
    init_counts, _ = np.histogram(initial, bins=edges)
    final_counts, _ = np.histogram(final, bins=edges)
    init_pct = init_counts * (100.0 / initial.sum()) if initial.size else init_counts * 0
    final_pct = final_counts * (100.0 / final.sum()) if final.size else final_counts * 0
    figure, axis = plt.subplots(figsize=(9, 5.0))
    width = (edges[1] - edges[0])
    axis.bar(edges[:-1] + width / 2, init_pct, width=width, color="#cccccc",
             label=f"source / initial (n={initial.sum():,})", align="center")
    axis.bar(edges[:-1] + width / 2, final_pct, width=width, color="#0072B2",
             label=f"curated / final (n={final.sum():,})", align="center")
    axis.set(xlabel="Altimeter significant wave height (m)",
             ylabel="Rows (%)", title="Altimeter SWH: initial (source) vs final (curated)")
    axis.legend(frameon=False)
    axis.grid(axis="y", alpha=0.2)
    figure.tight_layout()
    path = figures_dir / "alti_swh_initial_vs_final.png"
    figure.savefig(path, dpi=150)
    plt.close(figure)
    return path


def _figure_block(path, caption, label):
    return (
        r"\begin{figure}[H]\centering" + "\n"
        + r"\includegraphics[width=0.97\textwidth]{" + path.name + "}\n"
        + r"\caption{" + caption + "}\n"
        + r"\label{" + label + r"}" + "\n"
        + r"\end{figure}"
    )


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
    split = plot_alti_split_figures(curated, assets_dir)
    figures.update(split)
    values = pd.to_numeric(curated[VALUE_COLUMN], errors="coerce").to_numpy(dtype=float)
    finite = values[np.isfinite(values)]
    summary = ({"mean": float(finite.mean()), "median": float(np.median(finite))}
               if finite.size else {})

    # Review additions: re-read the sources for the per-filter removal maps and the
    # initial-vs-final Hs distribution (the Curated Parquets only keep retained rows).
    removals: dict = {}
    initial_vs_final = None
    source_frames = _load_source_frames(manifest["sources"], recipe_by_name)
    removals = plot_filter_removal_maps(source_frames, recipe_by_name,
                                        manifest["sources"], assets_dir)
    initial_values = np.concatenate(
        [frame[VALUE_COLUMN].to_numpy(dtype=float) for _, frame in source_frames]
        if source_frames else [np.array([])])
    initial_vs_final = plot_initial_vs_final(initial_values, values, assets_dir)
    if initial_vs_final is not None:
        figures["initial_vs_final"] = initial_vs_final

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
        version_extra=True,
        coverage_extra=_coverage_extra_latex(assets_dir, split),
        monthly_extra=_monthly_extra_latex(assets_dir, split),
        distribution_extra=_distribution_extra_latex(assets_dir, initial_vs_final),
        filter_removal_figures=_removal_extra_latex(assets_dir, removals),
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


def _coverage_extra_latex(assets_dir, split):
    keys = sorted(k for k in split if k.startswith("coverage_"))
    if not keys:
        return ""
    db_label = {"CCI": "CCI SeaState V5", "CMEMS": "CMEMS ALTI-L3-VAVH"}
    blocks = [r"\noindent\textbf{Coverage by reference database and SAR unit.}",
              "One panel per SAR-unit / altimeter pair, grouped by database and satellite.",
              ""]
    for key in keys:
        db, mission = key.split("_")[1], key.split("_")[2]
        path = assets_dir / split[key].name
        blocks.append(_figure_block(
            path, f"Retained coverage — {mission}, {db_label.get(db, db)} "
                  f"(one panel per altimeter).", f"fig:cov_{db.lower()}_{mission.lower()}"))
    return "\n".join(blocks)


def _monthly_extra_latex(assets_dir, split):
    keys = sorted(k for k in split if k.startswith("monthly_"))
    if not keys:
        return ""
    db_label = {"CCI": "CCI SeaState V5", "CMEMS": "CMEMS ALTI-L3-VAVH"}
    blocks = [r"\noindent\textbf{Monthly counts by reference database and SAR unit.}",
              "Stacked by altimeter (the SAR-unit / altimeter pair), per database.",
              ""]
    for key in keys:
        db, mission = key.split("_")[1], key.split("_")[2]
        path = assets_dir / split[key].name
        blocks.append(_figure_block(
            path, f"Curated rows by month — {mission}, {db_label.get(db, db)}, "
                  f"stacked by altimeter.", f"fig:monthly_{db.lower()}_{mission.lower()}"))
    return "\n".join(blocks)


def _distribution_extra_latex(assets_dir, path):
    if path is None:
        return ""
    return "\n".join([
        r"\noindent\textbf{Initial vs final altimeter Hs.}",
        "The grey histogram is the source (pre-curation) distribution and the blue "
        "one the curated (final) distribution of the closest altimeter significant "
        "wave height; both are normalised to 100 %.",
        _figure_block(Path(path), "Altimeter SWH: initial (source) vs final (curated).",
                      "fig:hs_initial_vs_final"),
    ])


def _removal_extra_latex(assets_dir, removals):
    if not removals:
        return ""
    blocks = [r"\noindent\textbf{Points removed by each filter.}",
              "One map per quality filter, showing the observations dropped at that "
              "step across all catalogues (source catalogues re-read and filters "
              "replayed). Denser removals are subsampled for legibility.",
              ""]
    for rule_id, info in removals.items():
        path = assets_dir / info["path"].name
        blocks.append(_figure_block(
            path, f"Points removed by \\textbf{{{_tex(info['name'])}}} "
                  f"({info['removed']:,} points).",
            f"fig:removed_{rule_id.lower()}"))
    return "\n".join(blocks)


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
