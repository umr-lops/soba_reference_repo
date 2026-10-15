"""Shared plot helpers for mission-level reference distributions."""

import json

import numpy as np
from matplotlib import colors, ticker
from matplotlib.patches import Polygon

from .pdf_support import ASSET_DIR

MISSION_COLORS = {
    "S1A": "#0072B2",
    "S1B": "#E69F00",
    "S1C": "#009E73",
    "S1D": "#CC79A7",
}


def finish_density_map(figure, axis, cells, reference="HSCAT"):
    """Style accumulated counts on a one-degree-wide geographic hexagonal grid."""
    counts = cells.get_array()
    maximum = max(2, int(counts.max()))
    cells.set_array(np.ma.masked_where(counts == 0, counts))
    cells.set_norm(colors.LogNorm(vmin=1, vmax=maximum))
    cells.set_cmap(colors.LinearSegmentedColormap.from_list("soba_density", ["#d3e8e5", "#123a40"]))
    land = json.loads((ASSET_DIR / "maps/ne_110m_land.geojson").read_text())
    for feature in land["features"]:
        geometry = feature["geometry"]
        polygons = ([geometry["coordinates"]] if geometry["type"] == "Polygon"
                    else geometry["coordinates"])
        for polygon in polygons:
            axis.add_patch(Polygon(polygon[0], facecolor="#e8e7e1", edgecolor="#b5b4ae",
                                   linewidth=0.35, zorder=3))
    axis.set_aspect("equal")
    axis.set_xticks(np.arange(-180, 181, 60),
                    ["180°", "120°W", "60°W", "0°", "60°E", "120°E", "180°"])
    axis.set_yticks(np.arange(-90, 91, 30), ["90°S", "60°S", "30°S", "0°", "30°N", "60°N", "90°N"])
    axis.set(xlabel="", ylabel="",
             title=f"{int(counts.sum()):,} retained Sentinel-1 / {reference} matchups")
    axis.tick_params(labelsize=8)
    axis.set_axisbelow(True)
    ticks = [value for value in (1, 2, 3, 6, 10, 20, 30, 60, 100, 300, 1000) if value <= maximum]
    bar = figure.colorbar(cells, ax=axis, pad=0.025, fraction=0.035, ticks=ticks,
                         format=ticker.ScalarFormatter())
    bar.set_label("Matchups per 1°-wide hexagonal cell", fontsize=9)
    bar.ax.tick_params(labelsize=8)
    figure.tight_layout()


def plot_mission_distribution(axis, counts_by_mission, edges, colors):
    """Overlay per-mission percentages on shared bins, with counts in the legend."""
    peak = 0.0
    plotted = 0
    for mission, counts in counts_by_mission.items():
        total = int(counts.sum())
        if not total:
            continue
        percentages = counts * (100.0 / total)
        color = colors[mission]
        axis.stairs(
            percentages,
            edges,
            fill=False,
            linewidth=2.0,
            color=color,
            label=f"{mission} (n={total:,})",
        )
        peak = max(peak, float(percentages.max()))
        plotted += 1
    axis.set_ylabel("Mission TEST rows (%)")
    if plotted:
        axis.set_ylim(0, peak * 1.20)
        axis.legend(
            loc="upper center",
            bbox_to_anchor=(0.5, -0.16),
            ncol=min(plotted, 4),
            borderaxespad=0,
            frameon=False,
        )
