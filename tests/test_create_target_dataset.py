"""TARGET dataset creation from published reference TEST Parquets."""

from __future__ import annotations

import importlib
from pathlib import Path
import tomllib

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from soba_reference_repo.create_target_dataset import (
    TARGET_COLUMNS,
    build_target_from_test,
    target_name,
    validate_target_file,
    validate_target_table,
    write_target,
)

ROOT = Path(__file__).resolve().parents[1]


def _test_table(n=4, safe_style="hyphen"):
    slc_name = "sar-safe-slc" if safe_style == "hyphen" else "sar_safe_slc"
    ocn_name = "sar-safe-ocn" if safe_style == "hyphen" else "sar_safe_ocn"
    return pa.Table.from_pandas(
        pd.DataFrame(
            {
                "primary_key": [f"safe:{i}" for i in range(n)],
                slc_name: [f"SLC_SAFE_{i}" for i in range(n)],
                ocn_name: [f"OCN_SAFE_{i}" for i in range(n)],
                "sar_lat": np.linspace(0.0, 10.0, n),
                "ref_lon": np.linspace(-1.0, 1.0, n),
                "ref_lat": np.linspace(50.0, 51.0, n),
                "ref_time": pd.date_range("2026-01-01", periods=n, freq="h", tz="UTC"),
                "ref_swh": np.linspace(0.5, 3.0, n),
            }
        ),
        preserve_index=False,
    )


def _test_path(tmp_path: Path, safe_style="hyphen") -> Path:
    path = tmp_path / "S1_reference_test_dataset_WV_20261119_swh_0.1.parquet"
    pq.write_table(_test_table(safe_style=safe_style), path)
    return path


def test_module_and_entry_point_are_registered():
    assert importlib.util.find_spec("soba_reference_repo.create_target_dataset") is not None
    scripts = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["scripts"]
    assert scripts["soba_create_target_dataset"] == (
        "soba_reference_repo.create_target_dataset:main"
    )


def test_target_name_is_the_spec_pair_of_the_test_name():
    assert (
        target_name("S1_reference_test_dataset_WV_20261119_swh_0.1.parquet")
        == "S1_target_dataset_WV_20261119_swh_0.1.parquet"
    )
    assert (
        target_name("S1_reference_test_dataset_WV_20261119_wind-direction_0.1.parquet")
        == "S1_target_dataset_WV_20261119_wind-direction_0.1.parquet"
    )


def test_target_name_rejects_other_filenames():
    with pytest.raises(ValueError, match="not a reference TEST filename"):
        target_name("S1_target_dataset_WV_20261119_swh_0.1.parquet")


def test_build_target_projects_six_columns_in_order(tmp_path):
    test_path = _test_path(tmp_path)
    target = build_target_from_test(test_path)
    assert list(target.column_names) == [
        "primary_key",
        "sar-safe-slc",
        "sar-safe-ocn",
        "ref_lon",
        "ref_lat",
        "ref_time",
    ]
    assert target.num_rows == 4
    assert "ref_swh" not in target.column_names
    assert target.column("primary_key").to_pylist() == [f"safe:{i}" for i in range(4)]


def test_build_target_resolves_underscore_safe_names(tmp_path):
    test_path = _test_path(tmp_path, safe_style="underscore")
    target = build_target_from_test(test_path)
    assert "sar_safe_slc" in target.column_names
    assert "sar_safe_ocn" in target.column_names
    assert target.column("sar_safe_slc").to_pylist() == [f"SLC_SAFE_{i}" for i in range(4)]


def test_build_target_fails_when_a_column_is_missing(tmp_path):
    path = tmp_path / "S1_reference_test_dataset_WV_20261119_swh_0.1.parquet"
    table = _test_table().drop_columns(["ref_time"])
    pq.write_table(table, path)
    with pytest.raises(ValueError, match="ref_time"):
        build_target_from_test(path)


def test_validate_target_table_rejects_duplicates_nulls_and_wrong_order():
    table = _test_table()
    target = table.select(TARGET_COLUMNS)
    validate_target_table(target, TARGET_COLUMNS)  # passes

    broken = target.set_column(
        0, pa.field("primary_key", pa.string()), pa.array(["safe:0", "safe:0", None, "safe:3"])
    )
    with pytest.raises(ValueError, match="non-null"):
        validate_target_table(broken, TARGET_COLUMNS)
    broken2 = target.set_column(
        0, pa.field("primary_key", pa.string()), pa.array(["a", "a", "b", "c"])
    )
    with pytest.raises(ValueError, match="unique"):
        validate_target_table(broken2, TARGET_COLUMNS)
    with pytest.raises(ValueError, match="must be"):
        validate_target_table(target.select(TARGET_COLUMNS[::-1]), TARGET_COLUMNS)


def test_write_target_writes_the_pair_with_metadata(tmp_path):
    test_path = _test_path(tmp_path)
    out = write_target(test_path, tmp_path / "targets")
    assert out.name == "S1_target_dataset_WV_20261119_swh_0.1.parquet"
    assert validate_target_file(out, test_path) is True

    schema = pq.read_schema(out)
    meta = {key.decode(): value.decode() for key, value in (schema.metadata or {}).items()}
    assert meta["soba_split"] == "target"
    assert meta["creation date"] == "20261119"
    assert meta["library used to produce the parquet"] == "soba_reference_repo"
    assert meta["source test dataset"] == test_path.name

    table = pq.read_table(out)
    assert list(table.column_names) == TARGET_COLUMNS
    assert table.num_rows == 4


def test_write_target_rejects_a_mismatched_row_sequence(tmp_path, monkeypatch):
    test_path = _test_path(tmp_path)
    real_read = pq.read_table

    def shuffled(path, columns=None):
        table = real_read(path, columns=columns)
        if columns == ["primary_key"]:
            return table.take(pa.array([0, 1, 3, 2]))
        return table

    monkeypatch.setattr("soba_reference_repo.create_target_dataset.pq.read_table", shuffled)
    with pytest.raises(ValueError, match="primary_key sequence"):
        write_target(test_path, tmp_path / "targets")


def test_cli_creates_one_target_per_test_parquet(tmp_path, capsys):
    test_dir = tmp_path / "tests"
    out_dir = tmp_path / "targets"
    test_dir.mkdir()
    for name in (
        "S1_reference_test_dataset_WV_20261119_swh_0.1.parquet",
        "S1_reference_test_dataset_WV_20261119_wind-speed_0.1.parquet",
    ):
        pq.write_table(_test_table(), test_dir / name)

    import soba_reference_repo.create_target_dataset as client

    assert client.main(["--test-dir", str(test_dir), "--out-dir", str(out_dir)]) == 0
    created = sorted(path.name for path in out_dir.glob("*.parquet"))
    assert created == [
        "S1_target_dataset_WV_20261119_swh_0.1.parquet",
        "S1_target_dataset_WV_20261119_wind-speed_0.1.parquet",
    ]
    assert "created 2 TARGET Parquet(s)" in capsys.readouterr().out


def test_cli_selects_with_only_and_fails_when_nothing_matches(tmp_path):
    test_dir = tmp_path / "tests"
    test_dir.mkdir()
    swh_name = "S1_reference_test_dataset_WV_20261119_swh_0.1.parquet"
    pq.write_table(_test_table(), test_dir / swh_name)

    import soba_reference_repo.create_target_dataset as client

    assert client.main(
        ["--test-dir", str(test_dir), "--out-dir", str(tmp_path / "targets"), "--only", "*_swh_*"]
    ) == 0
    assert [p.name for p in (tmp_path / "targets").glob("*.parquet")] == [
        "S1_target_dataset_WV_20261119_swh_0.1.parquet"
    ]

    with pytest.raises(SystemExit, match="no TEST Parquet matching"):
        client.main(
            ["--test-dir", str(test_dir), "--out-dir", str(tmp_path / "targets"), "--only", "wind*"]
        )
