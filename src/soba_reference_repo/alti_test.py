"""Curation-only export of WV x altimeter coaligned catalogues.

Unlike the SWOT exporter, this reference has no TEST/TARGET pair and no report:
each selected catalogue is curated (its recipe rules applied) and saved as one
Parquet under ``curated/``. It keeps the shared column types consistent across
catalogues so the curated set stays comparable.
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from .curation import curated_filename, read_recipe, resolve_rules, write_curated


def run_alti_recipe(recipe_path) -> int:
    """Curate every catalogue in an altimeter recipe and save one Parquet each."""
    recipe_path = Path(recipe_path).resolve()
    recipe = read_recipe(recipe_path)
    root = Path(recipe["output_dir"])
    if root.exists():
        raise FileExistsError(f"refusing existing run directory: {root}")

    catalogues = sorted(recipe["catalogues"], key=lambda item: (item["mission"], item["path"].name))
    for item in catalogues:
        name = curated_filename(item["path"], recipe["production_date"], recipe["version"])
        item["curated_name"] = name
        item["resolved_rules"] = resolve_rules(item.get("rules", []), include_defaults=False)

    root.mkdir(parents=True)
    shutil.copyfile(recipe_path, root / "recipe.toml")
    manifest = {"status": "running", "reference": "altimeter", "mode": "WV", "sources": []}
    try:
        for item in catalogues:
            target = root / "curated" / item["mission"] / item["curated_name"]
            target.parent.mkdir(parents=True, exist_ok=True)
            info = write_curated(item["path"], target, item["resolved_rules"], item["mission"])
            manifest["sources"].append({
                "mission": item["mission"],
                "path": str(item["path"]),
                "curated_path": str(target),
                "input_rows": info["input_rows"],
                "curated_rows": info["curated_rows"],
                "steps": info["steps"],
            })
        total_in = sum(source["input_rows"] for source in manifest["sources"])
        total_out = sum(source["curated_rows"] for source in manifest["sources"])
        manifest.update({"status": "complete", "input_rows": total_in, "curated_rows": total_out})
    except Exception as exc:
        manifest.update({"status": "failed", "error": str(exc)})
        (root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        raise
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"curated rows: {total_out:,} of {total_in:,} (altimeter curation)")
    for source in manifest["sources"]:
        print(f"  {source['mission']}: {source['curated_rows']:,}/{source['input_rows']:,} "
              f"-> {Path(source['curated_path']).name}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recipe", required=True, type=Path)
    args = parser.parse_args(argv)
    return run_alti_recipe(args.recipe)
