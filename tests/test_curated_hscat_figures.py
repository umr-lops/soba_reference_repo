"""Curated figures describe both native reference variables and point density."""

import numpy as np
import pandas as pd
import pytest
from matplotlib.figure import Figure

from soba_reference_repo import hscat_report
from test_hscat_pipeline import hscat_source


@pytest.mark.parametrize("selected", ["windspeed", "winddirection"])
def test_curated_dual_distributions_and_density(tmp_path, monkeypatch, selected):
    source = hscat_source(tmp_path)
    frame = pd.read_parquet(source)
    frame["_curation_mission"] = "S1A"
    frame["ref_param_1"] = [10.0, 180.0, 350.0]
    frame["ref_param_2"] = [1.0, 9.0, 19.0]
    frame.to_parquet(source, index=False)
    panels = []
    density_totals = []
    original_plot = hscat_report.plot_mission_distribution
    original_save = Figure.savefig

    def capture_distribution(axis, counts, edges, colors):
        panels.append((axis, sum(int(values.sum()) for values in counts.values()), edges))
        return original_plot(axis, counts, edges, colors)

    def capture_save(figure, path, *args, **kwargs):
        if str(path).endswith("sar_coverage.png"):
            density_totals.append(float(figure.axes[0].collections[0].get_array().sum()))
        return original_save(figure, path, *args, **kwargs)

    monkeypatch.setattr(hscat_report, "plot_mission_distribution", capture_distribution)
    monkeypatch.setattr(Figure, "savefig", capture_save)
    figures = hscat_report.plot_hscat_figures(
        source, selected, tmp_path / "figures", batch_size=1, curated=True,
    )
    assert len(panels) == 2
    assert panels[0][0].get_position().y0 > panels[1][0].get_position().y0
    assert [panel[1] for panel in panels] == [3, 3]
    assert "speed" in panels[0][0].get_xlabel()
    assert "direction" in panels[1][0].get_xlabel()
    assert np.array_equal(panels[1][2], np.linspace(0, 360, 37))
    assert density_totals == [3.0]
    assert figures["mission_counts"] == {"S1A": 3}
    assert all(figures[key].is_file()
               for key in ("coverage", "monthly_rows", "reference_distribution"))
