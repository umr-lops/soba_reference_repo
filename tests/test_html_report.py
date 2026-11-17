"""Tests of the interactive HTML producer report (curated / catalogue runs)."""

import json
import re

import pandas as pd

from soba_reference_repo import html_report

CCI = "ESACCI-SEASTATE-L2P-SWH-SARAL-20200107T010203-fv01.nc"
CMEMS = "global_vavh_l3_rt_h2b_20230219T090000_20230219T120000_20230220T220742.nc"
CAT_NAME = ("S1A_coaligned_catalogue_WV_20200101T000000_20200301T000000_20261117T000000_"
            "SV_CCI-SEATSTATE_ALTI_L2P-V5_2.0.parquet")
CUR_NAME = "S1A_curated_WV_20200101_20200301_20261117_SV_CCI-SEATSTATE_ALTI_L2P-V5_5.0.parquet"


def _catalogue(path):
    frame = pd.DataFrame({
        "sar_time": pd.to_datetime(["2020-01-05", "2020-01-20", "2020-02-03", "2020-02-10"],
                                   utc=True),
        "sar_lat": [10.0, 11.0, 9.97e36, -50.0],
        "sar_lon": [20.0, 21.0, 9.97e36, 100.0],
        "fname": [CCI, CCI, CCI, CMEMS],
        "hs_alti_mean": [1.05, 2.0, 3.0, 4.0],
        "ref_hs_alti_closest": [1.0, 2.1, 3.0, 4.2],
        "sar_safe_slc": ["A.SAFE", "UNKNOWN", "B.SAFE", "C.SAFE"],
    })
    frame.to_parquet(path)
    return frame


def _curated_run(tmp_path, version="5.0", rules=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    source = tmp_path / CAT_NAME
    frame = _catalogue(source)
    rules = rules if rules is not None else [
        {"id": "sar_position", "name": "valid position", "clauses": [["sar_lat", "le", 90]]},
        {"id": "sar_safe_present", "name": "SLC known",
         "clauses": [["sar_safe_slc", "not_in", ["UNKNOWN", ""]]]},
    ]
    run = tmp_path / f"run_{version}"
    (run / "curated" / "S1A").mkdir(parents=True)
    kept = frame.iloc[[0, 3]]
    kept.to_parquet(run / "curated" / "S1A" / CUR_NAME)
    recipe = ['reference = "altimeter"', f'version = "{version}"', 'production_date = "20261117"',
              "[[catalogues]]", 'mission = "S1A"', f'path = "{source}"']
    for rule in rules:
        recipe += ["[[catalogues.rules]]", f'id = "{rule["id"]}"', f'name = "{rule["name"]}"',
                   f"clauses = {json.dumps(rule['clauses'])}"]
    (run / "recipe.toml").write_text("\n".join(recipe) + "\n")
    steps = [{"id": r["id"], "name": r["name"], "clauses": r["clauses"], "before": 4,
              "removed": 1, "remaining": 3} for r in rules]
    manifest = {"reference": "altimeter", "input_rows": 4, "curated_rows": 2,
                "sources": [{"mission": "S1A", "path": str(source),
                             "curated_path": str(run / "curated" / "S1A" / CUR_NAME),
                             "input_rows": 4, "curated_rows": 2, "steps": steps}]}
    (run / "manifest.json").write_text(json.dumps(manifest))
    return run


def _payload(page):
    return json.loads(re.search(r"const P = (\{.*?\});\nconst COLORS", page, re.S).group(1))


def test_altimeter_of():
    assert html_report.altimeter_of(CCI) == "saral"
    assert html_report.altimeter_of(CMEMS) == "hy-2b"
    assert html_report.altimeter_of("x.nc") == ""


def test_aggregate_histogram_monthly_and_map():
    frame = pd.DataFrame({
        "sar_time": pd.to_datetime(["2020-01-05", "2020-02-03"], utc=True),
        "sar_lat": [10.5, 9.97e36], "sar_lon": [181.0, 0.0], "hs_alti_mean": [1.05, None]})
    variables = html_report._variables("alti", set(frame.columns))
    assert [v[0] for v in variables] == ["hs_alti_mean"]
    agg = html_report.aggregate(frame, variables)
    assert agg["n"] == 2 and sum(agg["hist"]["hs_alti_mean"]) == 1
    assert agg["hist_missing"]["hs_alti_mean"] == 1
    assert agg["monthly"] == {"2020-01": 1, "2020-02": 1}
    assert agg["map"] == [[-179.0, 11.0, 1]]  # lon 181 -> -179, fill-value position dropped


def test_curated_run_replays_the_cascade(tmp_path):
    run = _curated_run(tmp_path)
    out = html_report.build_html_report(run)
    assert out.name == "S1_curated_5.0.html"
    page = out.read_text()
    assert "cdn.plot.ly/plotly-2.35.2.min.js" in page
    payload = _payload(page)
    assert payload["curated"] and payload["variables"][0]["id"] == "hs_alti_mean"
    (row,) = payload["cascade"]
    assert row["input"] == 4 and row["kept"] == 2 == row["manifest_kept"]
    assert [(r["id"], r["removed"]) for r in row["rules"]] == [("sar_position", 1),
                                                                 ("sar_safe_present", 1)]
    assert payload["data"]["rule:sar_safe_present"]["ALL"]["n"] == 1
    subsets = {s["id"] for s in payload["subsets"]}
    assert {"ALL", "S1A CCI", "S1A CCI / saral", "S1A CCI / hy-2b"} <= subsets
    warnings = {c["column"]: c["warnings"] for c in payload["columns"]}
    assert warnings["sar_lat"] == []  # the curated file has no fill value left


def test_catalogue_run_and_column_warnings(tmp_path):
    source = tmp_path / CAT_NAME
    _catalogue(source)
    run = tmp_path / "cat_run"
    run.mkdir()
    (run / "recipe.toml").write_text('reference = "altimeter"\nversion = "2.0"\n')
    (run / "manifest.json").write_text(json.dumps({"reference": "altimeter", "sources": [
        {"mission": "S1A", "path": str(source), "curated_path": str(source), "input_rows": 4,
         "curated_rows": 4, "steps": []}]}))
    payload = _payload(html_report.build_html_report(run).read_text())
    assert not payload["curated"] and payload["cascade"] == []
    assert [g["id"] for g in payload["groups"]] == ["kept"]
    warnings = {c["column"]: c["warnings"] for c in payload["columns"]}
    assert "latitude out of [-90, 90]" in warnings["sar_lat"]
    assert any("UNKNOWN" in w for w in warnings["sar_safe_slc"])


def test_compare_with_previous(tmp_path):
    old = _curated_run(tmp_path / "old", version="4.0", rules=[
        {"id": "sar_position", "name": "valid position", "clauses": [["sar_lat", "le", 80]]},
        {"id": "no_rain", "name": "no rain", "clauses": [["sar_lat", "ge", -90]]}])
    new = _curated_run(tmp_path / "new")
    diff = html_report.compare_with_previous(new, old)
    status = {r["id"]: r["status"] for r in diff["rules"]}
    assert status == {"sar_position": "changed", "no_rain": "removed", "sar_safe_present": "added"}
    assert diff["previous_version"] == "4.0" and diff["counts"][0]["new_kept"] == 2
    page = html_report.build_html_report(new, previous_run=old).read_text()
    assert _payload(page)["previous"]["rules"]


def test_curate_dataset_html_flag_on_run_dir(tmp_path, monkeypatch):
    from soba_reference_repo import alti_report, curate_dataset

    run = _curated_run(tmp_path)
    monkeypatch.setattr(alti_report, "build_alti_run_report",
                        lambda root, compile_report: {"tex": "x.tex", "pdf": None})
    assert curate_dataset.main(["--run-dir", str(run), "--no-compile", "--html"]) == 0
    assert (run / "report" / "S1_curated_5.0.html").is_file()
