"""Sphinx configuration for the soba_reference_repo documentation."""

import sys
from pathlib import Path

# the package lives under src/, so autodoc needs it on the path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

project = "soba_reference_repo"
copyright = "2026, LOPS Laboratory for Ocean Physics and Satellite remote sensing"
author = "ilias"
release = "0.1.0"

extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.napoleon",
    "sphinx.ext.autosummary",
    "sphinx.ext.viewcode",
    "myst_parser",
]
autosummary_generate = True

exclude_patterns = ["_build", "Thumbs.db", ".DS_Store"]

html_theme = "furo"
html_title = "soba_reference_repo"
