"""coalign streaming writer: bounded memory, one file per group, atomic and strict."""

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from coalign_fixtures import make_archive, make_item, point, write_day

from soba_reference_repo.coalign import catalogue
from soba_reference_repo.coalign.catalogue import build_catalogues
from soba_reference_repo.coalign.rows import columns
from soba_reference_repo.dataset_validator import validate_dataset

DAYS = [f"2026-11-{d:02d}" for d in range(10, 16)]


def stac(tmp_path, points_per_day=5, days=DAYS, product="scat/hscat_hy-2b_25km"):
    """One OCN SAFE per day (so each day has its own SLC twin) with unique reference points."""
    root = tmp_path / "stac" / product
    for number, day in enumerate(days):
        stamp = day.replace("-", "")
        ocn = f"S1C_WV_OCN__2SSV_{stamp}T003214_{stamp}T004119_010398_014C82_3086"
        slc = ocn.replace("_OCN__2S", "_SLC__1S").replace("3086", "AAAA")
        ocn_dir = make_archive(tmp_path / "arch", ocn=ocn, slc=slc)
        points = [point(-72.0 - number - i * 0.01, -66.2 + i * 0.01, wind_speed=float(i),
                        quality_flag=0.0) for i in range(points_per_day)]
        item = make_item(ocn_dir, 1, points)
        item["properties"]["start_datetime"] = f"{day}T00:32:14Z"
        write_day(root, day, [item])
    return root


def run(tmp_path, root, out="out", **kwargs):
    return build_catalogues([root], DAYS[0], DAYS[-1], tmp_path / out, production="20261126",
                            **kwargs)


def test_one_file_spans_all_days_in_several_row_groups(tmp_path, monkeypatch):
    monkeypatch.setattr(catalogue, "FLUSH_ROWS", 10)
    paths, counts = run(tmp_path, stac(tmp_path))
    (path,) = paths
    assert path.name == ("S1C_coaligned_catalogue_WV_20261110_20261115_20261126_SV_"
                         "KNMI-HSCAT-HY2-25km_0.1.parquet")
    parquet = pq.ParquetFile(path)
    assert parquet.metadata.num_rows == 30 == counts["rows"]
    assert parquet.num_row_groups == 3          # 30 rows flushed every 10
    assert max(parquet.metadata.row_group(i).num_rows for i in range(3)) == 10
    meta = {k.decode(): v.decode() for k, v in parquet.schema_arrow.metadata.items()}
    assert (meta["start_date"], meta["end_date"], meta["rows"]) == ("20261110", "20261115", "30")
    assert meta["source scat"] == "HY-2B HSCAT"


def test_row_groups_are_sorted_inside_and_follow_the_day_order(tmp_path, monkeypatch):
    monkeypatch.setattr(catalogue, "FLUSH_ROWS", 5)
    (path,), _ = run(tmp_path, stac(tmp_path))
    parquet = pq.ParquetFile(path)
    firsts = []
    for i in range(parquet.num_row_groups):
        times = parquet.read_row_group(i).column("sar_time").to_pylist()
        assert times == sorted(times)
        firsts.append(times[0])
    assert firsts == sorted(firsts)


def test_content_does_not_depend_on_the_flush_size(tmp_path, monkeypatch):
    root = stac(tmp_path)
    (small,), _ = run(tmp_path, root, "small")
    monkeypatch.setattr(catalogue, "FLUSH_ROWS", 7)
    (big,), _ = run(tmp_path, root, "big")
    a = pq.read_table(small).sort_by("primary_key")
    b = pq.read_table(big).sort_by("primary_key")
    assert a.equals(b)


def test_schema_is_explicit_and_stable(tmp_path):
    (path,), _ = run(tmp_path, stac(tmp_path))
    schema = pq.read_schema(path)
    assert schema.names == columns(["windspeed_scat", "winddirection_scat"])
    assert schema.field("ref_flag").type == pa.int64()
    assert schema.field("sar_time").type == pa.timestamp("us")
    assert schema.field("legacy_usage").type == pa.string()
    assert schema.field("windspeed_scat").type == pa.float64()
    assert not [f.name for f in schema if pa.types.is_null(f.type)]


def test_ref_flag_with_some_nulls_stays_an_integer(tmp_path):
    ocn_dir = make_archive(tmp_path / "arch")
    product = tmp_path / "stac" / "scat" / "hscat_hy-2b_25km"
    write_day(product, "2026-11-19", [make_item(ocn_dir, 1, [
        point(-72.33, -66.2, wind_speed=1.0, quality_flag=2048.0),
        point(-72.31, -66.2, wind_speed=2.0)])])
    (path,), _ = build_catalogues([product], "2026-11-19", "2026-11-19", tmp_path / "out",
                                  production="20261126")
    table = pq.read_table(path)
    assert table.schema.field("ref_flag").type == pa.int64()
    assert sorted(table.column("ref_flag").to_pylist(), key=str) == [2048, None]


def test_duplicate_keys_across_chunks_fail_and_leave_no_file(tmp_path, monkeypatch):
    monkeypatch.setattr(catalogue, "FLUSH_ROWS", 2)
    root = stac(tmp_path, points_per_day=3)
    # the same SAR imagette and reference point again, in another day folder
    again = tmp_path / "stac" / "scat" / "hscat_hy-2b_25km_again"
    ocn_dir = make_archive(tmp_path / "arch2")
    item = make_item(ocn_dir, 1, [point(-72.0, -66.2, wind_speed=1.0, quality_flag=0.0)])
    write_day(again, DAYS[0], [item])
    write_day(again, DAYS[3], [item])
    with pytest.raises(ValueError, match="duplicated primary_key"):
        build_catalogues([root, again], DAYS[0], DAYS[-1], tmp_path / "out",
                         production="20261126")
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == []


def test_a_failure_in_the_middle_leaves_nothing_behind(tmp_path):
    root = stac(tmp_path)
    write_day(tmp_path / "stac" / "scat" / "bad", "2026-11-12", [],
              reference_product="NOT_A_PRODUCT")
    with pytest.raises(ValueError, match="no catalogue mapping"):
        build_catalogues([root, tmp_path / "stac" / "scat" / "bad"], DAYS[0], DAYS[-1],
                         tmp_path / "out", production="20261126")
    assert list((tmp_path / "out").iterdir()) == []


def test_only_the_finished_file_is_visible(tmp_path, monkeypatch):
    seen = []
    real = catalogue._Sink.flush

    def spy(self):
        seen.append(sorted(p.name for p in self.out_dir.iterdir()))
        return real(self)

    monkeypatch.setattr(catalogue._Sink, "flush", spy)
    monkeypatch.setattr(catalogue, "FLUSH_ROWS", 10)
    (path,), _ = run(tmp_path, stac(tmp_path))
    assert any(name.endswith(".partial") for names in seen for name in names)
    assert not any(n.endswith(".parquet") for names in seen for n in names)
    assert [p.name for p in path.parent.iterdir()] == [path.name]


def test_an_existing_file_is_never_overwritten_and_nothing_is_written_first(tmp_path):
    root = stac(tmp_path)
    run(tmp_path, root)
    before = sorted(p.name for p in (tmp_path / "out").iterdir())
    with pytest.raises(FileExistsError):
        run(tmp_path, root)
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == before


def test_peak_python_memory_does_not_grow_with_the_number_of_days(tmp_path, monkeypatch):
    import tracemalloc

    monkeypatch.setattr(catalogue, "FLUSH_ROWS", 50)
    peaks = {}
    for label, count in (("few", 6), ("many", 24)):
        days = [f"2026-12-{d:02d}" for d in range(1, count + 1)]
        root = stac(tmp_path / label, points_per_day=40, days=days)
        tracemalloc.start()
        build_catalogues([root], days[0], days[-1], tmp_path / f"out_{label}",
                         production="20261126")
        peaks[label] = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
    assert pq.read_metadata(next((tmp_path / "out_many").iterdir())).num_rows == 24 * 40
    assert peaks["many"] < 1.6 * peaks["few"], peaks


def test_output_passes_the_dataset_validator(tmp_path, monkeypatch):
    monkeypatch.setattr(catalogue, "FLUSH_ROWS", 10)
    (path,), _ = run(tmp_path, stac(tmp_path))
    report = validate_dataset(path, "catalogue")
    assert not [c for c in report.checks if c[0] == "FAIL"], report.checks
