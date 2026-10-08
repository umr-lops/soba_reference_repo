"""Create SWOT WV and HSCAT reference TEST/TARGET datasets from TOML recipes."""

from __future__ import annotations

import argparse
from pathlib import Path


def main(argv=None) -> int:
    """Run a reference recipe."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recipe", type=Path, help="TOML recipe for a reference dataset")
    args = parser.parse_args(argv)
    if args.recipe is None:
        parser.error("--recipe is required")
    from .curation import read_recipe

    recipe = read_recipe(args.recipe)
    if recipe["reference"] == "scat":
        from .hscat_test import run_hscat_recipe

        return run_hscat_recipe(args.recipe)
    from .swot_test import run_recipe

    return run_recipe(args.recipe)


if __name__ == "__main__":
    raise SystemExit(main())
