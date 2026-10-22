"""Curated HSCAT coverage by Sentinel-1 and HY-2 platform."""

import json
from collections import Counter

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
from matplotlib import colors, pyplot as plt, ticker
from matplotlib.patches import Polygon

from .pdf_support import ASSET_DIR


def _platforms(frame):
    letters = frame['ref_id'].astype('string').str.lower().str.extract(
        r'_hy_2([a-z])_', expand=False,
    )
    return letters.str.upper().radd('HY-2').fillna('Unknown HY-2')


def plot_platform_coverage(source, output_path, batch_size=65_536):
    """Count every row; map finite valid SAR coordinates without deduplication."""
    parquet = pq.ParquetFile(source)
    columns = ['_curation_mission', 'ref_id', 'sar_time', 'sar_lon', 'sar_lat']
    counts, periods = Counter(), {}
    for batch in parquet.iter_batches(batch_size=batch_size, columns=columns):
        frame = batch.to_pandas()
        frame['hy2'] = _platforms(frame)
        for key, group in frame.groupby(['_curation_mission', 'hy2']):
            counts[key] += len(group)
            dates = pd.to_datetime(group['sar_time'], utc=True).dropna()
            if len(dates):
                first, last = periods.get(key, (dates.min(), dates.max()))
                periods[key] = min(first, dates.min()), max(last, dates.max())
    missions = sorted({mission for mission, _ in counts})
    platforms = sorted({platform for _, platform in counts})
    if not missions:
        raise ValueError('No Curated rows for platform coverage')
    figure, axes = plt.subplots(len(missions), len(platforms), squeeze=False,
                               figsize=(8.1 * len(platforms), 2.7 * len(missions) + 3))
    figure.subplots_adjust(left=.055, right=.91, top=.865, bottom=.12,
                           hspace=.4, wspace=.13)
    land = json.loads((ASSET_DIR/'maps/ne_110m_land.geojson').read_text())
    panels, mapped = {}, Counter()
    for row, mission in enumerate(missions):
        for col, platform in enumerate(platforms):
            axis = axes[row, col]
            axis.set_facecolor('#F5F9FB')
            for feature in land['features']:
                geometry = feature['geometry']
                polygons = ([geometry['coordinates']] if geometry['type'] == 'Polygon'
                            else geometry['coordinates'])
                for polygon in polygons:
                    axis.add_patch(Polygon(polygon[0], facecolor='#DADADA',
                                           edgecolor='#A5A5A5', linewidth=.22, zorder=1))
            cells = axis.hexbin([], [], gridsize=360, extent=(-180, 180, -90, 90),
                                mincnt=0, linewidths=0, cmap='viridis', zorder=2, rasterized=True)
            panels[mission, platform] = axis, cells
    for batch in parquet.iter_batches(batch_size=batch_size, columns=columns):
        frame = batch.to_pandas()
        frame['hy2'] = _platforms(frame)
        for key, group in frame.groupby(['_curation_mission', 'hy2']):
            axis, cells = panels[key]
            lon = pd.to_numeric(group['sar_lon'], errors='coerce').to_numpy(dtype=float)
            lat = pd.to_numeric(group['sar_lat'], errors='coerce').to_numpy(dtype=float)
            valid = (np.isfinite(lon) & np.isfinite(lat) & (abs(lon) <= 180)
                     & (abs(lat) <= 90))
            if not valid.any():
                continue
            addition = axis.hexbin(lon[valid], lat[valid], gridsize=360,
                                   extent=(-180, 180, -90, 90), mincnt=0, linewidths=0)
            cells.set_array(cells.get_array() + addition.get_array())
            addition.remove()
            mapped[key] += int(valid.sum())
            axis.scatter(lon[valid], lat[valid], s=.13, c='#102C45', alpha=.09,
                         linewidths=0, zorder=3, rasterized=True)
    maximum = max(2, max(int(cells.get_array().max()) for _, cells in panels.values()))
    norm = colors.LogNorm(vmin=1, vmax=maximum)
    for (mission, platform), (axis, cells) in panels.items():
        values = cells.get_array()
        if int(values.sum()) != mapped[mission, platform]:
            raise ValueError(f'Coverage histogram lost rows for {mission}/{platform}')
        cells.set_array(np.ma.masked_where(values == 0, values))
        cells.set_norm(norm)
        total = counts[mission, platform]
        dates = periods.get((mission, platform))
        period = (f'{dates[0]:%Y-%m-%d} to {dates[1]:%Y-%m-%d}' if dates
                  else 'No coverage in these files')
        axis.set_title(f'{mission} × {platform} | {total:,} rows\n{period}',
                       fontsize=10.7, loc='left', pad=8, color='#153348')
        if not total:
            axis.text(.5, .5, 'No Curated matchups', transform=axis.transAxes,
                      ha='center', va='center', color='#626F7B')
        elif not mapped[mission, platform]:
            axis.text(.5, .5, 'No valid SAR coordinates', transform=axis.transAxes,
                      ha='center', va='center', color='#626F7B')
        axis.set(xlim=(-180, 180), ylim=(-90, 90), aspect='equal',
                 xticks=[-180, -120, -60, 0, 60, 120, 180], yticks=[-60, 0, 60])
        axis.tick_params(labelsize=8, length=2)
        axis.grid(alpha=.2, linewidth=.4)
    figure.text(.055, .969, 'S1 / HY-2 platform coverage', fontsize=23,
                weight='bold', color='#153348')
    figure.text(.055, .937, f'Curated HSCAT catalogues · {sum(mapped.values()):,} mapped rows',
                fontsize=12, color='#425C6A')
    figure.text(.055, .908, 'Rows: Sentinel-1 platform   Columns: HY-2 platform'
                '   Location: SAR WV imagette centre', fontsize=10.5, color='#425C6A')
    cax = figure.add_axes([.935, .3, .012, .4])
    bar = figure.colorbar(plt.cm.ScalarMappable(norm=norm, cmap='viridis'), cax=cax)
    bar.ax.yaxis.set_major_locator(ticker.LogLocator(base=10, subs=(1, 2, 5)))
    bar.ax.yaxis.set_major_formatter(ticker.FuncFormatter(lambda value, _: f'{value:g}'))
    bar.set_label('Matchup rows per 1°-wide hexagonal cell (log scale)', fontsize=10)
    figure.text(.055, .075, 'Same count scale in every panel; grey land and small point'
                ' overlays show location.', fontsize=10, color='#425C6A')
    figure.text(.055, .052, 'Coverage periods differ. Counts are Curated rows, not unique'
                ' imagettes, equal-area densities or observation rates.',
                fontsize=10, color='#425C6A')
    omitted = sum(counts.values()) - sum(mapped.values())
    figure.text(.055, .029, 'No additional filters or deduplication.'
                f' Invalid-coordinate rows omitted from maps: {omitted:,}.',
                fontsize=10, color='#425C6A')
    figure.savefig(output_path, dpi=180, facecolor='white')
    figure.savefig(output_path.with_suffix('.svg'), facecolor='white')
    plt.close(figure)
