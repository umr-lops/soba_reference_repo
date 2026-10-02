"""ESA CCI good-only CHALLENGER contract, using actual NetCDF observations."""

import json
import os
from pathlib import Path

import netCDF4
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from soba_reference_repo import challenger


NAME = "ESACCI-SEASTATE-L2P-ISSP-SENTINEL-1A_WV_IFR-20241231T235900-fv01.nc"
SAFE = "S1A_WV_OCN__2SSV_20241231T235900_20250101T000500_000000_000000_0000.SAFE:WV_001"


def recipe(tmp_path, rows, *, duplicate=False, quality=3, flags=0, swh=2.5):
    root = tmp_path / "archive"
    bucket = root / "products/v5/data/satellite/sar/sentinel-1a/l2p/2024/999"
    bucket.mkdir(parents=True)
    filename = bucket / NAME
    with netCDF4.Dataset(filename, "w") as ds:
        ds.createDimension("time", 2)
        time = ds.createVariable("time", "i8", ("time",))
        time.units = "nanoseconds since 1990-01-01"
        from datetime import datetime, timezone

        epoch = datetime(1990, 1, 1, tzinfo=timezone.utc)
        time[:] = [
            int((datetime(2025, 1, 1, tzinfo=timezone.utc) - epoch).total_seconds() * 1e9),
            int((datetime(2025, 1, 1, tzinfo=timezone.utc) - epoch).total_seconds() * 1e9)
            + 60_000_000_000,
        ]
        for name, data, dtype in (
            ("lat", [10.0, 80.0], "f4"),
            ("lon", [179.99, 0.0], "f4"),
            ("swh", [swh, 7.0], "f4"),
            ("swh_quality_level", [quality, 3], "i1"),
            ("swh_rejection_flags", [flags, 0], "i1"),
        ):
            ds.createVariable(name, dtype, ("time",))[:] = data
    if duplicate:
        other = bucket.parent / "001"
        other.mkdir()
        (other / NAME).write_bytes(filename.read_bytes())
    test = tmp_path / "test.parquet"
    pq.write_table(pa.Table.from_pylist(rows), test)
    config = tmp_path / "recipe.toml"
    config.write_text(
        'test = "test.parquet"\narchive_root = "archive"\noutput_dir = "out"\n'
        'production_date = "20261002"\nversion = "1.0"\n'
        "max_time_delta_seconds = 3\nmax_distance_km = 5\n"
    )
    return config


def row(key="good", **updates):
    data = dict(
        primary_key=key,
        sar_safe_ocn=SAFE,
        sar_time="2025-01-01T00:00:01",
        sar_lat=10.0,
        sar_lon=-179.99,
        swot_waveheight=999.0,
    )
    data.update(updates)
    return data


def read_run(path):
    out = path.parent / "out"
    result = pq.read_table(out / "S1_challenger_ESACCI-SEASTATE_WV_20261002_1.0.parquet")
    return result, json.loads((out / "audit.json").read_text())


def test_good_cross_year_opaque_bucket_and_no_reference_leak(tmp_path):
    path = recipe(tmp_path, [row(), row("missing", sar_safe_ocn=SAFE.replace("235900", "235901"))])
    challenger.produce_challenger(path)
    table, audit = read_run(path)
    assert table.column_names == ["primary_key", "swh"]
    assert table.schema.field("swh").nullable
    assert table.schema.field("swh").type == pa.float64()
    assert table.column("primary_key").to_pylist() == ["good", "missing"]
    assert table.column("swh").to_pylist() == [2.5, None]
    assert audit["rows"][0]["reason"] == "good"
    assert audit["rows"][1]["reason"] == "missing_source_file"
    assert audit["summary"]["total"] == 2
    assert "/home/" not in json.dumps(audit)
    with pytest.raises(FileExistsError):
        challenger.produce_challenger(path)


@pytest.mark.parametrize(
    "quality,flags,swh,reason",
    [
        (1, 0, 2.5, "bad_quality"),
        (2, 0, 2.5, "bad_quality"),
        (0, 0, 2.5, "bad_quality"),
        (3, 8, 2.5, "rejection_flags"),
        (3, 0, np.nan, "invalid_swh"),
        (3, 0, np.inf, "invalid_swh"),
    ],
)
def test_bad_observation_retains_null_key(tmp_path, quality, flags, swh, reason):
    path = recipe(tmp_path, [row()], quality=quality, flags=flags, swh=swh)
    challenger.produce_challenger(path)
    table, audit = read_run(path)
    assert table.column("swh").to_pylist() == [None]
    assert audit["rows"][0]["reason"] == reason


def test_duplicate_filename_start_is_ambiguous(tmp_path):
    path = recipe(tmp_path, [row()], duplicate=True)
    challenger.produce_challenger(path)
    assert read_run(path)[1]["rows"][0]["reason"] == "ambiguous_source_file"


def test_duplicate_observations_are_ambiguous(tmp_path):
    path = recipe(tmp_path, [row()])
    nc = next((tmp_path / "archive").rglob("*.nc"))
    with netCDF4.Dataset(nc, "a") as ds:
        ds["lat"][1] = 10
        ds["lon"][1] = 179.99
        ds["time"][1] = ds["time"][0]
    challenger.produce_challenger(path)
    assert read_run(path)[1]["rows"][0]["reason"] == "ambiguous_observation"


def test_missing_time_units_yield_audited_null(tmp_path):
    path = recipe(tmp_path, [row()])
    nc = next((tmp_path / "archive").rglob("*.nc"))
    with netCDF4.Dataset(nc, "a") as ds:
        del ds["time"].units
    challenger.produce_challenger(path)
    table, audit = read_run(path)
    assert table.column("swh").to_pylist() == [None]
    assert audit["rows"][0]["reason"] == "unsupported_time_units"


def test_many_source_files_never_stay_open_together(tmp_path, monkeypatch):
    import shutil

    starts = [f"20241231T2359{i:02d}" for i in range(10)]
    path = recipe(
        tmp_path,
        [
            row(str(i), sar_safe_ocn=SAFE.replace("235900", start[-6:]))
            for i, start in enumerate(starts)
        ],
    )
    source = next((tmp_path / "archive").rglob("*.nc"))
    for start in starts[1:]:
        shutil.copyfile(source, source.with_name(NAME.replace("20241231T235900", start)))
    original = netCDF4.Dataset
    state = {"open": 0, "peak": 0}

    class CountingDataset:
        def __init__(self, filename):
            self.ds = original(filename)
            state["open"] += 1
            state["peak"] = max(state["peak"], state["open"])

        def __enter__(self):
            return self.ds

        def __exit__(self, *args):
            self.ds.close()
            state["open"] -= 1

    monkeypatch.setattr(challenger.netCDF4, "Dataset", CountingDataset)
    challenger.produce_challenger(path)
    assert state == {"open": 0, "peak": 1}
    assert read_run(path)[0].num_rows == len(starts)


@pytest.mark.parametrize(
    "replacement",
    [
        'production_date = "20260230"',
        'version = "x"',
        "max_distance_km = 0",
        "max_time_delta_seconds = true",
        "unknown = 1",
    ],
)
def test_invalid_recipe_rejected_without_output(tmp_path, replacement):
    path = recipe(tmp_path, [row()])
    text = path.read_text()
    key = replacement.split(" = ")[0]
    import re

    text = (
        re.sub(rf"^{key} = .*?$", replacement, text, flags=re.M)
        if key in text
        else text + replacement + "\n"
    )
    path.write_text(text)
    with pytest.raises(ValueError):
        challenger.produce_challenger(path)
    assert not (tmp_path / "out").exists()


def test_cli_recipe(tmp_path):
    path = recipe(tmp_path, [row()])
    assert challenger.main(["--recipe", str(path)]) == 0
    assert read_run(path)[0].num_rows == 1


def test_real_copied_cci_files_with_local_test(tmp_path):
    if not os.environ.get("SOBA_TEST_PARQUET") or not os.environ.get("SOBA_CCI_SAMPLE_DIR"):
        pytest.skip("set SOBA_TEST_PARQUET and SOBA_CCI_SAMPLE_DIR for local real-file probe")
    original = Path(os.environ["SOBA_TEST_PARQUET"])
    sample_dir = Path(os.environ["SOBA_CCI_SAMPLE_DIR"])
    samples = [
        sample_dir / "ESACCI-SEASTATE-L2P-ISSP-SENTINEL-1A_WV_IFR-20241209T062454-fv01.nc",
        sample_dir / "ESACCI-SEASTATE-L2P-ISSP-SENTINEL-1C_WV_IFR-20250507T004810-fv01.nc",
    ]
    if not original.exists() or not all(p.exists() for p in samples):
        pytest.skip("local real CCI samples unavailable")
    table = pq.read_table(
        original, columns=["primary_key", "sar_safe_ocn", "sar_time", "sar_lat", "sar_lon"]
    )
    rows = [
        next(
            r
            for r in table.to_pylist()
            if r["sar_safe_ocn"].startswith(mission) and stamp in r["sar_safe_ocn"]
        )
        for mission, stamp in [("S1A", "20241209T062454"), ("S1C", "20250507T004810")]
    ]
    pq.write_table(pa.Table.from_pylist(rows), tmp_path / "test.parquet")
    for sample, mission, year in zip(samples, ["a", "c"], ["2024", "2025"]):
        import shutil

        folder = (
            tmp_path
            / "archive/products/v5/data/satellite/sar"
            / f"sentinel-1{mission}"
            / "l2p"
            / year
            / "999"
        )
        folder.mkdir(parents=True)
        shutil.copyfile(sample, folder / sample.name)
    path = tmp_path / "recipe.toml"
    path.write_text(
        'test = "test.parquet"\narchive_root = "archive"\noutput_dir = "out"\n'
        'production_date = "20261002"\nversion = "1.0"\n'
        "max_time_delta_seconds = 3\nmax_distance_km = 0.1\n"
    )
    challenger.produce_challenger(path)
    result, audit = read_run(path)
    assert result.column("primary_key").to_pylist() == [r["primary_key"] for r in rows]
    assert result.column("swh").to_pylist()[0] == pytest.approx(2.1119, abs=0.0001)
    assert result.column("swh").to_pylist()[1] is None
    assert audit["rows"][1]["reason"] == "bad_quality"
