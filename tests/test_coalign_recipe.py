"""Coalign recipe: one TOML file holds the run settings and every ancillary data path."""

import pytest
from coalign_fixtures import make_archive, make_item, point, write_day
from test_coalign_ancillary_tengeop import write_summary

from soba_reference_repo.coalign.recipe import ANCILLARY, read_recipe, run_recipe

BASE = """
stac_root = "stac"
output_dir = "out"
production_date = "20261126"
version = "0.1"
start = "2026-11-19"
end = "2026-11-19"
products = ["scat/hscat_hy-2b_25km"]
"""


def write(tmp_path, text):
    path = tmp_path / "coalign.toml"
    path.write_text(text)
    return path


def test_paths_are_resolved_relative_to_the_recipe(tmp_path):
    recipe = read_recipe(write(tmp_path, BASE + '[ancillary]\ntengeop = "dl"\n'))
    assert recipe["stac_root"] == (tmp_path / "stac").resolve()
    assert recipe["output_dir"] == (tmp_path / "out").resolve()
    assert recipe["ancillary"] == {"tengeop": (tmp_path / "dl").resolve()}
    assert recipe["satellites"] is None


def test_ancillary_table_is_optional(tmp_path):
    assert read_recipe(write(tmp_path, BASE))["ancillary"] == {}


@pytest.mark.parametrize("text,message", [
    (BASE + 'extra = 1\n', "unknown keys"),
    (BASE.replace('version = "0.1"', ""), "missing keys"),
    (BASE.replace('"0.1"', '"1"'), "version"),
    (BASE.replace('"20261126"', '"2026-11-26"'), "production_date"),
    (BASE.replace('"2026-11-19"\nend', '"19/11/2026"\nend'), "start"),
    (BASE.replace('["scat/hscat_hy-2b_25km"]', "[]"), "products"),
    (BASE + '[ancillary]\nimerg = "/x"\n', "not implemented"),
    (BASE + '[ancillary]\nnonsense = "/x"\n', "unknown ancillary"),
    (BASE + '[ancillary]\ntengeop = 3\n', "path"),
    (BASE + 'satellites = ["S1X"]\n', "satellites"),
])
def test_invalid_recipes_are_rejected(tmp_path, text, message):
    with pytest.raises(ValueError, match=message):
        read_recipe(write(tmp_path, text))


def test_every_ancillary_source_of_the_project_is_named():
    """The recipe knows all agreed sources; only the implemented ones are accepted for now."""
    assert set(ANCILLARY) == {"tengeop", "imerg", "ecmwf", "era5", "ww3", "gebco", "seaice"}
    assert [k for k, v in ANCILLARY.items() if v] == [
        "tengeop", "ecmwf", "era5", "ww3", "gebco", "seaice"]


def test_a_missing_ancillary_folder_fails_instead_of_leaving_nulls(tmp_path):
    path = write(tmp_path, BASE + '[ancillary]\ntengeop = "does_not_exist"\n')
    with pytest.raises(FileNotFoundError, match="tengeop"):
        run_recipe(path)


def test_run_recipe_builds_files_with_classes_and_records_the_paths(tmp_path):
    import pyarrow.parquet as pq
    ocn_dir = make_archive(tmp_path / "arch")
    write_day(tmp_path / "stac" / "scat" / "hscat_hy-2b_25km", "2026-11-19",
              [make_item(ocn_dir, 1, [point(-72.33, -66.2, wind_speed=11.0)])])
    write_summary(tmp_path / "dl")
    path = write(tmp_path, BASE + '[ancillary]\ntengeop = "dl"\n')
    paths, counts = run_recipe(path)
    assert [p.parent for p in paths] == [(tmp_path / "out").resolve()]
    table = pq.read_table(paths[0])
    assert table.column("class_1").to_pylist() == ["POS"]
    meta = {k.decode(): v.decode() for k, v in table.schema.metadata.items()}
    assert meta["ancillary path tengeop"] == str((tmp_path / "dl").resolve())
    assert meta["recipe_path"] == str(path.resolve())
    assert counts["no_class"] == 0


def test_cli_runs_a_recipe_and_the_example_recipe_is_valid(tmp_path, capsys):
    from pathlib import Path

    from soba_reference_repo.coalign.recipe import main

    ocn_dir = make_archive(tmp_path / "arch")
    write_day(tmp_path / "stac" / "scat" / "hscat_hy-2b_25km", "2026-11-19",
              [make_item(ocn_dir, 1, [point(-72.33, -66.2, wind_speed=11.0)])])
    path = write(tmp_path, BASE)
    assert main(["--recipe", str(path)]) == 0
    assert "1 rows" in capsys.readouterr().out
    example = Path(__file__).parents[1] / "examples" / "coalign.toml"
    recipe = read_recipe(example)
    assert set(recipe["ancillary"]) == {"tengeop", "ecmwf", "era5", "ww3", "gebco", "seaice"}
    assert len(recipe["products"]) == 4
