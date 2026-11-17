"""Tests of the HTML companion of the curated / catalogue reports."""

import base64
import json

import pandas as pd

from soba_reference_repo import html_report

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
TEX = r"""
\section*{Version History}
\begin{table}\caption{Versioning of the documentation}\end{table}
\section{Dataset Content Description}
\subsection{Global Coverage Map}
\begin{figure}[H]
\includegraphics[width=\textwidth]{images_S1_curated_WV_ALTI/sar_coverage.png}
\caption{Global density of retained Sentinel-1 / ALTI matches.}
\end{figure}
\begin{figure}[H]
\includegraphics[width=\textwidth]{images_S1_curated_WV_ALTI/no_caption.png}
\end{figure}
\begin{table}\caption{Not a figure caption}\end{table}
\subsection{Catalogue Columns}
\section{Dataset Production Steps}
\subsection{Details of the applied filters}
\begin{figure}[H]\includegraphics{removed_no_rain.png}
\caption{Points removed by \textbf{no rain\_alti} (8 points, $\ge$ 4\%).}\end{figure}
"""


def _run(tmp_path, steps=True):
    run = tmp_path / "run"
    images = run / "report" / "images_S1_curated_WV_ALTI"
    images.mkdir(parents=True)
    for name in ("sar_coverage.png", "no_caption.png", "removed_no_rain.png"):
        (images / name).write_bytes(PNG)
    (run / "report" / "S1_curated_WV_ALTI.tex").write_text(TEX)
    (run / "recipe.toml").write_text('version = "5.0"\nproduction_date = "20261117"\n')
    parquet = run / "S1C_curated_WV_SV_CCI-SEATSTATE_ALTI_L2P-V5_5.0.parquet"
    pd.DataFrame({"sar_lat": [1.0], "imerg_rainrate": [0.0]}).to_parquet(parquet)
    step = {"id": "no_rain_alti", "name": "no rain",
            "clauses": [["alti_rain_contaminated", "eq", 0]],
            "before": 10, "removed": 2, "remaining": 8}
    manifest = {"reference": "altimeter", "input_rows": 10, "curated_rows": 8,
                "sources": [{"mission": "S1C", "path": "src.parquet", "curated_path": str(parquet),
                             "input_rows": 10, "curated_rows": 8,
                             "steps": [step] if steps else []}]}
    (run / "manifest.json").write_text(json.dumps(manifest))
    return run


def test_latex_to_text():
    text = html_report.latex_to_text(r"Points removed by \textbf{no rain\_alti} ($\ge$ 4\%)~ok")
    assert text == "Points removed by no rain_alti (≥ 4%) ok"


def test_parse_tex_keeps_figures_with_their_own_captions_and_non_empty_sections():
    items = html_report.parse_tex(TEX)
    kinds = [(item["kind"], item.get("title") or item.get("caption")) for item in items]
    assert ("section", "Version History") not in kinds          # no figure below it
    assert ("subsection", "Catalogue Columns") not in kinds
    figures = [item for item in items if item["kind"] == "figure"]
    assert [figure["caption"] for figure in figures] == [
        "Global density of retained Sentinel-1 / ALTI matches.",
        "",                                                       # not the next table's caption
        "Points removed by no rain_alti (8 points, ≥ 4%).",
    ]


def test_build_html_report_curated(tmp_path):
    out = html_report.build_html_report(_run(tmp_path))
    assert out.name == "S1_curated_WV_ALTI_5.0.html"
    page = out.read_text()
    assert page.count("data:image/png;base64,") == 3            # self-contained
    assert "Filter cascade" in page and "alti_rain_contaminated eq 0" in page
    assert "imerg_rainrate" in page                               # column table from the schema
    assert "8 rows (of 10 before curation)" in page


def test_build_html_report_catalogue_has_no_filter_section(tmp_path):
    page = html_report.build_html_report(_run(tmp_path, steps=False)).read_text()
    assert "Filter cascade" not in page and "Files" in page


def test_curate_dataset_html_flag_on_run_dir(tmp_path, monkeypatch):
    from soba_reference_repo import alti_report, curate_dataset

    run = _run(tmp_path)
    monkeypatch.setattr(alti_report, "build_alti_run_report",
                        lambda root, compile_report: {"tex": "x.tex", "pdf": None})
    assert curate_dataset.main(["--run-dir", str(run), "--no-compile", "--html"]) == 0
    assert (run / "report" / "S1_curated_WV_ALTI_5.0.html").is_file()
