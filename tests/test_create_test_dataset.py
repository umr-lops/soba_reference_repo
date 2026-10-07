"""Public recipe client and installed command contracts."""

import importlib
import importlib.util
from pathlib import Path
import tomllib

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_recipe_client_has_descriptive_module_name():
    assert importlib.util.find_spec("soba_reference_repo.create_test_dataset") is not None


def test_recipe_console_commands_share_descriptive_client():
    scripts = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["scripts"]
    expected = "soba_reference_repo.create_test_dataset:main"
    assert scripts.get("soba_create_test_dataset") == expected
    assert scripts["soba_reference_repo"] == expected


def test_recipe_help_states_current_support(capsys):
    client = importlib.import_module("soba_reference_repo.create_test_dataset")
    with pytest.raises(SystemExit) as exit_info:
        client.main(["--help"])
    assert exit_info.value.code == 0
    output = capsys.readouterr().out
    assert "TEST/TARGET" in output
    assert "SWOT WV" in output
    assert "--recipe" in output
