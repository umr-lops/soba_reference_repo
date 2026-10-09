import matplotlib.pyplot as plt
import numpy as np

from soba_reference_repo.plotting import plot_mission_distribution


def test_mission_distribution_overlays_normalized_histograms_with_headroom():
    figure, axis = plt.subplots(figsize=(9, 4.5))
    counts = {
        "S1A": np.array([3, 1]),
        "S1C": np.array([1, 3]),
    }
    colors = {"S1A": "#0072B2", "S1C": "#009E73"}
    try:
        plot_mission_distribution(axis, counts, np.array([0, 1, 2]), colors)

        labels = [text.get_text() for text in axis.get_legend().get_texts()]
        assert labels == ["S1A (n=4)", "S1C (n=4)"]
        steps = list(axis.patches)
        assert len(steps) == 2
        assert all(not step.get_fill() for step in steps)
        maxima = [step.get_path().vertices[:, 1].max() for step in steps]
        assert maxima == [75, 75]
        assert axis.get_ylabel() == "Mission TEST rows (%)"
        assert axis.get_ylim()[1] > 75
        anchor = axis.get_legend().get_bbox_to_anchor()._bbox
        assert anchor.x0 == 0.5
        assert anchor.y0 < 0
    finally:
        plt.close(figure)
