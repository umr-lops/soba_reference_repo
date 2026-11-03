"""Co-aligned catalogue report: figures, LaTeX and the catalogue entry point."""

import json
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from soba_reference_repo.catalogue_report import (
    _dedup_by_mission_db,
    build_catalogue_report,
)

CCI = "CCI-SEATSTATE_ALTI_L2P-V5"
CMEMS = "CMEMS-ALTI-L3-VAVH"
SAFE = "S1A_WV_SLC__1SSV_20200101T000000_000000_000000_0000.SAFE"


def _catalogue_name(mission, product, prod="20261018T021205"):
    # Same naming as the real raw catalogues (one file per SAR unit / database).
    return (f"{mission}_coaligned_catalogue_WV_20150319T134347_20251231T192542_"
            f"{prod}_SV_{product}_1.0.parquet")


def _fname_for(product):
    if product == CCI:
        return "ESACCI-SEASTATE-L2P-SWH-SARAL-20200101T000000-fv01.nc"
    return "global_vavh_l3_rt_al_20200101_20200102_20200103.nc"


def _catalogue_table(mission, product):
    """Tiny raw catalogue frame (pre-curation). CCI and CMEMS carry different columns."""
    times = pd.DatetimeIndex([f"2020-01-0{i} 12:00:00" for i in range(1, 5)]).tz_localize("UTC")
    cols = {
        "sar_time": pa.array(times),
        "sar_lon": pa.array([-120.0, -119.5, -119.0, -118.5]),
        "sar_lat": pa.array([30.0, 30.5, 31.0, 31.5]),
        "sar_incidence_angle": pa.array([30.0, 31.0, 32.0, 33.0]),
        "sar_safe_slc": pa.array([SAFE] * 4),
        "primary_key": pa.array([f"{mission}-key-{i}" for i in range(4)]),
        "oswTotalHs": pa.array([1.1, 1.6, 2.1, 2.6]),
        "ref_hs_alti_closest": pa.array([1.0, 1.5, 2.0, 2.5]),
        "ref_time": pa.array(times),
        "ref_lon": pa.array([-120.1, -119.6, -119.1, -118.6]),
        "ref_lat": pa.array([30.1, 30.6, 31.1, 31.6]),
        "fname": pa.array([_fname_for(product)] * 4),
        "imerg_rainrate": pa.array([0.0, 0.0, 0.0, 0.0]),
        "ww3_swh": pa.array([1.1, 1.4, 2.1, 2.4]),
    }
    if product == CCI:
        # A CCI-only column (so the CCI table differs from the CMEMS table).
        cols["sar_distance_to_coast"] = pa.array([1.0, 2.0, 3.0, 4.0])
    else:
        # A CMEMS-only column.
        cols["ref_vavh_unfiltered"] = pa.array([1.0, 1.5, 2.0, 2.5])
    return pa.table(cols)


def _build_catalogue_run(tmp_path):
    """A minimal catalogue run: recipe.toml + catalogues for S1A (CCI+CMEMS), S1B (CCI+CMEMS),
    S1D (CMEMS only) + a duplicate S1A CCI re-run (to exercise dedup)."""
    run = tmp_path / "run"
    run.mkdir(parents=True)
    blocks = ['reference = "altimeter"', f'output_dir = "{run.name}"',
              'production_date = "20261101"', 'version = "1.0"', 'compile = false', '']
    paths = []
    plan = [
        ("S1A", CCI, "20261018T021205"),
        ("S1A", CCI, "20261019T030000"),  # re-run -> must be deduped to the latest
        ("S1A", CMEMS, "20261018T022921"),
        ("S1B", CCI, "20261019T114343"),
        ("S1B", CMEMS, "20261018T000937"),
        ("S1D", CMEMS, "20261017T232447"),
    ]
    for mission, product, prod in plan:
        src = run / _catalogue_name(mission, product, prod)
        pq.write_table(_catalogue_table(mission, product), src)
        paths.append(str(src))
        blocks += ['[[catalogues]]', f'mission = "{mission}"', f'path = "{src.name}"', '']
    (run / "recipe.toml").write_text("\n".join(blocks), encoding="utf-8")
    return run, paths


def _catalogues(paths):
    # missions come from the filenames, in file order
    missions = [Path(p).name.split("_")[0] for p in paths]
    return [{"mission": m, "path": p} for m, p in zip(missions, paths)]


def test_dedup_keeps_latest_per_mission_db():
    items = _catalogues([
        "x/S1A_coaligned_catalogue_WV_20150319T134347_20251231T192542_20261018T021205_SV_CCI-SEATSTATE_ALTI_L2P-V5_1.0.parquet",
        "x/S1A_coaligned_catalogue_WV_20150319T134347_20251231T192542_20261019T030000_SV_CCI-SEATSTATE_ALTI_L2P-V5_1.0.parquet",
        "x/S1A_coaligned_catalogue_WV_20191001T005910_20260629T193043_20261018T022921_SV_CMEMS-ALTI-L3-VAVH_1.0.parquet",
        "x/S1D_coaligned_catalogue_WV_20260107T022525_20261014T000306_20261017T232447_SV_CMEMS-ALTI-L3-VAVH_1.0.parquet",
    ])
    kept = _dedup_by_mission_db(items)
    keys = {(it["mission"], Path(it["path"]).name) for it in kept}
    # S1A CCI: only the latest production date (20261019) survives; S1A CMEMS + S1D kept.
    assert len(kept) == 3
    assert any("20261019T030000" in n for _, n in keys)
    assert not any("20261018T021205" in n for _, n in keys)


def test_build_catalogue_report_all_sar_units_and_per_db_tables(tmp_path):
    run, paths = _build_catalogue_run(tmp_path)
    result = build_catalogue_report(_catalogues(paths), version="1.0",
                                    production_date="20261101", output_dir=run,
                                    compile_report=False)
    tex = Path(result["tex"]).read_text(encoding="utf-8")
    manifest = json.loads((run / "manifest.json").read_text())
    # S1C absent on disk here; the 4 present SAR units (S1A, S1B, S1D) are all documented.
    assert {"S1A", "S1B", "S1D"} <= set(
        source["mission"] for source in manifest["sources"])
    # One file per (mission, db): S1A CCI deduped to 1, so 5 sources total.
    assert len(manifest["sources"]) == 5
    # Per-database column tables (different schemas), not a single merged table.
    assert "CCI SeaState V5 catalogue columns" in tex
    assert "CMEMS ALTI-L3-VAVH catalogue columns" in tex
    assert "tab:cols_cci" in tex and "tab:cols_cmems" in tex
    # CCI-only and CMEMS-only columns each appear only in their own table.
    assert "sar\\_distance\\_to\\_coast" in tex
    assert "ref\\_vavh\\_unfiltered" in tex
    assert "curated_WV" not in tex  # no curation provenance columns are described
    # Dataflow / enrichment section with the input path and the added columns.
    assert "How the Catalogue Is Produced" in tex
    assert "unified\\_colocs\\_wv\\_alti\\_ocn\\_v2" in tex  # input path, _tex-escaped
    assert "imerg\\_rainrate" in tex and "coloc\\_20" in tex
    assert "catawvaltioneshot" in tex
    # No curation sections: the catalogue is pre-filter.
    assert "Dataset Production Steps" not in tex
    assert "Selection of Tiles" not in tex
    assert "Sequential quality-filter counts" not in tex
    assert "Position in the Dataflow" in tex
    # Table 2 (Versioning of catalogue files) is dated with each file's own
    # production date (3rd date in the basename), not the recipe production date.
    assert "production date from filename" in tex
    assert "2026-10-17" in tex  # S1D CMEMS
    assert "2026-10-19" in tex  # S1A CCI (deduped re-run) + S1B CCI
    # No leftover tokens.
    import re
    assert re.findall(r"@@[A-Z_]+@@", tex) == []
    assert result["pdf"] is None
    assets = run / "report" / "images_S1_catalogue_WV_ALTI"
    assert (assets / "cov_CCI_S1A.png").is_file()
    assert (assets / "sar_coverage.png").is_file()


def test_file_version_rows_use_filename_production_date():
    from soba_reference_repo.catalogue_report import _file_version_rows
    s1a = ("S1A_coaligned_catalogue_WV_20150319T134347_20251231T192542_"
            "20261018T021205_SV_CCI-SEATSTATE_ALTI_L2P-V5_1.0.parquet")
    s1d = ("S1D_coaligned_catalogue_WV_20260107T022525_20261014T000306_"
            "20261017T232447_SV_CMEMS-ALTI-L3-VAVH_1.0.parquet")
    sources = [
        {"mission": "S1A", "path": s1a, "curated_path": s1a, "curated_rows": 10},
        {"mission": "S1D", "path": s1d, "curated_path": s1d, "curated_rows": 5},
    ]
    rows = _file_version_rows(sources)
    assert len(rows) == 2
    assert "2026-10-18" in rows[0] and "2026-10-17" in rows[1]
    # The recipe production date (e.g. today) must NOT be used for these rows.
    assert "2026-11-01" not in "".join(rows)


def test_read_catalogue_tags_mission_and_database(tmp_path):
    from soba_reference_repo.catalogue_report import _read_catalogue
    run, paths = _build_catalogue_run(tmp_path)
    frame = _read_catalogue(_catalogues(paths))
    assert "_curation_mission" in frame.columns and "_source_db" in frame.columns
    assert {"CCI", "CMEMS"} <= set(frame["_source_db"])
    # 4 rows x 6 (pre-dedup) files.
    assert len(frame) == 24
