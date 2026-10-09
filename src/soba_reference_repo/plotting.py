"""Shared plot helpers for mission-level reference distributions."""

MISSION_COLORS = {
    "S1A": "#0072B2",
    "S1B": "#E69F00",
    "S1C": "#009E73",
    "S1D": "#CC79A7",
}


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
        axis.set_ylim(0, peak * 1.15)
        axis.legend(
            loc="upper center",
            bbox_to_anchor=(0.5, -0.16),
            ncol=min(plotted, 4),
            borderaxespad=0,
            frameon=False,
        )
