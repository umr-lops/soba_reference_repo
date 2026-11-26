"""The seam between building a catalogue (coalign) and curating it (curation)."""

import pyarrow.parquet as pq
from coalign_fixtures import OCN, make_archive, make_item, point, write_day

from soba_reference_repo.coalign.catalogue import build_catalogues
from soba_reference_repo.curation import curated_filename, resolve_rules, write_curated
from soba_reference_repo.dataset_validator import validate_dataset


def _catalogue(tmp_path):
    ocn_dir = make_archive(tmp_path / "arch", ocn=OCN)
    product = tmp_path / "stac" / "scat" / "hscat_hy-2b_25km"
    points = [point(-72.33, -66.2, wind_speed=11.0, quality_flag=0.0),
              point(-72.31, -66.19, wind_speed=-1.0, quality_flag=0.0),
              point(-72.30, -66.18, wind_speed=9.0, quality_flag=0.0)]
    write_day(product, "2026-11-19", [make_item(ocn_dir, 1, points)])
    paths, _ = build_catalogues([product], "2026-11-19", "2026-11-19", tmp_path / "catalogues",
                                production="20261126")
    return paths[0]


def test_a_coalign_catalogue_is_curated_by_the_unchanged_curation_code(tmp_path):
    catalogue = _catalogue(tmp_path)
    rules = resolve_rules(
        [{"id": "wind_positive", "name": "wind speed > 0",
          "clauses": [["windspeed_scat", "gt", 0]]}],
        include_defaults=False)
    curated_name = curated_filename(catalogue, "20261127", "1.0")
    info = write_curated(catalogue, tmp_path / "curated" / curated_name, rules, "S1C")
    assert (info["input_rows"], info["curated_rows"]) == (3, 2)
    curated = pq.read_table(info["path"])
    assert curated.column_names[-2:] == ["_curation_mission", "_curation_row"]
    assert curated.column("_curation_row").to_pylist() == [0, 2]
    report = validate_dataset(info["path"], "curated")
    assert not [c for c in report.checks if c[0] == "FAIL"], report.checks


def test_coalign_does_not_depend_on_curation_or_reports():
    import pathlib
    import re

    package = pathlib.Path(__file__).parents[1] / "src" / "soba_reference_repo" / "coalign"
    forbidden = re.compile(
        r"(from|import)\s+(\.\.|soba_reference_repo\.)"
        r"(curation|curate_dataset|curated_report|dataset_validator|validator|"
        r"catalogue_report|html_report|swot_test|hscat_test)\b")
    for source in package.glob("*.py"):
        assert not forbidden.search(source.read_text()), source.name
