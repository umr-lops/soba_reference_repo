"""Documentation (PDF) of the co-aligned catalogues built by ``soba_build_coalign``.

Reads the catalogue Parquets of a folder and writes one document: the shared SOBA cover and
version tables, the coverage map, the monthly counts, the distribution of every reference
parameter, the time offset between the SAR and the reference, the ancillary values, one column
table per reference product with a completeness table, and the global attributes of the files.

It sits next to ``catalogue_report`` (the report of the older altimeter catalogues), reuses the
shared LaTeX style, asset staging and PDF compilation, and only reads the catalogues: the build
(``coalign``) does not depend on it.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import pyarrow as pa  # noqa: E402
import pyarrow.parquet as pq  # noqa: E402
from matplotlib import pyplot as plt  # noqa: E402
from matplotlib.patches import Polygon  # noqa: E402

from .curated_report import render_column_table  # noqa: E402
from .hscat_report import _tex  # noqa: E402
from .pdf_support import (  # noqa: E402
    ASSET_DIR, _path, compile_pdf, purge_latex_byproducts, stage_latex_assets,
)
from .plotting import MISSION_COLORS, finish_density_map, plot_mission_distribution  # noqa: E402

NAME = re.compile(
    r"(?P<sat>S1[A-D])_coaligned_catalogue_(?P<mode>WV)_(?P<start>\d{8})_(?P<stop>\d{8})_"
    r"(?P<production>\d{8})_(?P<pol>SV|DV|SH|DH)_(?P<product>[A-Za-z0-9][A-Za-z0-9._-]*)_"
    r"(?P<version>\d+\.\d+)\.parquet$")
POLARIZATION = {"SV": "single VV (SV)", "SH": "single HH (SH)", "DV": "dual VV+VH (DV)",
                "DH": "dual HH+HV (DH)"}

# Reference parameter column -> (axis label, unit, kind of reference)
PARAMETERS = {
    "windspeed_scat": ("Scatterometer wind speed", "m/s", "HSCAT"),
    "winddirection_scat": ("Scatterometer wind direction (towards)", "degrees", "HSCAT"),
    "swh_alti": ("Altimeter significant wave height", "m", "ALTI"),
    "swh_swot": ("SWOT KaRIn significant wave height", "m", "SWOT"),
}
KIND_TEXT = {
    "HSCAT": "HY-2 HSCAT 25 km ocean surface winds, processed by KNMI for EUMETSAT OSI SAF",
    "ALTI": "altimeter significant wave height (CMEMS wave L3 altimetry, CCI SeaState V5)",
    "SWOT": "SWOT KaRIn Level-2 WindWave (PGD0/PID0), distributed by NASA/JPL PO.DAAC",
}
SCOPE_KIND = {"HSCAT": "scatterometer winds", "ALTI": "altimeter significant wave heights",
              "SWOT": "SWOT significant wave heights"}

DESCRIPTIONS = {
    "primary_key": ("SLC SAFE name, imagette number, ref_lon and ref_lat (four decimals); unique "
                    "in the file."),
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
SAR_PRODUCT = ("Copernicus/ESA Sentinel-1 Level-2 Ocean (OCN), Wave Mode, "
               "with its Level-1 SLC product")


def _kind(columns) -> str:
    for column, (_, _, kind) in PARAMETERS.items():
        if column in columns:
            return kind
    return "OTHER"


def _date(text: str) -> str:
    return f"{text[:4]}-{text[4:6]}-{text[6:8]}"


def read_catalogues(folder) -> list:
    """One entry per catalogue file of ``folder``: name parts, attributes, schema and row count."""
    entries = []
    for path in sorted(Path(folder).glob("S1*_coaligned_catalogue_WV_*.parquet")):
        match = NAME.match(path.name)
        if match is None:
            raise ValueError(f"not a coalign catalogue name: {path.name}")
        parquet = pq.ParquetFile(path)
        attrs = {k.decode(): v.decode("utf-8", errors="replace")
                 for k, v in (parquet.schema_arrow.metadata or {}).items()}
        schema = parquet.schema_arrow
        entries.append({**match.groupdict(), "path": path, "attrs": attrs, "schema": schema,
                        "rows": parquet.metadata.num_rows, "kind": _kind(schema.names)})
    if not entries:
        raise FileNotFoundError(f"no coalign catalogue in {folder}")
    return entries


def read_frame(entries) -> pd.DataFrame:
    """The report columns of every file, with the satellite, reference and kind of each row."""
    wanted = ["sar_lon", "sar_lat", "sar_time", "ref_source", *PARAMETERS]
    frames = []
    for entry in entries:
        columns = [c for c in wanted if c in entry["schema"].names]
        frame = pq.read_table(entry["path"], columns=columns).to_pandas()
        frame["mission"] = frame.pop("ref_source").astype("category")
        frame["satellite"], frame["product"], frame["kind"] = (
            entry["sat"], entry["product"], entry["kind"])
        frames.append(frame)
    return pd.concat(frames, ignore_index=True)


# ---------------------------------------------------------------- figures

_LAND = None


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


# The groupings of the maps and of the monthly counts: (title, frame column).
GROUPS = (("Sentinel-1 satellite", "satellite"), ("reference mission", "mission"),
          ("reference product", "product"))
PANELS_PER_FIGURE = 3


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


def coverage_blocks(frame, assets, figures) -> str:
    """Maps per Sentinel-1 satellite, reference mission and reference product (LaTeX)."""
    blocks = []
    for title, column in GROUPS:
        values = group_values(frame, column)
        if len(values) < 2:
            blocks.append(rf"\noindent\textbf{{Coverage by {title}.}} All rows have the same "
                          rf"{title}: {_tex(values[0])}, shown in the global map above.")
            continue
        blocks.append(rf"\noindent\textbf{{Coverage by {title}.}}")
        for number, start in enumerate(range(0, len(values), PANELS_PER_FIGURE), 1):
            chunk = values[start:start + PANELS_PER_FIGURE]
            name = f"coverage_{column}_{number}.png"
            figure_coverage_panels(frame, column, chunk, figures / name)
            blocks.append(_figure(f"{assets}/{name}", f"Coverage per {title}: "
                                  + ", ".join(_tex(v) for v in chunk) + ".",
                                  f"fig:coverage_{column}_{number}", width="0.88"))
    return "\n\n".join(blocks)


# Both monthly figures share one canvas width and one axes box, so they line up in the document.
MONTHLY_WIDTH, MONTHLY_LEFT, MONTHLY_PLOT = 10.0, 1.0, 6.6      # inches
BLUES = {"S1A": "#c6dbef", "S1B": "#6baed6", "S1C": "#2171b5", "S1D": "#08306b"}


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


def monthly_blocks(frame, assets, figures) -> str:
    """Monthly counts by reference mission and reference product (LaTeX)."""
    blocks = []
    for title, column in GROUPS[1:]:
        values = group_values(frame, column)
        if len(values) < 2:
            blocks.append(rf"\noindent\textbf{{Matchups by {title}.}} All rows have the same "
                          rf"{title}: {_tex(values[0])}.")
            continue
        name = f"monthly_{column}.png"
        figure_monthly_stacked(frame, column, title, figures / name)
        blocks.append(_figure(f"{assets}/{name}", f"Monthly count of matches, stacked by {title}.",
                              f"fig:monthly_{column}", width="0.97"))
    return "\n\n".join(blocks)


def figure_distribution(frame, column, path):
    label, unit, _ = PARAMETERS[column]
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


# ---------------------------------------------------------------- LaTeX pieces

def _rows(rows):
    return "\n".join(" & ".join(row) + r" \\" for row in rows)


def _figure(name, caption, label, width="0.9"):
    return "\n".join([
        r"\begin{figure}[H]\centering",
        rf"\includegraphics[width={width}\textwidth]{{{name}}}",
        rf"\caption{{{caption}}}", rf"\label{{{label}}}", r"\end{figure}"])


def _table(caption, label, spec, header, rows):
    return "\n".join([
        r"\begin{table}[H]\centering", rf"\caption{{{caption}}}", rf"\label{{{label}}}",
        r"{\small", rf"\begin{{tabular}}{{{spec}}}", r"\toprule",
        " & ".join(rf"\textbf{{{h}}}" for h in header) + r" \\", r"\midrule", _rows(rows),
        r"\bottomrule", r"\end{tabular}", "}", r"\end{table}"])


def reference_stats(frame, column) -> list:
    values = pd.to_numeric(frame[column], errors="coerce").dropna()
    unit = PARAMETERS[column][1]
    if values.empty:
        return [_tex(column), "--", "--", "--", "--", "0"]
    return [_tex(column) + f" ({_tex(unit)})", f"{values.min():.4g}", f"{values.max():.4g}",
            f"{values.mean():.4g}", f"{values.median():.4g}", f"{len(values):,}"]


def reference_sections(frame, assets, figures) -> str:
    blocks = []
    for column, (label, unit, kind) in PARAMETERS.items():
        if column not in frame or not frame[column].notna().any():
            continue
        name = f"distribution_{column}.png"
        figure_distribution(frame, column, figures / name)
        blocks.append("\n".join([
            r"\Needspace{0.43\textheight}", rf"\subsection{{{_tex(label)} Distribution}}",
            r"\textbf{Description:} Colored lines show the parameter by satellite. Each satellite "
            r"is normalized to its own finite sample count, shown in the legend; percentages do "
            r"not compare absolute counts.",
            _figure(f"{assets}/{name}", rf"Distribution of {_tex(column)}.",
                    f"fig:dist_{column}"),
            r"Table~\ref{tab:stats_" + column + r"} gives the summary.",
            _table(rf"Summary statistics of {_tex(column)}", f"tab:stats_{column}", "lrrrrr",
                   ["Parameter", "Minimum", "Maximum", "Mean", "Median", "Finite rows"],
                   [reference_stats(frame, column)]),
        ]))
    return "\n\n".join(blocks)


def column_tables(entries) -> str:
    by_product = defaultdict(list)
    for entry in entries:
        by_product[entry["product"]].append(entry)
    reference_names = set(PARAMETERS)
    blocks = []
    for index, (product, group) in enumerate(sorted(by_product.items())):
        fields = {}
        for entry in group:
            for field in entry["schema"]:
                fields.setdefault(field.name, field)
        schema = pa.schema(list(fields.values()))
        satellites = "/".join(sorted({e["sat"] for e in group}))
        blocks.append(render_column_table(
            schema, DESCRIPTIONS, caption=f"{_tex(product)} catalogue columns ({satellites})",
            label=f"tab:cols_{index}", reference_names=reference_names,
            name_width="5.0cm", description_width="7.2cm"))
    return "\n\n".join(blocks)


def completeness_table(entries) -> str:
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
    rows = [[_tex(name), *(f"{shares[p][name]:.1%}".replace("%", r"\%") if name in shares[p]
                           else "--" for p in products)] for name in names]
    spec = r"@{}>{\raggedright\arraybackslash}p{5cm}" + "r" * len(products) + "@{}"
    return _table("Share of empty values per column and reference product",
                  "tab:completeness", spec, ["Column", *[_tex(p) for p in products]], rows)


def metadata_rows(entries) -> list:
    keys = []
    for entry in entries:
        for key in entry["attrs"]:
            if key not in keys:
                keys.append(key)
    skip = {"rows", "start_date", "end_date", "satellite", "polarization", "ref_product"}
    rows = []
    for key in keys:
        if key in skip:
            continue
        by_value = defaultdict(list)
        for entry in entries:
            if key in entry["attrs"]:
                by_value[entry["attrs"][key]].append(
                    f"{entry['sat']} {entry['start']}-{entry['stop']}")
        text = "; ".join((f"{', '.join(labels)}: " if len(by_value) > 1 else "") + value
                         for value, labels in by_value.items())
        rows.append((_tex(key), _path(text) if "/" in text and " " not in text else _tex(text)))
    return rows


def build_report(catalogue_dir, output_dir, *, compile_report=True, miktex_bin=None) -> list:
    """One document per reference kind (SWOT, HSCAT, ALTI) found in ``catalogue_dir``.

    Each is written as ``<output_dir>/S1_coalign_catalogue_WV_<KIND>_<version>.{tex,pdf}``.
    Returns one result dict per document.
    """
    entries = read_catalogues(catalogue_dir)
    results = []
    for kind in ("SWOT", "HSCAT", "ALTI"):
        group = [e for e in entries if e["kind"] == kind]
        if group:
            results.append(build_document(group, kind, output_dir, compile_report=compile_report,
                                          miktex_bin=miktex_bin))
    unknown = [e["path"].name for e in entries if e["kind"] not in ("SWOT", "HSCAT", "ALTI")]
    if unknown:
        raise ValueError(f"catalogues of an unknown reference kind: {unknown}")
    return results


def build_document(entries, kind, output_dir, *, compile_report=True, miktex_bin=None) -> dict:
    """The document of the catalogues of one reference kind."""
    versions = sorted({e["version"] for e in entries})
    if len(versions) != 1:
        raise ValueError(f"{kind} catalogues of several versions in one report: {versions}")
    version = versions[0]
    stem = f"S1_coalign_catalogue_WV_{kind}"
    output_dir = Path(output_dir)
    assets = f"images_{stem}"
    figures = output_dir / assets
    figures.mkdir(parents=True, exist_ok=True)
    stage_latex_assets(ASSET_DIR / "latex", figures)
    (figures / "sobadataflow.png").write_bytes((ASSET_DIR / "latex/sobadataflow.png").read_bytes())

    frame = read_frame(entries)
    kinds = [kind]
    figure_coverage(frame, figures / "coverage.png")
    figure_monthly(frame, figures / "monthly.png")
    coverage_extra = coverage_blocks(frame, assets, figures)
    monthly_extra = monthly_blocks(frame, assets, figures)
    production_dates = sorted({e["production"] for e in entries})
    polarizations = ", ".join(sorted({POLARIZATION[e["pol"]] for e in entries}))
    file_rows = _rows([
        (_path(e["path"].name), _date(e["production"]),
         f"Contains {e['rows']:,} matches"
         + (f", {int(e['attrs']['rows_dropped_duplicate_key'])} repeated key(s) dropped"
            if "rows_dropped_duplicate_key" in e["attrs"] else "") + ".")
        for e in entries])
    mapping = {
        "STYLE_FILE": f"{assets}/soba", "GRAPHICS_DIRECTORY": assets,
        "DATAFLOW_FIGURE": f"{assets}/sobadataflow.png",
        "DATASET_NAME": _tex(" + ".join(kinds)),
        "SCOPE_TEXT": _tex(
            "A co-aligned catalogue of Sentinel-1 Wave Mode observations matched to "
            + ", ".join(SCOPE_KIND[k] for k in kinds) + ", with ancillary values, before "
            "quality curation."),
        "GENERAL_DESCRIPTION": _tex(
            "Copernicus/ESA Sentinel-1 Level-2 Ocean (OCN), Wave Mode observations are matched "
            "to the reference products below; the matched points are merged into one row per "
            "reference point, with the ancillary values added."),
        "DOC_RELEASE_DATE": _date(production_dates[-1]),
        "FILE_VERSION_ROW": file_rows,
        "COVERAGE_FIGURE": f"{assets}/coverage.png", "MONTHLY_FIGURE": f"{assets}/monthly.png",
        "COVERAGE_EXTRA": coverage_extra, "MONTHLY_EXTRA": monthly_extra,
        "REFERENCE_SECTIONS": reference_sections(frame, assets, figures),
        "COLUMN_TABLES": column_tables(entries),
        "COMPLETENESS_TABLE": completeness_table(entries),
        "METADATA_ROWS": _rows([
            ("SAR product / provider", _tex(SAR_PRODUCT)),
            ("Reference product / provider", _tex("; ".join(KIND_TEXT[k] for k in kinds))),
            ("Dataset version", _tex(version)),
            ("Dataset production date(s)", _tex(", ".join(production_dates))),
            *metadata_rows(entries)]),
        "SAR_PRODUCT": _tex(SAR_PRODUCT),
        "REFERENCE_PRODUCT": _tex("; ".join(KIND_TEXT[k] for k in kinds)),
        "POLARIZATIONS": _tex(polarizations) + ".",
        "PRODUCT_REFERENCES": (
            r"\bibitem{soba_format} SOBA Consortium. Parquet format specification. "
            r"\url{https://soba.ifremer.fr/parquet_format_specification.html}"),
    }
    tex = (ASSET_DIR / "latex" / "coalign_catalogue_template.tex").read_text(encoding="utf-8")
    for key, value in mapping.items():
        tex = tex.replace(f"@@{key}@@", value)
    left = sorted(set(re.findall(r"@@[A-Z_]+@@", tex)))
    if left:
        raise ValueError(f"template fields without a value: {left}")
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


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="soba_coalign_report",
        description="Documentation PDF of the catalogues written by soba_build_coalign.")
    parser.add_argument("--catalogues", type=Path, required=True,
                        help="folder with the catalogue Parquets")
    parser.add_argument("--output", type=Path, required=True,
                        help="folder for the .tex, the figures and the PDF")
    parser.add_argument("--no-compile", action="store_true", help="skip the PDF step")
    parser.add_argument("--miktex-bin", type=Path, default=None)
    args = parser.parse_args(argv)
    for result in build_report(args.catalogues, args.output, compile_report=not args.no_compile,
                               miktex_bin=args.miktex_bin):
        print(f"{result['kind']}: {result['tex']} ({result['files']} files, "
              f"{result['rows']:,} rows)")
        if result["pdf"]:
            print(f"  PDF: {result['pdf']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
