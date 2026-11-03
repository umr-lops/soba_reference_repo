"""Co-aligned catalogue report: figures, LaTeX and the catalogue entry point."""

import json
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from soba_reference_repo.catalogue_report import build_catalogue_report

CCI = "CCI-SEATSTATE_ALTI_L2P-V5"
CMEMS = "CMEMS-ALTI-L3-VAVH"


def _catalogue_name(mission, product):
    # Same naming as the real raw catalogues (one file per SAR unit / database).
    return (f"{mission}_coaligned_catalogue_WV_20150319T134347_20251231T192542_"
            f"20261018T021205_SV_{product}_1.0.parquet")


def _fname_for(product):
    if product == CCI:
        return "ESACCI-SEASTATE-L2P-SWH-SARAL-20200101T000000-fv01.nc"
    return "global_vavh_l3_rt_al_20200101_20200102_20200103.nc"


SAFE = "S1A_WV_SLC__1SSV_20200101T000000_000000_000000_0000.SAFE"


def _catalogue_table(product):
    """Tiny raw catalogue frame (pre-curation): no ``_curation_*`` columns."""
    times = pd.DatetimeIndex([f"2020-01-0{i} 12:00:00" for i in range(1, 5)]).tz_localize("UTC")
    return pa.table({
        "sar_time": pa.array(times),
        "sar_lon": pa.array([-120.0, -119.5, -119.0, -118.5]),
        "sar_lat": pa.array([30.0, 30.5, 31.0, 31.5]),
        "sar_incidence_angle": pa.array([30.0, 31.0, 32.0, 33.0]),
        "oswTotalHs": pa.array([1.1, 1.6, 2.1, 2.6]),
        "ref_hs_alti_closest": pa.array([1.0, 1.5, 2.0, 2.5]),
        "ref_time": pa.array(times),
        "ref_lon": pa.array([-120.1, -119.6, -119.1, -118.6]),
        "ref_lat": pa.array([30.1, 30.6, 31.1, 31.6]),
        "fname": pa.array([_fname_for(product)] * 4),
        "imerg_rainrate": pa.array([0.0, 0.0, 0.0, 0.0]),
        "ww3_swh": pa.array([1.1, 1.4, 2.1, 2.4]),
        "sar_safe_slc": pa.array([SAFE] * 4),
        "primary_key": pa.array([f"key-{i}" for i in range(4)]),
    })


def _build_catalogue_run(tmp_path):
    """A minimal catalogue run: recipe.toml + 2 S1A catalogues (CCI + CMEMS)."""
    run = tmp_path / "run"
    run.mkdir(parents=True)
    blocks = ['reference = "altimeter"', f'output_dir = "{run.name}"',
              'production_date = "20261101"', 'version = "1.0"', 'compile = false', '']
    paths = []
    for product in (CCI, CMEMS):
        src = run / _catalogue_name("S1A", product)
        pq.write_table(_catalogue_table(product), src)
        paths.append(str(src))
        blocks += ['[[catalogues]]', 'mission = "S1A"', f'path = "{src.name}"', '']
    (run / "recipe.toml").write_text("\n".join(blocks), encoding="utf-8")
    return run, paths


def _catalogues(paths):
    return [{"mission": "S1A", "path": p} for p in paths]


def test_read_catalogue_tags_mission_and_database(tmp_path):
    from soba_reference_repo.catalogue_report import _read_catalogue
    run, paths = _build_catalogue_run(tmp_path)
    frame = _read_catalogue(_catalogues(paths))
    assert "_curation_mission" in frame.columns
    assert "_source_db" in frame.columns
    assert set(frame["_source_db"]) == {"CCI", "CMEMS"}
    assert (frame["_curation_mission"] == "S1A").all()
    assert len(frame) == 8  # 4 rows x 2 catalogues


def test_build_catalogue_report_two_sources_one_document(tmp_path):
    run, paths = _build_catalogue_run(tmp_path)
    result = build_catalogue_report(_catalogues(paths), version="1.0",
                                    production_date="20261101", output_dir=run,
                                    compile_report=False)
    tex_path = Path(result["tex"])
    assert tex_path.name == "S1_catalogue_WV_ALTI.tex"
    tex = tex_path.read_text(encoding="utf-8")
    # Catalogue wording (not curated wording).
    assert "Catalogue Co-aligned Dataset Description - Ifremer" in tex
    assert "Curated Dataset Description - Ifremer" not in tex
    assert "co-aligned catalogue of Sentinel-1 Wave Mode observations" in tex
    # Distribution is the altimeter Hs (as requested), described by the shared engine.
    assert "Altimeter Significant Wave-Height Distribution" in tex
    # Both S1A databases appear as distinct, labelled columns (not collapsed).
    assert "S1A CCI-SeaState" in tex and "S1A CMEMS-VAVH" in tex
    # Per-database split figures + source versions (review additions reused).
    assert "cov_CCI_S1A.png" in tex and "cov_CMEMS_S1A.png" in tex
    assert "monthly_CCI_S1A.png" in tex
    assert "L2P-V5 (source catalogue)" in tex and "CMEMS-ALTI-L3-VAVH (source catalogue)" in tex
    # No curation sections: the catalogue is pre-filter, so the curated
    # "Production Steps" (selection + sequential filter counts) are absent.
    assert "Dataset Production Steps" not in tex
    assert "Selection of Tiles" not in tex
    assert "Sequential quality-filter counts" not in tex
    # But the dataflow-position section that replaces it is present.
    assert "Position in the Dataflow" in tex
    assert "before quality curation" in tex
    # Cover date comes from the production date (2026-11-01), no leftover tokens.
    assert "@@DOC_RELEASE_DATE@@" not in tex and "\\DTMtoday" not in tex
    assert "2026-11-01" in tex
    # Manifest + figure bundle are written.
    manifest = json.loads((run / "manifest.json").read_text())
    assert manifest["report_tex"] == str(tex_path)
    assert manifest["reference"] == "altimeter" and manifest["input_rows"] == 8
    assets = run / "report" / "images_S1_catalogue_WV_ALTI"
    assert (assets / "cov_CCI_S1A.png").is_file()
    assert (assets / "sar_coverage.png").is_file()
    assert result["pdf"] is None


def test_build_catalogue_report_reuses_alti_figure_builders(tmp_path):
    # The three core figures are produced by the shared ALTI builders.
    from soba_reference_repo.catalogue_report import _read_catalogue
    run, paths = _build_catalogue_run(tmp_path)
    frame = _read_catalogue(_catalogues(paths))
    assert {"sar_lon", "sar_lat", "sar_time", "fname", "ref_hs_alti_closest"} <= set(frame.columns)
    assert "_curation_mission" in frame.columns  # tagged, so plot_alti_figures works
