"""SOBA co-aligned / TEST dataset report tool.

Cuts a SAR / scatterometer / SWOT crossing out of two coaligned catalogues,
draws three figures, fills the SOBA LaTeX template, compiles a PDF and exports
the spec-conformant WV TEST parquet.

Spec: "Format Description for parquet co-aligned datasets" (SOBA WP3, v1.0.3).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
import re
import shutil
import subprocess

# Must be set before any geopandas/GDAL import: silences the PROJ "ERROR 1" lookup noise.
os.environ.setdefault("CPL_LOG", "/dev/null")

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import FormatStrFormatter, MaxNLocator
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

PACKAGE_ROOT = Path(__file__).resolve().parents[2]  # .../soba_reference_repo
ASSET_DIR = PACKAGE_ROOT / "assets"
DEFAULT_LAND_MAP = ASSET_DIR / "ne_110m_land.geojson"
DEFAULT_LATEX_DIR = ASSET_DIR / "latex"
DEFAULT_TEST_DIR = PACKAGE_ROOT / "test_datasets"
# LaTeX is not assumed to be installed: --miktex-bin / $MIKTEX_BIN names a directory
# holding the pdflatex executable, otherwise it is taken from PATH (see find_pdflatex).

# Column names for the exported TEST file. The *Format Description* PDF writes the path and
# SAFE columns with hyphens (sar-path-ocn, sar-safe-slc), but the source catalogues and the
# consortium validator use underscores, and the validator is the acceptance gate — so the
# export follows the validator. Every other name is the spec's, unchanged.
WV_MANDATORY_COLUMNS = [
    "primary_key", "sar_time", "sar_lat", "sar_lon", "sar_incidence_angle",
    "sar_elevation_angle", "sar_ground_heading", "sar_distance_to_coast",
    "sar_path_ocn", "sar_path_slc", "sar_safe_slc", "sar_safe_ocn",
    "scat_lon", "scat_lat", "scat_time", "scat_flag", "scat_id", "legacy_usage",
]
WV_REF_PARAM_COLUMNS = ["scat_windspeed", "scat_winddirection", "swot_waveheight"]
ANCILLARY_COLUMNS = ["ecmwf_overlap", "ecmwf_rain_rate"]
ANCILLARY_SOURCE = "rain: IMERG HHL v7 NASA"

SCAT_COLUMNS = [
    "primary_key", "sar_safe_ocn", "sar_safe_slc", "sar_time", "sar_lat", "sar_lon",
    "sar_incidence_angle", "sar_elevation_angle", "sar_ground_heading",
    "sar_path_ocn", "sar_path_slc",
    "ref_time", "ref_lat", "ref_lon", "ref_param_1", "ref_param_2",
    "ref_distance_km", "ref_flag", "ref_id",
]
SWOT_COLUMNS = [
    "primary_key", "sar_safe_ocn", "sar_safe_slc", "sar_time", "sar_lat", "sar_lon",
    "sar_incidence_angle", "sar_elevation_angle", "sar_distance_to_coast",
    "sar_path_ocn", "sar_path_slc",
    "ref_time", "ref_lat", "ref_lon", "ref_mean_hs_karin",
    "ref_time_delta", "ref_distance_delta", "overlap_pct",
    "mean_rainrate_IMERG", "swot_dynamic_ice_flag", "swot_rain_flag",
    "ref_flag", "swot_cycle", "swot_pass", "legacy_usage",
]


@dataclass(frozen=True)
class ReportConfig:
    """Filter thresholds for one catalogue-report run."""

    overlap_min_pct: float = 100.0
    rain_max_mm_h: float = 0.3
    time_max_min: float = 120.0


@dataclass
class CrossingResult:
    """Everything one crossing produces."""

    satellite: str
    scatterometer: str
    scat_rows: int
    swot_rows: int
    crossing: pd.DataFrame
    filtered: pd.DataFrame


# --------------------------------------------------------------------------- #
# Scene key
# --------------------------------------------------------------------------- #

def _leaf(values) -> pd.Series:
    """The SAFE identifier without any archive-relative path prefix.

    The catalogues disagree on whether they store ``<collection>/<year>/<doy>/<SAFE>`` or
    the bare ``<SAFE>``: the prefixed form is an upstream data error that is being removed,
    so both the join key and the exported primary key are built from the leaf. That keeps a
    key identical before and after the catalogues are fixed, and is a no-op once they are.
    """
    return values.astype("string").str.rsplit("/", n=1).str[-1]


def make_scene_key(frame: pd.DataFrame, satellite: str) -> pd.Series:
    """Normalise a SAFE identifier to ``<acq>_<orbit>_<datatake>:WV_<imagette>``."""
    satellite = satellite.upper()
    blank = r"^\s*(?:nan)?\s*$"
    ocn = frame["sar_safe_ocn"].astype("string").replace(blank, pd.NA, regex=True)
    slc = frame["sar_safe_slc"].astype("string").replace(blank, pd.NA, regex=True)
    identifier = ocn.fillna(slc)
    if identifier.isna().any():
        raise ValueError("No SAR SAFE identifier in either sar_safe_ocn or sar_safe_slc")

    leaf = _leaf(identifier)
    prefix = rf"^{satellite}_WV_(?:OCN__2S|SLC__1S)S[HV]_"
    valid = leaf.str.match(prefix, na=False)
    if not valid.all():
        raise ValueError(
            f"SAFE identifiers do not match mission {satellite}: "
            f"{leaf.loc[~valid].head(3).tolist()}"
        )
    pieces = leaf.str.split(".SAFE:WV_", n=1, expand=True)
    acquisition = pieces[0].str.replace(prefix, "", regex=True)
    return acquisition.str.rsplit("_", n=1).str[0] + ":WV_" + pieces[1]


# --------------------------------------------------------------------------- #
# Crossing
# --------------------------------------------------------------------------- #

SCAT_RENAME = {
    "primary_key": "scat_primary_key",
    "sar_time": "sar_time_scat", "sar_lat": "sar_lat_scat", "sar_lon": "sar_lon_scat",
    "sar_incidence_angle": "sar_incidence_angle_scat",
    "sar_elevation_angle": "sar_elevation_angle_scat",
    "sar_ground_heading": "sar_ground_heading_scat",
    "sar_path_ocn": "sar_path_ocn_scat", "sar_path_slc": "sar_path_slc_scat",
    "sar_safe_ocn": "sar_safe_ocn_scat", "sar_safe_slc": "sar_safe_slc_scat",
    "ref_time": "scat_time", "ref_lat": "scat_lat", "ref_lon": "scat_lon",
    "ref_param_1": "scat_wind_direction_deg", "ref_param_2": "scat_wind_speed_ms",
    "ref_id": "scat_ref_id", "ref_flag": "scat_flag",
}
SWOT_RENAME = {
    "primary_key": "swot_primary_key",
    "sar_time": "sar_time_swot", "sar_lat": "sar_lat_swot", "sar_lon": "sar_lon_swot",
    "sar_incidence_angle": "sar_incidence_angle_swot",
    "sar_elevation_angle": "sar_elevation_angle_swot",
    "sar_distance_to_coast": "sar_distance_to_coast_swot",
    "sar_path_ocn": "sar_path_ocn_swot", "sar_path_slc": "sar_path_slc_swot",
    "sar_safe_ocn": "sar_safe_ocn_swot", "sar_safe_slc": "sar_safe_slc_swot",
    "ref_time": "swot_time", "ref_lat": "swot_lat", "ref_lon": "swot_lon",
    "ref_mean_hs_karin": "swot_wave_height_m", "legacy_usage": "swot_legacy_usage",
    "ref_flag": "swot_flag",
}
KEEP_SCAT = [
    "match_key", "scat_primary_key", "sar_time_scat", "sar_lat_scat", "sar_lon_scat",
    "sar_incidence_angle_scat", "sar_elevation_angle_scat", "sar_ground_heading_scat",
    "sar_path_ocn_scat", "sar_path_slc_scat", "sar_safe_ocn_scat", "sar_safe_slc_scat",
    "scat_time", "scat_lat", "scat_lon", "scat_wind_direction_deg",
    "scat_wind_speed_ms", "ref_distance_km", "scat_to_sar_time_min",
    "scat_flag", "scat_ref_id",
]
KEEP_SWOT = [
    "match_key", "sar_time_swot", "sar_lat_swot", "sar_lon_swot",
    "sar_incidence_angle_swot", "sar_elevation_angle_swot",
    "sar_distance_to_coast_swot", "sar_path_ocn_swot", "sar_path_slc_swot",
    "sar_safe_ocn_swot", "sar_safe_slc_swot",
    "swot_time", "swot_lat", "swot_lon", "swot_wave_height_m",
    "ref_time_delta", "ref_distance_delta", "swot_to_sar_time_min",
    "overlap_pct", "mean_rainrate_IMERG", "swot_dynamic_ice_flag",
    "swot_rain_flag", "swot_flag", "swot_cycle", "swot_pass", "swot_legacy_usage",
]


def haversine_km(lat1, lon1, lat2, lon2):
    """Great-circle distance in km."""
    lat1_rad, lat2_rad = np.radians(lat1), np.radians(lat2)
    delta_lat = lat2_rad - lat1_rad
    delta_lon = np.radians((lon2 - lon1 + 180) % 360 - 180)
    a = (
        np.sin(delta_lat / 2) ** 2
        + np.cos(lat1_rad) * np.cos(lat2_rad) * np.sin(delta_lon / 2) ** 2
    )
    return 6371.0088 * 2 * np.arcsin(np.sqrt(np.clip(a, 0, 1)))


def _dedup(frame: pd.DataFrame, key_column: str, time_column: str, tie_break: str) -> pd.DataFrame:
    """Keep one row per key, the reference closest in time to the SAR acquisition."""
    frame = frame.copy()
    frame[time_column] = (
        pd.to_datetime(frame["ref_time"], utc=True)
        - pd.to_datetime(frame["sar_time"], utc=True)
    ).abs().dt.total_seconds() / 60
    return (
        frame.sort_values([key_column, time_column, tie_break], na_position="last")
        .drop_duplicates(key_column, keep="first")
        .copy()
    )


def run_crossing(
    scat_path: Path,
    swot_path: Path,
    satellite: str,
    config: ReportConfig,
    scatterometer: str = "ASCAT",
    use_scene_key: bool = False,
) -> CrossingResult:
    """Cross a scatterometer catalogue and the SWOT catalogue through the SAR imagette.

    By default the two catalogues are matched on their own ``primary_key``, the identifier
    they both ship and agree on for a shared imagette. ``use_scene_key`` matches on the
    normalised imagette key instead, which bridges the two ways the catalogues can disagree:
    a different SAFE form (path prefix, checksum) or a different reference point chosen for
    the same imagette.
    """
    satellite = satellite.upper()
    scatterometer = scatterometer.upper()

    for name, path, columns in (
        (scatterometer, Path(scat_path), SCAT_COLUMNS),
        ("SWOT", Path(swot_path), SWOT_COLUMNS),
    ):
        available = set(pq.ParquetFile(path).schema_arrow.names)
        missing = sorted(set(columns) - available)
        if missing:
            raise ValueError(f"{name} catalogue {path.name} is missing columns: {missing}")

    scat = pd.read_parquet(scat_path, columns=sorted(SCAT_COLUMNS))
    swot = pd.read_parquet(swot_path, columns=sorted(SWOT_COLUMNS))
    for frame in (scat, swot):
        frame["match_key"] = (
            make_scene_key(frame, satellite)
            if use_scene_key
            else frame["primary_key"].astype("string")
        )

    scat = _dedup(scat, "match_key", "scat_to_sar_time_min", "ref_distance_km")
    swot = _dedup(swot, "match_key", "swot_to_sar_time_min", "ref_distance_delta")

    matches = scat.rename(columns=SCAT_RENAME)[KEEP_SCAT].merge(
        swot.rename(columns=SWOT_RENAME)[KEEP_SWOT],
        on="match_key", how="inner", validate="one_to_one",
    )
    if matches["match_key"].duplicated().any():
        raise ValueError("crossing is not unique per match key")

    matches["scat_swot_distance_km"] = haversine_km(
        matches["scat_lat"], matches["scat_lon"], matches["swot_lat"], matches["swot_lon"]
    )
    matches["scat_swot_time_delta_min"] = (
        pd.to_datetime(matches["scat_time"], utc=True)
        - pd.to_datetime(matches["swot_time"], utc=True)
    ).abs().dt.total_seconds() / 60

    filtered = matches[
        (matches["overlap_pct"] == config.overlap_min_pct)
        & (matches["mean_rainrate_IMERG"] < config.rain_max_mm_h)
        & (matches["scat_swot_time_delta_min"] < config.time_max_min)
    ].copy()

    return CrossingResult(
        satellite=satellite,
        scatterometer=scatterometer,
        scat_rows=len(scat),
        swot_rows=len(swot),
        crossing=matches,
        filtered=filtered,
    )


# --------------------------------------------------------------------------- #
# Figures
# --------------------------------------------------------------------------- #

ACCENT = "#2a626d"


def _wrap_longitude(values):
    return ((pd.to_numeric(values, errors="coerce") + 180.0) % 360.0) - 180.0


def _plot_geography(result: CrossingResult, path: Path, land_map_path: Path) -> None:
    import geopandas as gpd

    frame = result.filtered
    world = gpd.read_file(land_map_path)
    fig, ax = plt.subplots(figsize=(12, 6))
    world.plot(ax=ax, color="#d9d9d9", edgecolor="#9a9a9a", linewidth=0.3, zorder=1)

    if len(frame):
        hexes = ax.hexbin(
            frame["sar_lon_scat"], frame["sar_lat_scat"],
            gridsize=360, extent=(-180, 180, -90, 90),
            cmap="bone_r", bins="log", mincnt=1, linewidths=0.3, zorder=2,
        )
        fig.colorbar(hexes, ax=ax, shrink=0.8).set_label(
            "Matchups per 1° hexagonal cell (log scale)"
        )

    ax.scatter(
        frame["sar_lon_scat"], frame["sar_lat_scat"], s=4, color=ACCENT,
        alpha=0.85, edgecolor="black", linewidth=0.4, zorder=3,
        label=f"Filtered crossing scenes (N={len(frame):,})",
    )
    ax.set(
        title=(
            f"{result.satellite} / SWOT / {result.scatterometer}: "
            "geographical distribution of the filtered crossing"
        ),
        xlabel="Longitude (°)", ylabel="Latitude (°)",
        xlim=(-180, 180), ylim=(-90, 90),
    )
    ax.set_xticks(np.arange(-180, 181, 60))
    ax.set_yticks(np.arange(-90, 91, 30))
    ax.set_aspect("equal", adjustable="box")
    ax.legend(loc="upper right", markerscale=2)
    fig.tight_layout()
    fig.savefig(path, dpi=160, bbox_inches="tight")
    plt.close(fig)


def _plot_monthly(result: CrossingResult, path: Path) -> None:
    frame = result.filtered
    if len(frame):
        months = pd.to_datetime(frame["sar_time_scat"], utc=True).dt.strftime("%Y-%m")
        counts = months.value_counts().sort_index()
    else:
        counts = pd.Series(dtype="int64")

    fig, ax = plt.subplots(figsize=(10, 5))
    ax.bar(counts.index.astype(str), counts.values, color=ACCENT, alpha=0.85)
    ax.set(
        title=(
            f"{result.satellite} / SWOT / {result.scatterometer}: monthly distribution "
            f"of the filtered crossing (N={len(frame):,})"
        ),
        xlabel="Month", ylabel="matchups",
    )
    ax.yaxis.set_major_locator(MaxNLocator(integer=True))
    plt.xticks(rotation=45, ha="right")
    fig.tight_layout()
    fig.savefig(path, dpi=160, bbox_inches="tight")
    plt.close(fig)


# Section boundaries of the reference-distribution figure, per reference variable. The
# last boundary of each tuple is followed by an open-ended section; variables without an
# entry split into equal thirds of the axis.
REFERENCE_BINS: dict[str, tuple[float, ...]] = {
    "scat_wind_speed_ms": (5.0, 10.0, 15.0),        # 0-5, 5-10, 10-15, >15 m/s
    "swot_wave_height_m": (1.0, 3.0, 5.0, 10.0),    # 0-1, 1-3, 3-5, 5-10, >10 m
}


def section_bounds(column: str, xmin: float, xmax: float) -> np.ndarray:
    """Section boundaries for one reference variable, as ``-inf … +inf`` edges.

    Fixed bins where the variable calls for them (wind speed, wave height), equal thirds
    of the axis otherwise (wind direction).
    """
    fixed = REFERENCE_BINS.get(column)
    edges = np.asarray(fixed, dtype=float) if fixed else np.linspace(xmin, xmax, 4)[1:-1]
    return np.concatenate(([-np.inf], edges, [np.inf]))


def _plot_reference_distributions(result: CrossingResult, path: Path) -> None:
    frame = result.filtered
    fig, axes = plt.subplots(1, 3, figsize=(16, 4.5))
    panels = [
        ("scat_wind_speed_ms", np.arange(0, 40.1, 1),
         f"{result.scatterometer} wind speed (m/s)"),
        ("scat_wind_direction_deg", np.arange(0, 360.1, 10),
         f"{result.scatterometer} wind direction (°)"),
        ("swot_wave_height_m", np.arange(0, 20.1, 0.5), "SWOT KaRIn wave height (m)"),
    ]
    for ax, (column, bins, label) in zip(axes, panels):
        values = frame[column].dropna()
        ax.hist(values, bins=bins, color=ACCENT, alpha=0.85)
        ax.set_title(f"{label}\nn = {len(values):,}", fontsize=11)
        ax.set_xlabel(label)
        ax.set_ylabel("matchups")
        ax.yaxis.set_major_locator(MaxNLocator(integer=True))
        if len(values):
            ax.axvline(values.median(), color="orange", linewidth=1)
            ax.text(values.median(), 0.80, f"median = {values.median():.2f}", color="orange",
                ha="center", va="top", transform=ax.get_xaxis_transform(), fontsize=9,)

            # sections: fixed bins for wind speed and wave height, equal thirds otherwise.
            # Only the part of a section inside the axis can be drawn, so an empty
            # open-ended section (e.g. "> 15 m/s") simply does not appear.
            # zoom to the data, keeping the last bin edge visible when the data stops
            # short of it: an empty top bin should not own most of the axis
            fixed = REFERENCE_BINS.get(column)
            if fixed:
                ax.set_xlim(0, max(float(values.max()), float(fixed[-1])))
            elif column == "scat_wind_direction_deg":
                ax.set_xlim(0, 360)  # the natural domain of a direction
            xmin, xmax = ax.get_xlim()
            bounds = section_bounds(column, xmin, xmax)
            # tick the section boundaries, so the bins are readable off the axis
            inner = bounds[np.isfinite(bounds)]
            ax.set_xticks(np.unique(np.round(np.concatenate([[xmin, xmax], inner]), 6)))
            ax.xaxis.set_major_formatter(FormatStrFormatter("%g"))
            for i in range(len(bounds) - 1):
                left, right = bounds[i], bounds[i + 1]
                visible_left, visible_right = max(left, xmin), min(right, xmax)
                if visible_right <= visible_left:
                    continue
                count = ((values >= left) & (values < right)).sum()
                ax.axvspan(visible_left, visible_right, color="lightgray" if i % 2 == 0 else "white",
                           alpha=0.15, zorder=-1)
                # the sections are as narrow as the bins, so the count is written top-down:
                # a horizontal label would collide with its neighbour
                ax.text((visible_left + visible_right) / 2, 0.99, f"N = {count:,}",
                    transform=ax.get_xaxis_transform(), rotation=90,
                    ha="center", va="top", fontsize=10, fontweight="bold" )
                if i > 0:
                    ax.axvline(left, color="gray", linestyle="--", linewidth=0.8, alpha=0.7)

    fig.suptitle(
        f"{result.satellite} / SWOT / {result.scatterometer}: "
        "reference-variable distributions of the filtered crossing",
        y=1.04, fontsize=13,
    )
    fig.tight_layout()
    fig.savefig(path, dpi=160, bbox_inches="tight")
    plt.close(fig)


def plot_figures(
    result: CrossingResult,
    figures_dir: Path,
    land_map_path: Path = DEFAULT_LAND_MAP,
) -> list[Path]:
    """Draw the three report figures and return their paths."""
    figures_dir = Path(figures_dir)
    figures_dir.mkdir(parents=True, exist_ok=True)
    if not Path(land_map_path).is_file():
        raise FileNotFoundError(f"land map not found: {land_map_path}")

    paths = [
        figures_dir / "geographical_distribution.png",
        figures_dir / "monthly_distribution.png",
        figures_dir / "reference_distributions.png",
    ]
    _plot_geography(result, paths[0], Path(land_map_path))
    _plot_monthly(result, paths[1])
    _plot_reference_distributions(result, paths[2])
    return paths


# --------------------------------------------------------------------------- #
# TEST dataset filename
# --------------------------------------------------------------------------- #

SOURCE_CATALOGUE_PATTERN = re.compile(
    r"^(?P<satellite>S1[ABCD])_coaligned_catalogue_(?P<sar_mode>WV|IW|EW)_"
    r"(?P<start>\d{8})_(?P<stop>\d{8})_(?P<production>\d{8})_"
    r"(?P<polarization>S[VH]|D[VH])_(?P<ref_product>.+)_(?P<version>\d+\.\d+)\.parquet$"
)
SAFE_LEAF_PATTERN = re.compile(
    r"^(?P<satellite>S1[ABCD])_(?P<sar_mode>WV|IW|EW)_"
    r"(?:OCN__2S|SLC__1S)(?P<polarization>[SD][VH])_"
)


def parse_source_catalogue_name(name: str) -> dict[str, str]:
    """Parse a co-aligned catalogue filename into its naming-convention fields."""
    match = SOURCE_CATALOGUE_PATTERN.match(Path(name).name)
    if match is None:
        raise ValueError(
            f"cannot parse the reference catalogue filename {name!r}; "
            "pass --test-name explicitly"
        )
    return match.groupdict()


def derive_sar_identity(result: CrossingResult) -> tuple[str, str]:
    """Return ``(sar_mode, polarization)`` read from the crossing's SAFE identifiers."""
    frame = result.filtered if len(result.filtered) else result.crossing
    leaves = frame["sar_safe_slc_scat"].dropna().astype("string")
    if leaves.empty:
        raise ValueError("no SAR SAFE identifier available to derive the dataset name")
    leaf = leaves.iloc[0].rsplit("/", 1)[-1]
    match = SAFE_LEAF_PATTERN.match(leaf)
    if match is None:
        raise ValueError(f"cannot derive the SAR mode and polarization from {leaf!r}")
    return match.group("sar_mode"), match.group("polarization")


def build_test_filename(
    satellite: str,
    sar_mode: str,
    polarization: str,
    ref_product: str,
    sar_times: pd.Series,
    version: str = "0.1",
    production_date=None,
) -> str:
    """Apply the SOBA naming convention for a reference TEST dataset.

    ``S1{A,B,C,D}_reference_test_dataset_<sarmode>_<startdate>_<stopdate>_
    <productiondate>_<polarization>_<refproductname1>_<refproductname2>_<version>.parquet``

    ``startdate``/``stopdate`` are the first and last SAR starting dates present
    in the TEST dataset itself; ``productiondate`` is the day the file is written.
    ``ref_product`` carries both reference product names, joined by ``_``.
    """
    times = pd.to_datetime(sar_times, utc=True)
    if times.empty or times.isna().all():
        raise ValueError("cannot name the TEST dataset: the cohort has no SAR times")
    production = (
        pd.Timestamp(production_date)
        if production_date is not None
        else pd.Timestamp.now(tz="UTC")
    )
    return (
        f"{satellite.upper()}_reference_test_dataset_{sar_mode}_"
        f"{times.min().strftime('%Y%m%d')}_{times.max().strftime('%Y%m%d')}_"
        f"{production.strftime('%Y%m%d')}_{polarization}_{ref_product}_{version}.parquet"
    )


def default_test_name(
    result: CrossingResult,
    scat_name: str,
    swot_name: str,
    satellite: str,
    version: str = "0.1",
) -> str:
    """Build the TEST filename from the crossing and both reference catalogue names.

    The crossing has two reference products, so the convention's
    ``<refproductname>`` field carries both: the scatterometer first, SWOT second.
    """
    references = []
    for name in (scat_name, swot_name):
        source = parse_source_catalogue_name(name)
        if source["satellite"].upper() != satellite.upper():
            raise ValueError(
                f"--satellite {satellite} does not match the catalogue "
                f"({source['satellite']})"
            )
        references.append(source["ref_product"])

    sar_mode, polarization = derive_sar_identity(result)
    frame = result.filtered if len(result.filtered) else result.crossing
    return build_test_filename(
        satellite, sar_mode, polarization, "_".join(references),
        frame["sar_time_scat"], version,
    )


LATEX_BYPRODUCT_SUFFIXES = (".aux", ".log", ".out", ".toc")


def purge_latex_byproducts(output_dir: Path, stem: str) -> list[Path]:
    """Delete the LaTeX byproducts of ``stem`` from the build directory.

    The ``.tex``, the figures and the staged assets are kept so the report can be
    hand-edited and recompiled; only the compiler's own scratch files go.
    """
    output_dir = Path(output_dir)
    removed: list[Path] = []
    for suffix in LATEX_BYPRODUCT_SUFFIXES:
        candidate = output_dir / f"{stem}{suffix}"
        if candidate.is_file():
            candidate.unlink()
            removed.append(candidate)
    return removed


# --------------------------------------------------------------------------- #
# TEST parquet export
# --------------------------------------------------------------------------- #

def _second_precision(values) -> pd.Series:
    """Timestamps at whole-second precision, UTC, timezone-naive.

    The spec fixes the *format* (YYYY-mm-dd HH:MM:SS, UTC). A typed timestamp renders that
    way while staying sortable, and it is what the validator asks for — a string column
    warns.
    """
    return (
        pd.to_datetime(values, utc=True)
        .dt.tz_convert(None)
        .dt.floor("s")
        .astype("datetime64[ns]")  # the declared dtype, whatever the source unit was
    )


def _one_decimal(values) -> pd.Series:
    """Fixed one-decimal string form, as the reference position takes inside a key."""
    return pd.to_numeric(values, errors="coerce").round(1).map("{:.1f}".format)


def _primary_key(frame: pd.DataFrame, ref_lon, ref_lat) -> pd.Series:
    """The row identifier: the scatterometer catalogue's own ``primary_key``.

    The catalogues ship that column in the spec's form — SLC SAFE name, imagette number and
    the reference position at one decimal — and two catalogues agree on it for a shared
    imagette, so it is carried through as-is instead of being recomposed. Composing it from
    ``ref_lon``/``ref_lat`` does *not* reproduce it: the scatterometer catalogues derive those
    key coordinates slightly differently from their own reference columns. The composition
    stays as a fallback for a catalogue that does not ship the column.
    """
    if "scat_primary_key" in frame.columns:
        keys = frame["scat_primary_key"].astype("string")
        if keys.notna().all():
            return keys
    return (
        _leaf(frame["sar_safe_slc_scat"])
        + "_" + _one_decimal(ref_lon)
        + "_" + _one_decimal(ref_lat)
    )


def _wrap_heading(values) -> pd.Series:
    """Ground heading as clockwise from north, in [0, 360).

    The catalogue delivers it signed — negative west of north — while the spec and the
    validator use the 0-360 convention.
    """
    return pd.to_numeric(values, errors="coerce").mod(360.0).round(4)


def build_test_frame(result: CrossingResult) -> pd.DataFrame:
    """Reshape the filtered crossing into the mandatory WV TEST layout."""
    frame = result.filtered
    ref_lon = _wrap_longitude(frame["scat_lon"])
    ref_lat = pd.to_numeric(frame["scat_lat"])

    test = pd.DataFrame({
        # primary key: the catalogue's own identifier for the matched reference — see
        # _primary_key. It is the spec's <SAFE>:WV_<imagette>_<ref_lon>_<ref_lat> form.
        "primary_key": _primary_key(frame, ref_lon, ref_lat),
        "sar_time": _second_precision(frame["sar_time_scat"]),
        "sar_lat": pd.to_numeric(frame["sar_lat_scat"]).round(6),
        "sar_lon": _wrap_longitude(frame["sar_lon_scat"]).round(6),
        "sar_incidence_angle": frame["sar_incidence_angle_scat"],
        "sar_elevation_angle": frame["sar_elevation_angle_scat"],
        "sar_ground_heading": _wrap_heading(frame["sar_ground_heading_scat"]),
        "sar_distance_to_coast": frame["sar_distance_to_coast_swot"],
        "sar_path_ocn": frame["sar_path_ocn_scat"],
        "sar_path_slc": frame["sar_path_slc_scat"],
        # the SAFE columns carry the name alone: the archive path prefix the catalogues
        # sometimes prepend is not part of the SAFE name, and the validator's patterns
        # anchor on the collection tag immediately after the satellite prefix.
        "sar_safe_slc": _leaf(frame["sar_safe_slc_scat"]),
        "sar_safe_ocn": _leaf(frame["sar_safe_ocn_scat"]),
        # the reference family is named after its source: <ref>_lon / _lat / _time, with
        # <ref> = scat for the scatterometer and swot for the KaRIn side (spec v1.1.0).
        "scat_lon": ref_lon.round(4),
        "scat_lat": ref_lat.round(4),
        "scat_windspeed": frame["scat_wind_speed_ms"],
        "scat_winddirection": frame["scat_wind_direction_deg"],
        "scat_time": _second_precision(frame["scat_time"]),
        "scat_flag": frame["scat_flag"],
        "scat_id": frame["scat_ref_id"],
        "legacy_usage": frame["swot_legacy_usage"],
        "swot_waveheight": frame["swot_wave_height_m"],
        "swot_lon": _wrap_longitude(frame["swot_lon"]).round(4),
        "swot_lat": pd.to_numeric(frame["swot_lat"]).round(4),
        "swot_time": _second_precision(frame["swot_time"]),
        "swot_flag": frame["swot_flag"],
        # ancillary products, kept in their own table in the report
        "ecmwf_overlap": frame["overlap_pct"],
        "ecmwf_rain_rate": frame["mean_rainrate_IMERG"],
        "scat_swot_distance_km": frame["scat_swot_distance_km"],
        "scat_swot_time_delta_min": frame["scat_swot_time_delta_min"],
    })

    missing = [column for column in WV_MANDATORY_COLUMNS if column not in test.columns]
    if missing:
        raise ValueError(f"missing mandatory WV TEST columns: {missing}")
    if not set(WV_REF_PARAM_COLUMNS) <= set(test.columns):
        raise ValueError("missing dataset-specific ref_param columns")
    if not test["primary_key"].is_unique:
        raise ValueError("primary_key is not unique")
    if not test["primary_key"].str.match(r".*\.SAFE:WV_\d+_-?\d+\.\d_-?\d+\.\d$").all():
        raise ValueError("primary_key is not <SAFE>:WV_<imagette>_<ref_lon>_<ref_lat>")
    for column in ("sar_lon", "scat_lon", "swot_lon"):
        if not test[column].between(-180, 180).all():
            raise ValueError(f"{column} outside [-180, 180]")
    return test


def library_version() -> str:
    """The version recorded in the parquet's global attributes.

    Prefers the git commit when the package runs from a checkout — the spec's example is
    ``commit <sha>`` or a tag — and falls back to the installed distribution version.
    """
    repository = Path(__file__).resolve().parents[2]
    try:
        described = subprocess.run(
            ["git", "-C", str(repository), "describe", "--tags", "--always", "--dirty"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        if described.returncode == 0 and described.stdout.strip():
            return f"commit {described.stdout.strip()}"
    except OSError:
        pass
    try:
        from importlib.metadata import version

        return f"version {version('soba_reference_repo')}"
    except Exception:
        return "unknown"


def write_test_parquet(test: pd.DataFrame, target: Path, source_scat: str = "") -> Path:
    """Write the TEST parquet with the SOBA mandatory global attributes.

    Attribute names follow the spec verbatim: ``source <ref>`` — ``<ref>`` replaced by the
    reference's short name, ``scat`` here — ``source ancillary datasets``, ``library used to
    produce the parquet``, ``library version`` and ``creation date``.
    """
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pandas(test, preserve_index=False)
    metadata = dict(table.schema.metadata or {})
    metadata.update({
        b"source scat": (source_scat or "unknown").encode(),
        b"source ancillary datasets": ANCILLARY_SOURCE.encode(),
        b"library used to produce the parquet": b"soba_reference_repo",
        b"library version": library_version().encode(),
        b"creation date": pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d").encode(),
    })
    pq.write_table(table.replace_schema_metadata(metadata), target)
    return target


# --------------------------------------------------------------------------- #
# LaTeX
# --------------------------------------------------------------------------- #

LATEX_AUXILIARY_FILES = [
    "soba.sty", "logo_soba.png"
]


def stage_latex_assets(latex_dir: Path, output_dir: Path) -> list[Path]:
    """Copy the template's companion files into the build directory.

    ``soba.sty``, ``logo_soba.png`` and the two ``\\input`` files must sit next
    to the document for pdflatex to resolve them.
    """
    latex_dir, output_dir = Path(latex_dir), Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    staged = []
    for name in LATEX_AUXILIARY_FILES:
        source = latex_dir / name
        if not source.is_file():
            raise FileNotFoundError(f"missing LaTeX asset: {source}")
        target = output_dir / name
        target.write_bytes(source.read_bytes())
        staged.append(target)
    return staged


def _replace_once(text: str, anchor: str, replacement: str) -> str:
    """Replace ``anchor`` exactly once, or fail loudly."""
    count = text.count(anchor)
    if count != 1:
        raise ValueError(f"anchor not found exactly once (found {count}): {anchor!r}")
    return text.replace(anchor, replacement)


# (label template, column of the filtered frame) for the reference summary table
REFERENCE_STATISTICS = (
    ("{scatterometer} wind speed (m/s)", "scat_wind_speed_ms"),
    ("{scatterometer} wind direction (°)", "scat_wind_direction_deg"),
    ("SWOT KaRIn wave height (m)", "swot_wave_height_m"),
)

STATS_DECIMALS = 2


def reference_statistics_table(
    result: CrossingResult, decimals: int = STATS_DECIMALS
) -> str:
    """LaTeX table with the min/max/mean/median of every reference variable."""
    frame = result.filtered
    rows = []
    for label, column in REFERENCE_STATISTICS:
        values = pd.to_numeric(frame[column], errors="coerce").dropna()
        label = label.format(scatterometer=result.scatterometer)
        if values.empty:
            rows.append(f"    {label} & -- & -- & -- & -- \\\\")
            continue
        rows.append(
            f"    {label} & {values.min():.{decimals}f} & {values.max():.{decimals}f}"
            f" & {values.mean():.{decimals}f} & {values.median():.{decimals}f} \\\\"
        )
    header = (
        "    \\textbf{Reference parameter} & \\textbf{Min} & \\textbf{Max} & "
        "\\textbf{Mean} & \\textbf{Median} \\\\"
    )
    return "\n".join(
        [
            "\\begin{longtable}{@{}p{6.4cm}rrrr@{}}",
            "    \\caption{Reference parameter statistics of the reference TEST dataset."
            "\\label{tab:reference_stats}} \\\\",
            "    \\toprule",
            header,
            "    \\midrule",
            "    \\endfirsthead",
            "    \\toprule",
            header,
            "    \\midrule",
            "    \\endhead",
            "    \\bottomrule",
            "    \\endlastfoot",
            *rows,
            "\\end{longtable}",
        ]
    )


TEX_LINEBREAK = "\\" * 2  # LaTeX \\ : a line break inside a paragraph


def _path(value: str) -> str:
    """Wrap a file name in ``\\path|...|`` so long names wrap instead of overflowing."""
    return "\\path|" + value + "|"


def _table_float(text: str, caption: str) -> tuple[int, int, str]:
    """Locate the ``table`` float carrying ``caption``; return (start, end, block)."""
    anchor = f"\\caption{{{caption}}}"
    caption_at = text.index(anchor)
    start = text.rindex(r"\begin{table}", 0, caption_at)
    end = text.index(r"\end{table}", caption_at) + len(r"\end{table}")
    return start, end, text[start:end]


def insert_table_row(text: str, caption: str, row: str) -> str:
    """Append ``row`` as the last row of the table with that caption.

    Anchored on the caption rather than on a specific row, so editing the
    template's own rows does not break the build.
    """
    start, end, block = _table_float(text, caption)
    position = block.rindex(r"\bottomrule")
    row = row if row.startswith("        ") else f"        {row}"
    row = row if row.endswith(" \\\\") else f"{row} \\\\\n"
    return text[:start] + block[:position] + row + block[position:] + text[end:]


def insert_version_history_row(text: str, row: str) -> str:
    """Insert ``row`` into the documentation version table."""
    return insert_table_row(text, "Versioning of the documentation", row)


def build_report_tex(
    template_path: Path,
    result: CrossingResult,
    label: str,
    figure_names: list[str],
    config: ReportConfig = ReportConfig(),
    scat_name: str = "",
    swot_name: str = "",
    test_name: str = "",
    figures_dir: str = "figures",
    use_scene_key: bool = False,
) -> str:
    """Fill ``template.tex`` so the document describes this TEST dataset."""
    text = Path(template_path).read_text(encoding="utf-8")
    frame = result.filtered
    n = len(frame)
    distance = frame["scat_swot_distance_km"]
    times = frame["scat_swot_time_delta_min"]
    # the template supplies the words "reference TEST dataset" after this name
    dataset_name = f"{result.satellite} / SWOT / {result.scatterometer}"
    modification = (
        "TEST dataset extracted from the co-aligned catalogues "
        f"(overlap $\\geq$ {config.overlap_min_pct:g}\\%, "
        f"rain $<$ {config.rain_max_mm_h:g} mm/h, "
        f"$\\Delta t <$ {config.time_max_min:g} min); {n} rows."
    )

    # the template already provides the surrounding enumerate environment,
    # so only the \item lines are replaced.
    match_on = (
        "the normalised imagette scene key" if use_scene_key else "the catalogue primary key"
    )
    first_step = (
        r"\item \textbf{step 1:} normalise the SAFE identifiers to a stable imagette scene "
        r"key (acquisition, orbit, datatake, \texttt{:WV\_<imagette>});"
        if use_scene_key
        else r"\item \textbf{step 1:} read the catalogue's own \texttt{primary\_key}, which "
        r"already carries the SLC SAFE name, the imagette number and the reference position;"
    )
    steps = "\n".join([
        first_step,
        r"    \item \textbf{step 2:} keep one row per key, the reference closest in time to "
        r"the SAR acquisition;",
        rf"    \item \textbf{{step 3:}} inner-join the scatterometer and SWOT catalogues on "
        rf"{match_on} (validated one-to-one merge);",
        r"    \item \textbf{step 4:} compute the direct scatterometer--SWOT distance and "
        r"time difference;",
        r"    \item \textbf{step 5:} apply the quality filters and export the TEST parquet.",
    ])

    edits = [
        (r"\hl{dataset name}", dataset_name),
        (
            r"\hl{fill this part with product name, provider, resolution, SAR mode, "
            r"polarization, unit ...etc}",
            f"TEST dataset derived from a {result.satellite} Wave Mode (WV) SAR crossing "
            f"with KNMI {result.scatterometer} winds and SWOT KaRIn wave heights. "
            f"Wind speed in m/s, significant wave height in m. Retained rows: {n}.",
        ),
        (r"(\hl{IW and WV modes})", r"(WV mode)"),
        (
            r"\hl{<geophysical parameters>}",
            "scatterometer wind speed and direction, and SWOT KaRIn significant wave height",
        ),
        (
            r"\hl{path or DOI or dataset name}",
            f"{_path(scat_name)}{TEX_LINEBREAK}\n    {_path(swot_name)}",
        ),
        (r"\hl{WV, IW , EW}", "WV"),
        (r"\hl{VV, VH, HH, HV }", "VV"),
        (
            r"\hl{KNMI-SCAT-HY2B-25km (scatterometer wind), altimetry-derived wave "
            r"heights, or model reanalysis data (e.g., ERA5).}",
            f"{_path(scat_name)} (scatterometer wind){TEX_LINEBREAK}\n    "
            f"{_path(swot_name)} (KaRIn significant wave height).",
        ),
        (
            r"\item \textbf{geographic delta co-location criteria:}  \hl{to be filled}",
            r"\item \textbf{geographic delta co-location criteria:} no hard distance "
            f"threshold; measured scatterometer--SWOT separation min {distance.min():.1f} km, "
            f"median {distance.median():.1f} km, max {distance.max():.1f} km",
        ),
        (
            r"\item \textbf{time delta co-location criteria:}  \hl{to be filled}",
            r"\item \textbf{time delta co-location criteria:} direct scatterometer--SWOT "
            f"time difference $< {config.time_max_min:g}$ min "
            f"(max observed {times.max():.1f} min)",
        ),
        (
            r"\item \textbf{step 1:} \hl{to be filled}" "\n"
            r"    \item \textbf{step 2:} \hl{to be filled}" "\n"
            r"    \item ...",
            steps,
        ),
        (r"\hl{github/gitlab link}", r"\texttt{Link to Github tool}"),
        (r"\hl{to be modified}", "used for this dataset"),
        (
            r"\hl{reference parameter statistics}",
            reference_statistics_table(result),
        ),
        (
            "\\begin{lstlisting}[caption={Example of bash cmd for data generation.}, "
            "label={lst:code}]\nexample of command used.\n\\end{lstlisting}",
            "\\begin{lstlisting}[caption={Command used to generate this dataset.}, "
            "label={lst:code}]\n"
            "soba_reference_repo \\\n"
            f"  --scat {scat_name} \\\n"
            f"  --swot {swot_name} \\\n"
            f"  --satellite {result.satellite} "
            f"--scatterometer {result.scatterometer} \\\n"
            "\\end{lstlisting}",
        ),
    ]
    for anchor, replacement in edits:
        text = _replace_once(text, anchor, replacement)

    # figure placeholders -> real includegraphics
    figure_anchors = [
        r"\fbox{\parbox{\textwidth}{\centering \vspace{4cm} \textit{[Insert Global "
        r"Coverage Map Image Here]} \vspace{4cm}}}",
        r"\fbox{\parbox{0.8\textwidth}{\centering \vspace{3cm} \textit{[Insert Monthly "
        r"Barchart Image Here]} \vspace{3cm}}}",
        r"\fbox{\parbox{0.8\textwidth}{\centering \vspace{3cm} \textit{[Insert 1D or 2D "
        r"Histogram of Reference Variables Here]} \vspace{3cm}}}",
    ]
    widths = [r"\textwidth", r"0.85\textwidth", r"\textwidth"]
    for anchor, name, width in zip(figure_anchors, figure_names, widths):
        text = _replace_once(
            text, anchor, f"\\includegraphics[width={width}]{{{figures_dir}/{name}}}"
        )

    # the config listing must show the values this run actually used
    start = text.index(r"\begin{lstlisting}[caption={Example of configuration file")
    end = text.index(r"\end{lstlisting}", start) + len(r"\end{lstlisting}")
    text = text[:start] + (
        "\\begin{lstlisting}[caption={Configuration used for this dataset "
        "(config.yaml).}, label={lst:config}]\n"
        f"# SOBA catalogue report - {label}\n"
        'sar_products:\n  mode: "WV"\n  polarizations: ["VV"]\n\n'
        "reference_products:\n"
        f'  - name: "{scat_name}"\n    type: "scatterometer"\n'
        '    variables: ["scat_windspeed", "scat_winddirection"]\n'
        f'  - name: "{swot_name}"\n    type: "wave_height"\n'
        '    variables: ["swot_waveheight"]\n\n'
        "filters:\n"
        f"  overlap_pct: {config.overlap_min_pct:g}\n"
        f"  max_rainrate_mm_h: {config.rain_max_mm_h:g}\n"
        f"  max_time_diff_minutes: {config.time_max_min:g}\n"
        "\\end{lstlisting}"
    ) + text[end:]

    # version-history rows for the generated document and its dataset file
    today = pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d")
    text = insert_table_row(
        text,
        "Versioning of test catalogue files",
        f"{_path(test_name)} & {today} & {modification}",
    )
    return text


def find_pdflatex(miktex_bin: str | Path | None = None) -> str:
    """Resolve the pdflatex executable and return its path.

    ``--miktex-bin`` (or ``$MIKTEX_BIN``) names a directory holding the executable — a
    MiKTeX install on Windows, for instance. Without it, pdflatex is looked up on
    ``PATH``, which is what a TeX Live install on Linux or macOS provides. Nothing
    machine-specific is baked into the package.
    """
    if miktex_bin:
        directory = Path(miktex_bin)
        for name in ("pdflatex", "pdflatex.exe"):
            candidate = directory / name
            if candidate.is_file():
                return str(candidate)
        raise FileNotFoundError(f"no pdflatex in {directory}")
    binary = shutil.which("pdflatex") or shutil.which("pdflatex.exe")
    if binary is None:
        raise FileNotFoundError(
            "pdflatex is not on PATH; install TeX Live or MiKTeX, or name the directory "
            "holding it with --miktex-bin / $MIKTEX_BIN"
        )
    return binary


def compile_pdf(
    output_dir: Path, stem: str, miktex_bin: str | Path | None = None
) -> Path:
    """Run pdflatex twice from the build directory; raise if no PDF appears."""
    output_dir = Path(output_dir)
    pdflatex = find_pdflatex(miktex_bin)

    for _ in range(2):  # twice so the table of contents is stable
        completed = subprocess.run(
            [pdflatex, "-interaction=nonstopmode", "-halt-on-error", f"{stem}.tex"],
            cwd=output_dir, capture_output=True, text=True, check=False,
        )
        if completed.returncode != 0:
            log = output_dir / f"{stem}.log"
            tail = (
                log.read_text(errors="replace")[-4000:]
                if log.is_file()
                else completed.stdout[-4000:]
            )
            raise RuntimeError(f"pdflatex failed for {stem}.tex:\n{tail}")

    pdf = output_dir / f"{stem}.pdf"
    if not pdf.is_file():
        raise RuntimeError(f"pdflatex reported success but {pdf} is missing")
    return pdf
