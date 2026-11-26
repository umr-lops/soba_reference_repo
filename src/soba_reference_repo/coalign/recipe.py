"""Coalign recipe: run settings and every ancillary data path in one TOML file.

Relative paths are resolved against the recipe file, like the other SOBA recipes. A source
listed under ``[ancillary]`` must exist: a wrong path stops the run instead of leaving null
columns.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from .catalogue import build_catalogues

# Every ancillary source agreed for the catalogues, and whether its reader exists yet.
ANCILLARY = {
    "tengeop": True,   # class_1, pos_1: CLS deep-learning DL_Summary.xml tree
    "imerg": False,    # rain rate
    "ecmwf": False,    # operational 10 m wind
    "era5": False,     # reanalysis 10 m wind
    "ww3": False,      # CCI sea state WW3 run
    "gebco": False,    # bathymetry
}
KEYS = {"stac_root", "output_dir", "production_date", "version", "start", "end", "products",
        "satellites", "ancillary"}
REQUIRED = {"stac_root", "output_dir", "production_date", "version", "start", "end", "products"}
SATELLITE = re.compile(r"S1[A-D]")


def _check(condition, message):
    if not condition:
        raise ValueError(message)


def read_recipe(path) -> dict:
    """Load and validate a coalign recipe without touching the data."""
    path = Path(path).resolve()
    with path.open("rb") as stream:
        recipe = tomllib.load(stream)
    unknown, missing = recipe.keys() - KEYS, REQUIRED - recipe.keys()
    _check(not unknown and not missing,
           f"unknown keys {sorted(unknown)}; missing keys {sorted(missing)}")
    _check(re.fullmatch(r"\d+\.\d+", str(recipe["version"])), "version must look like X.Y")
    _check(re.fullmatch(r"\d{8}", str(recipe["production_date"])),
           "production_date must be YYYYMMDD")
    for key in ("start", "end"):
        _check(re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(recipe[key])), f"{key} must be YYYY-MM-DD")
    _check(recipe["start"] <= recipe["end"], "start is after end")
    products = recipe["products"]
    _check(isinstance(products, list) and products
           and all(isinstance(p, str) and p for p in products),
           "products must be a nonempty list of paths under stac_root")
    satellites = recipe.get("satellites")
    _check(satellites is None or (isinstance(satellites, list) and satellites
                                  and all(SATELLITE.fullmatch(str(s)) for s in satellites)),
           "satellites must be a list of S1A..S1D")
    recipe["satellites"] = satellites
    ancillary = recipe.get("ancillary", {})
    _check(isinstance(ancillary, dict), "ancillary must be a table of source = path")
    for name, value in ancillary.items():
        _check(name in ANCILLARY, f"unknown ancillary source {name!r}; known: {sorted(ANCILLARY)}")
        _check(ANCILLARY[name], f"ancillary source {name!r} is not implemented yet")
        _check(isinstance(value, str) and value, f"ancillary.{name} must be a path")
    recipe["ancillary"] = {name: (path.parent / value).resolve()
                           for name, value in ancillary.items()}
    recipe["stac_root"] = (path.parent / recipe["stac_root"]).resolve()
    recipe["output_dir"] = (path.parent / recipe["output_dir"]).resolve()
    recipe["recipe_path"] = path
    return recipe


def run_recipe(path) -> tuple:
    """Build the catalogues a recipe describes. Returns (paths, counts)."""
    recipe = read_recipe(path)
    for name, folder in recipe["ancillary"].items():
        if not folder.is_dir():
            raise FileNotFoundError(f"ancillary {name}: {folder} is not a folder")
    return build_catalogues(
        [recipe["stac_root"] / product for product in recipe["products"]],
        recipe["start"], recipe["end"], recipe["output_dir"], recipe["version"],
        satellites=recipe["satellites"], production=recipe["production_date"],
        stac_root=recipe["stac_root"], tengeop_root=recipe["ancillary"].get("tengeop"),
        recipe_path=recipe["recipe_path"], ancillary_paths=recipe["ancillary"])


def main(argv=None) -> int:
    import argparse

    import pyarrow.parquet as pq

    parser = argparse.ArgumentParser(
        prog="soba_build_coalign",
        description="Build WV co-aligned catalogue Parquets from soba_matchups STAC days.")
    parser.add_argument("--recipe", required=True, help="coalign recipe (TOML)")
    args = parser.parse_args(argv)
    paths, counts = run_recipe(args.recipe)
    for path in paths:
        print(f"{path} ({pq.ParquetFile(path).metadata.num_rows} rows)")
    print(counts)
    return 0
