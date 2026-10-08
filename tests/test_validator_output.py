"""CLI reports describe completed checks without claiming skipped checks passed."""

import pytest

from soba_reference_repo import validator
from test_validator import challenger_table, product_table, save


@pytest.mark.parametrize("role", validator.FILE_TYPES)
def test_single_report_names_file_size_and_actual_checks(tmp_path, capsys, role):
    table = product_table(role) if role in ("test", "target") else challenger_table()
    path = save(tmp_path, table)
    assert validator.main(["--type", role, "--file", str(path)]) == 0
    output = capsys.readouterr().out
    assert f"{role.upper()}: {path}" in output
    assert f"Rows: {table.num_rows:,} | Columns: {table.num_columns}" in output
    schema_line = next(line for line in output.splitlines() if "Schema checked:" in line)
    for name in table.column_names:
        assert f"{name}: " in schema_line
    assert "exact columns and order" in schema_line
    assert "[PASS] Parquet readable" in output
    assert "[PASS] Field types" in output
    assert f"{role.upper()} validation passed" in output
    assert "Cross-file alignment: not checked" in output
    if role in ("test", "target"):
        assert "[PASS] Required five metadata attributes" in output
        assert "[PASS] Primary key composition" in output
        assert "[PASS] Required values non-null" in output
    else:
        assert "[PASS] Non-null predictions finite" in output
        assert "Missing predictions are allowed; prediction coverage is not checked" in output


def test_cli_rejects_removed_pair_mode(tmp_path, capsys):
    test = save(tmp_path, product_table("test"), "test.parquet")
    target = save(tmp_path, product_table("target"), "target.parquet")
    with pytest.raises(SystemExit) as error:
        validator.main(["--test", str(test), "--target", str(target)])
    assert error.value.code == 2
    assert "validation passed" not in capsys.readouterr().out
    with pytest.raises(SystemExit) as help_exit:
        validator.main(["--help"])
    assert help_exit.value.code == 0
    help_text = capsys.readouterr().out
    assert "--test " not in help_text
    assert "--target " not in help_text
    assert "catalogue" not in help_text
    assert "curated" not in help_text


def test_failed_validation_never_prints_a_pass_report(tmp_path, capsys):
    path = save(tmp_path, product_table("target").drop_columns(["primary_key"]))
    with pytest.raises(SystemExit) as exit_info:
        validator.main(["--type", "target", "--file", str(path)])
    assert exit_info.value.code == 1
    output = capsys.readouterr()
    assert "validation failed:" in output.err
    assert "[PASS]" not in output.out
    assert "validation passed" not in output.out
