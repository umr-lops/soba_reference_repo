"""Shared plot helpers for mission-level reference distributions."""

import json

import matplotlib
import numpy as np
import pandas as pd
from matplotlib import colors, ticker
from matplotlib import pyplot as plt
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


# ---------------------------------------------------------------------------
# Figures of the co-aligned catalogue documents (any reference: scatterometer, altimeter, SWOT).
# ---------------------------------------------------------------------------

_LAND = None
# Both monthly figures share one canvas width and one axes box, so they line up in the document.
MONTHLY_WIDTH, MONTHLY_LEFT, MONTHLY_PLOT = 10.0, 1.0, 6.6      # inches
BLUES = {"S1A": "#c6dbef", "S1B": "#6baed6", "S1C": "#2171b5", "S1D": "#08306b"}


def _land():
    global _LAND
    if _LAND is None:
        data = json.loads((ASSET_DIR / "maps/ne_110m_land.geojson").read_text())
        _LAND = []
        for feature in data["features"]:
            geometry = feature["geometry"]
            polygons = ([geometry["coordinates"]] if geometry["type"] == "Polygon"
                        else geometry["coordinates"])
            _LAND.extend(polygon[0] for polygon in polygons)
    return _LAND


def _coverage_axis(axis, lon, lat):
    valid = np.isfinite(lon) & np.isfinite(lat)
    return axis.hexbin(lon[valid], lat[valid], gridsize=360, extent=(-180, 180, -90, 90),
                       mincnt=0, linewidths=0)


def figure_coverage(frame, path):
    figure, axis = plt.subplots(figsize=(9, 4.5))
    lon = pd.to_numeric(frame["sar_lon"], errors="coerce").to_numpy(dtype=float)
    lat = pd.to_numeric(frame["sar_lat"], errors="coerce").to_numpy(dtype=float)
    cells = _coverage_axis(axis, lon, lat)
    axis.set(xlim=(-180, 180), ylim=(-90, 90), xlabel="Longitude (degrees)",
             ylabel="Latitude (degrees)")
    figure.tight_layout()
    finish_density_map(figure, axis, cells, reference="reference")
    axis.set_title(f"{len(frame):,} Sentinel-1 WV matchups")
    figure.savefig(path, dpi=150)
    plt.close(figure)


def group_values(frame, column) -> list:
    """The values of a grouping present in the frame, most rows first (satellites in order)."""
    counts = frame[column].astype(str).value_counts()
    if column == "satellite":
        return [s for s in MISSION_COLORS if s in counts.index]
    return sorted(counts.index, key=lambda v: (-counts[v], v))


def figure_coverage_panels(frame, column, values, path):
    """World maps of ``values`` of one grouping, one centred panel each, colour bar on the right."""
    width, left, map_width = 10.0, 1.2, 7.6          # inches; the map is 2:1 and centred
    map_height = map_width / 2
    step = map_height + 0.9                          # room for the title and the gap
    height = step * len(values)
    figure = plt.figure(figsize=(width, height))
    cmap = matplotlib.colors.LinearSegmentedColormap.from_list(
        "soba_density", ["#d3e8e5", "#123a40"])
    for index, value in enumerate(values):
        bottom = 1 - (index * step + 0.5 + map_height) / height
        axis = figure.add_axes([left / width, bottom, map_width / width, map_height / height])
        subset = frame[frame[column].astype(str) == value]
        cells = _coverage_axis(
            axis, pd.to_numeric(subset["sar_lon"], errors="coerce").to_numpy(dtype=float),
            pd.to_numeric(subset["sar_lat"], errors="coerce").to_numpy(dtype=float))
        counts = cells.get_array()
        cells.set_array(np.ma.masked_where(counts == 0, counts))
        cells.set_norm(matplotlib.colors.LogNorm(vmin=1, vmax=max(2, int(counts.max()))))
        cells.set_cmap(cmap)
        for polygon in _land():
            axis.add_patch(Polygon(polygon, facecolor="#e8e7e1", edgecolor="#b5b4ae",
                                   linewidth=0.35, zorder=3))
        axis.set(xlim=(-180, 180), ylim=(-90, 90), title=f"{value} ({len(subset):,} matchups)")
        axis.set_xticks([])
        axis.set_yticks([])
        bar = figure.add_axes([(left + map_width + 0.12) / width, bottom, 0.18 / width,
                               map_height / height])
        figure.colorbar(cells, cax=bar).set_label("Matchups per cell")
    figure.savefig(path, dpi=130)
    plt.close(figure)


def _monthly_axes(figure, height, top, panel):
    """An axes of the common box: ``top`` and ``panel`` are heights in inches from the top."""
    return figure.add_axes([MONTHLY_LEFT / MONTHLY_WIDTH, 1 - (top + panel) / height,
                            MONTHLY_PLOT / MONTHLY_WIDTH, panel / height])


def _stack(axis, table, order, colours):
    labels = table.index.astype(str)
    bottom = pd.Series(0, index=table.index)
    for value in order:
        axis.bar(labels, table[value], bottom=bottom, color=colours[value], label=value)
        bottom += table[value]
    for label, total in zip(labels, bottom):
        axis.annotate(f"{int(total):,}", (label, total), ha="center", va="bottom", fontsize=8,
                      xytext=(0, 2), textcoords="offset points")
    axis.set_ylim(0, float(bottom.max()) * 1.15 if len(bottom) else 1)
    axis.tick_params(axis="x", labelsize=8)
    axis.grid(axis="y", alpha=0.2)
    axis.set_ylabel("Matchups")
    axis.legend(frameon=False, fontsize=8, loc="upper left", bbox_to_anchor=(1.02, 1.0))


def _monthly_table(frame, mask, column):
    months = pd.to_datetime(frame["sar_time"], errors="coerce", utc=True).dt.tz_localize(
        None).dt.to_period("M")
    return (pd.DataFrame({"month": months[mask], "group": frame[column].astype(str)[mask]})
            .dropna().groupby(["month", "group"]).size().unstack(fill_value=0).sort_index())


def figure_monthly(frame, path):
    """Rows per SAR month, one panel per reference product, stacked by Sentinel-1 satellite."""
    products = sorted(frame["product"].unique())
    panel, gap, top = 2.3, 0.7, 0.5
    height = top + len(products) * (panel + gap)
    figure = plt.figure(figsize=(MONTHLY_WIDTH, height))
    for index, product in enumerate(products):
        axis = _monthly_axes(figure, height, top + index * (panel + gap), panel)
        mask = (frame["product"] == product).to_numpy()
        table = _monthly_table(frame, mask, "satellite")
        _stack(axis, table, [s for s in BLUES if s in table], BLUES)
        axis.set_title(product)
    figure.savefig(path, dpi=150)
    plt.close(figure)


def figure_monthly_stacked(frame, column, title, path):
    """Rows per SAR month, stacked by the values of one grouping (blues)."""
    values = group_values(frame, column)
    table = _monthly_table(frame, np.ones(len(frame), dtype=bool), column)
    shades = dict(zip(values, matplotlib.colormaps["Blues"](np.linspace(0.3, 0.95, len(values)))))
    panel, top = 2.3, 0.5
    height = top + panel + 0.7
    figure = plt.figure(figsize=(MONTHLY_WIDTH, height))
    axis = _monthly_axes(figure, height, top, panel)
    _stack(axis, table, values, shades)
    axis.set_title(f"Matchups per month, by {title}")
    figure.savefig(path, dpi=150)
    plt.close(figure)


def figure_distribution(frame, column, label, unit, path):
    """Distribution of ``column`` by Sentinel-1 satellite (percent of each satellite's rows)."""
    values = pd.to_numeric(frame[column], errors="coerce").to_numpy(dtype=float)
    finite = np.isfinite(values)
    low, high = (float(values[finite].min()), float(values[finite].max())) if finite.any() else (
        0.0, 1.0)
    if low == high:
        low, high = low - 0.5, high + 0.5
    edges = np.linspace(low, high, 41)
    satellites = frame["satellite"].to_numpy()
    present = [s for s in MISSION_COLORS if ((satellites == s) & finite).any()]
    histogram = {s: np.histogram(values[(satellites == s) & finite], bins=edges)[0]
                 for s in present}
    figure, axis = plt.subplots(figsize=(9, 5.0))
    plot_mission_distribution(axis, histogram, edges, MISSION_COLORS)
    axis.set(xlabel=f"{label} ({unit})", title=f"{label}, by satellite")
    axis.set_ylabel("Rows per satellite (%)")
    axis.grid(axis="y", alpha=0.2)
    figure.tight_layout(rect=(0, 0.18, 1, 1))
    figure.savefig(path, dpi=150)
    plt.close(figure)
