"""Read-only validation of individual producer contracts."""

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from soba_reference_repo import validator, swot_test


def product_table(role):
    columns = swot_test.TEST_COLUMNS if role == "test" else swot_test.TARGET_COLUMNS
    strings = {"primary_key", "sar_safe_slc", "sar_safe_ocn", "swot_source"}
    values = {}
    for name in columns:
        if name in strings:
            value = {"primary_key": "S1A_WV.SAFE_1.0_2.0", "sar_safe_slc": "S1A_WV.SAFE",
                     "sar_safe_ocn": "S1A_OCN.SAFE", "swot_source": "swot.nc"}[name]
            values[name] = pa.array([value], type=pa.string())
        elif name.endswith("time"):
            values[name] = pa.array([0], type=pa.timestamp("ns"))
        else:
            value = None if name == "sar_ground_heading" else 2.0 if name == "swot_lat" else 1.0
            values[name] = pa.array([value], type=pa.float32())
    metadata = {key.encode(): value.encode() for key, value in swot_test.SWOT_METADATA.items()}
    metadata.update({b"library version": b"0.1", b"creation date": b"20261007"})
    return pa.table(values).replace_schema_metadata(metadata)


def save(tmp_path, table, name="file.parquet"):
    path = tmp_path / name
    pq.write_table(table, path)
    return path


@pytest.mark.parametrize("role", ["test", "target"])
def test_validate_single_product_and_cli_read_only(tmp_path, capsys, role):
    path = save(tmp_path, product_table(role))
    original = path.read_bytes()
    assert validator.validate_file(path, role) is True
    assert validator.main(["--type", role, "--file", str(path)]) == 0
    assert f"{role.upper()} validation passed" in capsys.readouterr().out
    assert path.read_bytes() == original


def replace(table, name, values, dtype=None):
    index = table.column_names.index(name)
    array = pa.array(values, type=dtype or table.schema.field(name).type)
    return table.set_column(index, name, array)


@pytest.mark.parametrize("role", ["test", "target"])
@pytest.mark.parametrize("column,values,dtype,message", [
    ("swot_lon", [1.0], pa.float64(), "type"),
    ("swot_time", [0], pa.timestamp("us"), "type"),
    ("primary_key", [None], None, "primary_key"),
    ("primary_key", [""], None, "primary_key"),
    ("sar_safe_slc", [""], None, "components"),
    ("swot_lon", [float("inf")], None, "finite"),
    ("swot_time", [None], None, "non-null"),
])
def test_product_rejects_invalid_types_and_values(tmp_path, role, column, values, dtype, message):
    table = replace(product_table(role), column, values, dtype)
    path = save(tmp_path, table)
    with pytest.raises(ValueError, match=message):
        validator.validate_file(path, role)


@pytest.mark.parametrize("value", [None, float("nan"), float("inf")])
def test_test_rejects_missing_or_nonfinite_measurement(tmp_path, value):
    path = save(tmp_path, replace(product_table("test"), "swot_waveheight", [value]))
    with pytest.raises(ValueError, match="non-null|finite"):
        validator.validate_file(path, "test")


def challenger_table(keys=("a", "b"), predictions=(1.2, None)):
    return pa.table({"primary_key": pa.array(keys, type=pa.string()),
                     "swh": pa.array(predictions, type=pa.float64())})


def test_challenger_accepts_nullable_predictions(tmp_path):
    path = save(tmp_path, challenger_table())
    original = path.read_bytes()
    assert validator.validate_file(path, "challenger") is True
    assert validator.main(["--type", "challenger", "--file", str(path)]) == 0
    assert path.read_bytes() == original


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_challenger_rejects_nonnull_nonfinite_prediction(tmp_path, value):
    path = save(tmp_path, challenger_table(predictions=(value, None)))
    with pytest.raises(ValueError, match="finite"):
        validator.validate_file(path, "challenger")


@pytest.mark.parametrize("keys", [("a", "a"), (None, "b"), ("", "b")])
def test_challenger_rejects_invalid_keys(tmp_path, keys):
    path = save(tmp_path, challenger_table(keys=keys))
    with pytest.raises(ValueError, match="primary_key"):
        validator.validate_file(path, "challenger")


@pytest.mark.parametrize("mutation", ["missing", "extra", "float32", "nonnullable"])
def test_challenger_requires_actual_producer_schema(tmp_path, mutation):
    table = challenger_table()
    if mutation == "missing":
        table = table.drop(["swh"])
    elif mutation == "extra":
        table = table.append_column("swot_waveheight", pa.array([1.0, 2.0]))
    elif mutation == "float32":
        table = replace(table, "swh", [1.0, None], pa.float32())
    else:
        table = pa.table([table.column("primary_key"), pa.array([1.0, 2.0])], schema=pa.schema([
            pa.field("primary_key", pa.string()), pa.field("swh", pa.float64(), nullable=False),
        ]))
    path = save(tmp_path, table)
    with pytest.raises(ValueError, match="schema|type|nullable"):
        validator.validate_file(path, "challenger")


def native_table():
    output = product_table("test")
    mapping = {"swot_lon": "ref_lon", "swot_lat": "ref_lat", "swot_time": "ref_time",
               "swot_waveheight": "ref_mean_hs_karin", "swot_source": "swot_path"}
    values = {}
    for name in swot_test.SOURCE_COLUMNS[:13]:
        output_name = next((key for key, value in mapping.items() if value == name), name)
        values[name] = output.column(output_name)
    # Native metadata is unconstrained and retained by the Curated writer.
    return pa.table(values).replace_schema_metadata({b"source": b"native catalogue"})


@pytest.mark.parametrize("empty", [False, True])
def test_native_catalogue_and_real_curated_writer_contract(tmp_path, empty):
    from soba_reference_repo.curation import write_curated
    table = native_table().append_column("extra", pa.array([99]))
    table = replace(table, "ref_mean_hs_karin", [-1.0], pa.float64())
    table = replace(table, "sar_safe_slc", ["/archive/S1A_WV.SAFE"])
    source = save(tmp_path, table, "catalogue.parquet")
    assert validator.validate_file(source, "catalogue") is True
    curated = tmp_path / "curated.parquet"
    rules = [{"id": "remove", "name": "remove", "clauses": [["extra", "lt", 0]]}] if empty else []
    write_curated(source, curated, rules, "S1A")
    original = curated.read_bytes()
    assert validator.validate_file(curated, "curated") is True
    assert validator.main(["--type", "curated", "--file", str(curated)]) == 0
    assert curated.read_bytes() == original
    assert pq.read_schema(curated).metadata == table.schema.metadata


@pytest.mark.parametrize("role", ["catalogue", "curated"])
@pytest.mark.parametrize("mutation", ["missing", "numeric_string", "bad_time"])
def test_native_rejects_missing_fields_and_invalid_types(tmp_path, role, mutation):
    table = native_table()
    if role == "curated":
        table = table.append_column("_curation_mission", pa.array(["S1A"]))
        table = table.append_column("_curation_row", pa.array([0], type=pa.int64()))
    if mutation == "missing":
        table = table.drop(["ref_lon"])
    elif mutation == "numeric_string":
        table = replace(table, "sar_lat", ["2.0"], pa.string())
    else:
        table = replace(table, "sar_time", ["not a time"], pa.string())
    path = save(tmp_path, table)
    with pytest.raises(ValueError, match="missing|type|time"):
        validator.validate_file(path, role)


@pytest.mark.parametrize("field,values,dtype", [
    ("_curation_mission", [None], pa.string()),
    ("_curation_mission", ["SCAT"], pa.string()),
    ("_curation_row", [-1], pa.int64()),
    ("_curation_row", [None], pa.int64()),
    ("_curation_row", [0.0], pa.float64()),
])
def test_curated_rejects_invalid_provenance(tmp_path, field, values, dtype):
    table = native_table().append_column("_curation_mission", pa.array(["S1A"]))
    table = table.append_column("_curation_row", pa.array([0], type=pa.int64()))
    path = save(tmp_path, replace(table, field, values, dtype))
    with pytest.raises(ValueError, match="curation"):
        validator.validate_file(path, "curated")


@pytest.mark.parametrize("args", [
    [], ["--type", "test"], ["--file", "x.parquet"], ["--test", "x.parquet"],
    ["--target", "x.parquet"],
    ["--type", "test", "--file", "x", "--test", "y", "--target", "z"],
    ["--type", "test", "--test", "y", "--target", "z"],
    ["--file", "x", "--test", "y", "--target", "z"],
    ["--type", "scat", "--file", "x"],
    ["--type", "test", "--file", "x", "--reference", "scat"],
    ["--kind", "test", "--file", "x"],
])
def test_cli_rejects_invalid_modes_with_usage_exit(args, capsys):
    with pytest.raises(SystemExit) as error:
        validator.main(args)
    assert error.value.code == 2
    assert "usage:" in capsys.readouterr().err


@pytest.mark.parametrize("role", validator.FILE_TYPES)
@pytest.mark.parametrize("exists", [False, True])
def test_cli_reports_unreadable_file_as_validation_failure(tmp_path, capsys, role, exists):
    path = tmp_path / "bad.parquet"
    if exists:
        path.write_text("not parquet")
    with pytest.raises(SystemExit) as error:
        validator.main(["--type", role, "--file", str(path)])
    assert error.value.code == 1
    assert "validation failed:" in capsys.readouterr().err


@pytest.mark.parametrize("role", ["test", "target"])
@pytest.mark.parametrize("mutation", ["columns", "metadata", "duplicate", "composition"])
def test_product_schema_metadata_and_keys_are_checked_individually(tmp_path, role, mutation):
    table = product_table(role)
    if mutation == "columns":
        table = table.drop(["swot_lat"])
    elif mutation == "metadata":
        table = table.replace_schema_metadata({b"creation date": b"20261007"})
    elif mutation == "duplicate":
        table = pa.concat_tables([table, table])
    else:
        table = replace(table, "primary_key", ["wrong"])
    path = save(tmp_path, table)
    with pytest.raises(ValueError):
        validator.validate_file(path, role)


def test_exporter_still_checks_ordered_alignment_and_metadata(tmp_path):
    test = save(tmp_path, product_table("test"), "test.parquet")
    target_table = product_table("target")
    target = save(tmp_path, target_table, "target.parquet")
    assert swot_test.validate_swot_test_pair(test, target) is True
    different = replace(target_table, "swot_lon", [3.0])
    different = replace(different, "primary_key", ["S1A_WV.SAFE_3.0_2.0"])
    save(tmp_path, different, target.name)
    assert validator.validate_file(target, "target")
    with pytest.raises(ValueError, match="primary_key values differ"):
        swot_test.validate_swot_test_pair(test, target)
    metadata = {**target_table.schema.metadata, b"library version": b"9.9"}
    save(tmp_path, target_table.replace_schema_metadata(metadata), target.name)
    with pytest.raises(ValueError, match="metadata values differ"):
        swot_test.validate_swot_test_pair(test, target)


def test_single_file_does_not_accept_parquet_dataset_directory(tmp_path):
    save(tmp_path, challenger_table())
    with pytest.raises((ValueError, OSError)):
        validator.validate_file(tmp_path, "challenger")


def test_nullable_heading_rejects_nonnull_nan(tmp_path):
    path = save(tmp_path, replace(product_table("test"), "sar_ground_heading", [float("nan")]))
    with pytest.raises(ValueError, match="finite"):
        validator.validate_file(path, "test")
