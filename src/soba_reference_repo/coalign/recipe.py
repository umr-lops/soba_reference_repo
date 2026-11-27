"""Coalign recipe: run settings and every ancillary data path in one TOML file.

Relative paths are resolved against the recipe file, like the other SOBA recipes. A source
listed under ``[ancillary]`` must exist: a wrong path stops the run instead of leaving null
columns. ``sar_mode`` picks the catalogues: ``"WV"`` (default, ``wv.py``) or ``"IW"``
(``iw.py``: rows from GRD matchups; it also takes ``sar_parquet_root``, to put the OCN / SLC
twins on the rows, and ``[archives]``, where SAFEs are looked for).
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from .catalogue import build_catalogues

# Every ancillary source agreed for the catalogues, and whether its reader exists yet.
ANCILLARY = {
    "tengeop": True,   # class_1, pos_1: CLS deep-learning DL_Summary.xml tree
    "imerg": True,     # imerg_rainrate: IMERG Late V07B rain rate (mm/h), half-hourly
    "ecmwf": True,     # ECMWF_Wind_Speed, ECMWF_Wind_Direction: operational 10 m wind
    "era5": True,      # ERA5_Wind_Speed, ERA5_Wind_Direction: reanalysis 10 m wind
    "ww3": True,       # WW3_Hs, WW3_Tp: WAVEWATCH III CCI sea state run GLOB-30M
    "gebco": True,     # bathymetry: GEBCO grid, depth = -elevation at the nearest cell
    "seaice": True,    # sea_ice_concentration: Bremen ASI v5.4 AMSR2, 6.25 km, daily
}
KEYS = {"stac_root", "output_dir", "production_date", "version", "start", "end", "products",
        "satellites", "ancillary", "sar_mode"}
# Keys that only IW recipes take.
IW_KEYS = {"sar_parquet_root", "archives"}
SAR_MODES = ("WV", "IW")
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
    mode = recipe.setdefault("sar_mode", "WV")
    _check(mode in SAR_MODES, f"sar_mode must be one of {list(SAR_MODES)}")
    allowed = KEYS | (IW_KEYS if mode == "IW" else set())
    unknown, missing = recipe.keys() - allowed, REQUIRED - recipe.keys()
    _check(not unknown and not missing,
           f"unknown keys {sorted(unknown)} for sar_mode {mode}; missing keys {sorted(missing)}")
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
    if mode == "IW":
        archives = recipe.get("archives", {})
        _check(isinstance(archives, dict) and set(archives) <= {"ocn", "l1"},
               "archives takes 'ocn' (provider L2 tree) and 'l1' (list of MPC-layout roots)")
        _check(isinstance(archives.get("l1", []), list), "archives.l1 must be a list of paths")
        recipe["archives"] = {
            "ocn": (path.parent / archives["ocn"]).resolve() if archives.get("ocn") else None,
            "l1": [(path.parent / p).resolve() for p in archives.get("l1", [])],
        }
        if recipe.get("sar_parquet_root"):
            recipe["sar_parquet_root"] = (path.parent / recipe["sar_parquet_root"]).resolve()
    recipe["recipe_path"] = path
    return recipe


def mode_of(recipe: dict):
    """The catalogue ``Mode`` a validated recipe asks for."""
    if recipe["sar_mode"] == "IW":
        from .iw import IW, Archives, cdsodatacli_matcher

        parquet = recipe.get("sar_parquet_root")
        if parquet is not None and not parquet.is_dir():
            raise FileNotFoundError(f"sar_parquet_root {parquet} is not a folder")
        archives = recipe.get("archives", {"ocn": None, "l1": []})
        return IW(cdsodatacli_matcher(parquet) if parquet else None,
                  Archives(archives["ocn"], archives["l1"]))
    from .wv import WV

    return WV()


def run_recipe(path) -> tuple:
    """Build the catalogues a recipe describes. Returns (paths, counts)."""
    recipe = read_recipe(path)
    for name, folder in recipe["ancillary"].items():
        if not folder.is_dir():
            raise FileNotFoundError(f"ancillary {name}: {folder} is not a folder")
    meta = {}
    if recipe.get("sar_parquet_root"):
        meta["sar_parquet_root"] = str(recipe["sar_parquet_root"])
    return build_catalogues(
        [recipe["stac_root"] / product for product in recipe["products"]],
        recipe["start"], recipe["end"], recipe["output_dir"], recipe["version"],
        satellites=recipe["satellites"], production=recipe["production_date"],
        stac_root=recipe["stac_root"],
        recipe_path=recipe["recipe_path"], ancillary_paths=recipe["ancillary"],
        mode=mode_of(recipe), meta=meta)


def main(argv=None) -> int:
    import argparse

    import pyarrow.parquet as pq

    parser = argparse.ArgumentParser(
        prog="soba_build_coalign",
        description="Build WV (or, with sar_mode = \"IW\", IW) co-aligned catalogue Parquets "
                    "from soba_matchups STAC days.")
    parser.add_argument("--recipe", required=True, help="coalign recipe (TOML)")
    args = parser.parse_args(argv)
    paths, counts = run_recipe(args.recipe)
    for path in paths:
        print(f"{path} ({pq.ParquetFile(path).metadata.num_rows} rows)")
    print(counts)
    return 0
