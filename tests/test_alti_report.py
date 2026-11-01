"""Curated WV-altimeter report: figures, LaTeX and the curation entry point."""

import json
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from soba_reference_repo.alti_report import build_alti_run_report, plot_alti_figures

CCI = "CCI-SEATSTATE_ALTI_L2P-V5"
CMEMS = "CMEMS-ALTI-L3-VAVH"


def _alti_name(mission, product):
    return (f"{mission}_coaligned_catalogue_WV_20150319T134347_20251231T192542_"
            f"20261018T021205_SV_{product}_1.0.parquet")


def _fname_for(product):
    if product == CCI:
        return "ESACCI-SEASTATE-L2P-SWH-SARAL-20200101T000000-fv01.nc"
    return "global_vavh_l3_rt_al_20200101_20200102_20200103.nc"


def _curated_table(mission, product):
    """Tiny curated frame with every column the ALTI report reads."""
    times = pd.DatetimeIndex([f"2020-01-0{i} 12:00:00" for i in range(1, 5)]).tz_localize("UTC")
    return pa.table({
        "sar_time": pa.array(times),
        "sar_lon": pa.array([-120.0, -119.5, -119.0, -118.5]),
        "sar_lat": pa.array([30.0, 30.5, 31.0, 31.5]),
        "ref_hs_alti_closest": pa.array([1.0, 1.5, 2.0, 2.5]),
        "ww3_swh": pa.array([1.1, 1.4, 2.1, 2.4]),
        "imerg_rainrate": pa.array([0.0, 0.0, 0.0, 0.0]),
        "ref_lon": pa.array([-120.1, -119.6, -119.1, -118.6]),
        "ref_lat": pa.array([30.1, 30.6, 31.1, 31.6]),
        "fname": pa.array([_fname_for(product)] * 4),
        "_curation_mission": pa.array([mission] * 4),
        "_curation_row": pa.array([0, 1, 2, 3]),
    })


def _build_alti_run(tmp_path):
    """A minimal altimeter curated run: recipe + 2 S1A sources (CCI + CMEMS) + manifest."""
    run = tmp_path / "run"
    (run / "curated/S1A").mkdir(parents=True)
    step = {"id": "no_rain", "name": "IMERG rain rate == 0", "status": "applied",
            "remaining": 4, "removed": 0, "clauses": [["imerg_rainrate", "eq", 0]]}
    sources = []
    blocks = ['reference = "altimeter"', f'output_dir = "{run.name}"',
              'production_date = "20261101"', 'version = "2.0"', 'compile = false', '']
    for product, label in ((CCI, "CCI"), (CMEMS, "CMEMS")):
        src = tmp_path / _alti_name("S1A", product)
        pq.write_table(_curated_table("S1A", product), src)
        curated = run / "curated" / "S1A" / (
            f"S1A_curated_WV_20150319_20251231_20261101_SV_{product}_2.0.parquet")
        pq.write_table(_curated_table("S1A", product), curated)
        blocks += ['[[catalogues]]', 'mission = "S1A"', f'path = "{src.name}"',
                   '[[catalogues.rules]]', 'id = "no_rain"', 'name = "IMERG rain rate == 0"',
                   'clauses = [["imerg_rainrate", "eq", 0]]', '']
        sources.append({"mission": "S1A", "path": str(src), "curated_path": str(curated),
                        "input_rows": 4, "curated_rows": 4, "steps": [step]})
    (run / "recipe.toml").write_text("\n".join(blocks), encoding="utf-8")
    manifest = {"status": "complete", "reference": "altimeter", "mode": "WV",
                "sources": sources, "input_rows": 8, "curated_rows": 8}
    (run / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return run


def test_source_label_disambiguates_duplicate_mission():
    from soba_reference_repo.curated_report import _source_label
    cci = _source_label({"mission": "S1A",
                         "curated_path": "S1A_curated_WV_20150319_20251231_20261101_"
                                         "SV_CCI-SEATSTATE_ALTI_L2P-V5_2.0.parquet"})
    cmems = _source_label({"mission": "S1A",
                           "curated_path": "S1A_curated_WV_20191001_20260629_20261101_"
                                          "SV_CMEMS-ALTI-L3-VAVH_2.0.parquet"})
    assert cci == "S1A CCI-SeaState"
    assert cmems == "S1A CMEMS-VAVH"
    assert cci != cmems


def test_plot_alti_figures_writes_three_pngs(tmp_path):
    curated = pd.DataFrame({
        "sar_lon": [-120.0, -119.0, -118.0], "sar_lat": [30.0, 31.0, 32.0],
        "sar_time": pd.to_datetime(["2020-01-01 12:00:00"] * 3),
        "_curation_mission": ["S1A", "S1A", "S1B"],
        "ref_hs_alti_closest": [1.0, 2.0, 3.0],
    })
    paths = plot_alti_figures(curated, tmp_path / "figures")
    assert set(paths) == {"coverage", "monthly_rows", "ref_distribution"}
    assert all(p.is_file() and p.suffix == ".png" for p in paths.values())


def test_build_alti_run_report_two_sources_one_document(tmp_path):
    run = _build_alti_run(tmp_path)
    result = build_alti_run_report(run, compile_report=False)
    tex_path = Path(result["tex"])
    assert tex_path.name == "S1_curated_WV_ALTI.tex"
    tex = tex_path.read_text(encoding="utf-8")
    assert "Altimeter Significant Wave-Height Distribution" in tex
    assert "altimeter significant wave heights" in tex
    # Both S1A catalogues must appear as distinct, labelled columns (not collapsed).
    assert "S1A CCI-SeaState" in tex
    assert "S1A CMEMS-VAVH" in tex
    assert "Filter & S1A CCI-SeaState & S1A CMEMS-VAVH" in tex
    # Distribution is described from the altimeter Hs variable.
    assert "Colored lines show significant wave height (m)" in tex
    # Review additions: per-database source versions, split figures, initial-vs-final.
    assert "Source catalogue versions" in tex
    assert "L2P-V5 (source catalogue)" in tex
    assert "CMEMS-ALTI-L3-VAVH (source catalogue)" in tex
    assert "1.0 (source) / 2.0 (curated)" in tex
    assert "cov_CCI_S1A.png" in tex and "cov_CMEMS_S1A.png" in tex
    assert "monthly_CCI_S1A.png" in tex
    assert "alti_swh_initial_vs_final.png" in tex
    # Table 6 now reports remaining / removed / % removed (percent sign escaped).
    assert "/ 0 (0.0\\%)" in tex
    assert result["pdf"] is None
    manifest = json.loads((run / "manifest.json").read_text())
    assert manifest["report_tex"] == str(tex_path)
    assert "figures" in manifest
    # The split + initial-vs-final figures are written to the assets bundle.
    assets = run / "report" / "images_S1_curated_WV_ALTI"
    assert (assets / "cov_CCI_S1A.png").is_file()
    assert (assets / "alti_swh_initial_vs_final.png").is_file()
