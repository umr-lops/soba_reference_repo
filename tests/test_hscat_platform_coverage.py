"""S1/HY-2 coverage retains row counts and shares one logarithmic scale."""

import numpy as np
import pandas as pd
from matplotlib.figure import Figure

from soba_reference_repo.hscat_report import plot_hscat_figures
from test_hscat_pipeline import hscat_source


def test_platform_coverage_replaces_first_map_and_keeps_global(tmp_path, monkeypatch):
    source = hscat_source(tmp_path)
    frame = pd.read_parquet(source)
    frame['_curation_mission'] = ['S1A', 'S1A', 'S1C']
    frame['ref_id'] = ['file_hy_2b_wind.nc', 'file_hy_2c_wind.nc', 'unknown']
    frame['sar_lon'] = [-30.0, 20.0, np.nan]
    frame.to_parquet(source, index=False)
    captured = []
    original = Figure.savefig

    def capture(figure, path, *args, **kwargs):
        if str(path).endswith('sar_coverage.png'):
            axes = figure.axes[:-1]
            captured.extend(axes)
            norms = [axis.collections[0].norm for axis in axes]
            assert all(norm is norms[0] for norm in norms)
            assert sum(float(axis.collections[0].get_array().filled(0).sum()) for axis in axes) == 2
        return original(figure, path, *args, **kwargs)

    monkeypatch.setattr(Figure, 'savefig', capture)
    figures = plot_hscat_figures(source, 'windspeed', tmp_path/'plots', batch_size=1,
                                 curated=True)
    assert len(captured) == 6  # Two SAR missions × two identified HY-2s + unknown.
    assert any('Unknown HY-2' in axis.get_title(loc='left') for axis in captured)
    assert any('No Curated matchups' in text.get_text()
               for axis in captured for text in axis.texts)
    assert figures['coverage_legacy'].name == 'sar_coverage_global.png'
    assert figures['coverage_legacy'].is_file()
    assert figures['coverage'].is_file()
