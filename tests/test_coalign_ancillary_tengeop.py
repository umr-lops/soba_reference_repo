"""TenGeoP surface roughness classes (class_1, pos_1) from the CLS DL_Summary.xml files."""

import xml.etree.ElementTree as ET

import pandas as pd
import pytest
from coalign_fixtures import make_archive, make_item, point, write_day

from soba_reference_repo.coalign.catalogue import build_catalogues, collect
from soba_reference_repo.coalign.wv import slc_safe_dir
from soba_reference_repo.coalign.ancillary.tengeop import (
    TenGeoP, TenGeoPSource, dl_summary_path,
)

SLC = "S1C_WV_SLC__1SSV_20261119T003214_20261119T004119_010398_014C82_AAAA"
LABELS = "AF , BS , IB , LWA , MCC , OF , POS , RC , SI , WS"


def write_summary(root, safe=SLC, imagettes=None, labels=LABELS):
    """A DL_Summary.xml as the CLS classifier writes it, under <root>/<yyyy>/<mm>/<dd>/<DL SAFE>."""
    date = safe.split("_")[5]
    dl_safe = safe.replace("_SLC__1S", "_DL__2S") + ".SAFE"
    folder = root / date[:4] / date[4:6] / date[6:8] / dl_safe
    folder.mkdir(parents=True)
    imagettes = imagettes or {1: ("POS", 0.84, "OK"), 2: ("AF", 0.5, "OK")}
    body = "".join(
        f'<id value="{n}"><wm_type>wv1</wm_type><lonlat_center>-72.5 -66.1</lonlat_center>'
        f"<process_status>{status}</process_status>"
        + (f"<class_1>{cls}</class_1><prob_1>{prob}</prob_1>" if cls else "")
        + "</id>"
        for n, (cls, prob, status) in imagettes.items())
    (folder / "DL_Summary.xml").write_text(
        f"<DL_Summary><input_file><path>/x/{safe}.SAFE</path></input_file><configuration>"
        f"<model><short_label>{labels}</short_label></model></configuration>"
        f"<imagettes>{body}</imagettes></DL_Summary>")
    return folder


def test_dl_summary_path_swaps_the_product_tag(tmp_path):
    path = dl_summary_path(tmp_path, f"{SLC}.SAFE")
    dl_safe = SLC.replace("_SLC__1S", "_DL__2S") + ".SAFE"
    assert path == tmp_path / "2026/11/19" / dl_safe / "DL_Summary.xml"
    with pytest.raises(ValueError, match="WV SLC SAFE"):
        dl_summary_path(tmp_path, "S1C_WV_OCN__2SSV_20261119T003214_x.SAFE")


def test_class_and_probability_by_imagette(tmp_path):
    write_summary(tmp_path)
    classes = TenGeoP(tmp_path)
    assert classes.lookup(f"{SLC}.SAFE", 1) == ("POS", 0.84)
    assert classes.lookup(f"{SLC}.SAFE", 2) == ("AF", 0.5)


def test_missing_summary_or_imagette_or_failed_status_gives_none(tmp_path):
    write_summary(tmp_path, imagettes={1: ("POS", 0.9, "OK"), 2: (None, None, "ERROR")})
    classes = TenGeoP(tmp_path)
    assert classes.lookup(f"{SLC}.SAFE", 3) == (None, None)  # imagette absent
    assert classes.lookup(f"{SLC}.SAFE", 2) == (None, None)  # classifier failed on it
    other = SLC.replace("20261119", "20261120")
    assert classes.lookup(f"{other}.SAFE", 1) == (None, None)  # no summary for that SAFE
    assert classes.missing_summaries == 1


def test_unknown_class_label_is_rejected(tmp_path):
    write_summary(tmp_path, imagettes={1: ("XX", 0.9, "OK")})
    with pytest.raises(ValueError, match="XX"):
        TenGeoP(tmp_path).lookup(f"{SLC}.SAFE", 1)


def test_probability_outside_zero_one_is_rejected(tmp_path):
    write_summary(tmp_path, imagettes={1: ("POS", 1.7, "OK")})
    with pytest.raises(ValueError, match="1.7"):
        TenGeoP(tmp_path).lookup(f"{SLC}.SAFE", 1)


def test_summary_is_read_once_per_safe(tmp_path, monkeypatch):
    write_summary(tmp_path)
    calls = []
    real = ET.parse
    monkeypatch.setattr(ET, "parse", lambda p: calls.append(p) or real(p))
    classes = TenGeoP(tmp_path)
    for n in (1, 2, 1, 2):
        classes.lookup(f"{SLC}.SAFE", n)
    assert len(calls) == 1


def _stac(tmp_path):
    ocn_dir = make_archive(tmp_path / "arch")
    product = tmp_path / "stac" / "scat" / "hscat_hy-2b_25km"
    write_day(product, "2026-11-19", [
        make_item(ocn_dir, 1, [point(-72.33, -66.2, wind_speed=11.0)]),
        make_item(ocn_dir, 2, [point(-72.31, -66.19, wind_speed=9.0)]),
        make_item(ocn_dir, 3, [point(-72.30, -66.18, wind_speed=7.0)]),
    ])
    return product


def test_catalogue_gets_class_and_pos_and_keeps_rows_without_a_class(tmp_path):
    product = _stac(tmp_path)
    write_summary(tmp_path / "dl", imagettes={1: ("POS", 0.84, "OK"), 2: ("AF", 0.5, "OK")})
    frames, counts, _ = collect([product], "2026-11-19", "2026-11-19", slc_safe_dir,
                                sources=[TenGeoPSource(tmp_path / "dl")])
    frame = frames[("S1C", "SV", "KNMI-HSCAT-HY2-25km")].sort_values("primary_key")
    assert frame["class_1"].tolist()[:2] == ["POS", "AF"] and pd.isna(frame["class_1"].iloc[2])
    assert frame["pos_1"].tolist()[:2] == [0.84, 0.5] and pd.isna(frame["pos_1"].iloc[2])
    assert counts["no_class"] == 1


def test_without_a_tengeop_root_the_columns_stay_null(tmp_path):
    frames, counts, _ = collect([_stac(tmp_path)], "2026-11-19", "2026-11-19", slc_safe_dir)
    frame = frames[("S1C", "SV", "KNMI-HSCAT-HY2-25km")]
    assert frame["class_1"].isna().all() and "no_class" not in counts


def test_global_attributes_name_the_ancillary_source_and_library(tmp_path):
    import pyarrow.parquet as pq
    product = _stac(tmp_path)
    write_summary(tmp_path / "dl")
    paths, _ = build_catalogues([product], "2026-11-19", "2026-11-19", tmp_path / "out",
                                production="20261126", ancillary_paths={"tengeop": tmp_path / "dl"})
    meta = {k.decode(): v.decode() for k, v in pq.read_schema(paths[0]).metadata.items()}
    assert meta["source scat"] == "HY-2B HSCAT"
    assert "class: TenGeoP" in meta["source ancillary datasets"]
    assert meta["library used to produce the parquet"] == "soba_reference_repo"
    assert meta["creation date"] == "20261126" and meta["library version"]
