"""Curation-only export of coaligned catalogues (catalogue -> curated).

Applies each catalogue's recipe rules and saves one Curated Parquet per source
under ``curated/`` plus a ``manifest.json``. There is no TEST/TARGET pairing
and no report: the curated-to-TEST step is out of scope of this entry point.
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from .curation import curated_filename, read_recipe, resolve_rules, write_curated


def run_curate_recipe(recipe_path, compile_report=None, html=False, previous_run=None) -> int:
    """Curate every catalogue in a curation recipe and save one Parquet each.

    ``compile_report`` overrides the recipe ``compile`` flag when not None;
    ``html`` also writes the HTML companion of the report (``html_report``).
    """
    recipe_path = Path(recipe_path).resolve()
    recipe = read_recipe(recipe_path)
    if recipe["reference"] != "altimeter":
        raise ValueError(
            f"curation-only recipes require reference = 'altimeter', "
            f"got {recipe['reference']!r}"
        )
    root = Path(recipe["output_dir"])
    if root.exists():
        raise FileExistsError(f"refusing existing run directory: {root}")

    context = {
        "recipe_path": str(recipe_path),
        "dataset_version": recipe["version"],
        "production_date": recipe["production_date"],
    }
    if recipe.get("quality_disclaimers"):
        from .quality_disclaimers import load_flagged_safe_basenames

        context["qd_flagged"] = load_flagged_safe_basenames(recipe["quality_disclaimers"])

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
            info = write_curated(
                item["path"], target, item["resolved_rules"], item["mission"], context
            )
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
    print(f"curated rows: {total_out:,} of {total_in:,}")
    for source in manifest["sources"]:
        print(f"  {source['mission']}: {source['curated_rows']:,}/{source['input_rows']:,} "
              f"-> {Path(source['curated_path']).name}")
    if recipe.get("report", False):
        from .alti_report import build_alti_run_report

        if compile_report is None:
            compile_report = recipe.get("compile", False)
        report = build_alti_run_report(root, compile_report=compile_report)
        print(f"REPORT: {report['tex']}")
        if report["pdf"]:
            print(f"PDF:    {report['pdf']}")
        if html:
            from .html_report import build_html_report

            print(f"HTML:   {build_html_report(root, previous_run=previous_run)}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--recipe", type=Path, help="curation recipe to run")
    group.add_argument("--run-dir", type=Path,
                       help="build the report from an existing curated run directory")
    parser.add_argument("--no-compile", action="store_true", help="skip the PDF step")
    parser.add_argument("--html", action="store_true",
                        help="also write the interactive HTML producer report")
    parser.add_argument("--previous-run", type=Path, default=None,
                        help="curated run directory of the previous version (N-1), "
                             "compared in the HTML report")
    args = parser.parse_args(argv)
    if args.recipe is not None:
        return run_curate_recipe(args.recipe, compile_report=False if args.no_compile else None,
                                 html=args.html, previous_run=args.previous_run)
    from .alti_report import build_alti_run_report

    result = build_alti_run_report(args.run_dir, compile_report=not args.no_compile)
    print(f"REPORT: {result['tex']}")
    if result["pdf"]:
        print(f"PDF:    {result['pdf']}")
    if args.html:
        from .html_report import build_html_report

        print(f"HTML:   {build_html_report(args.run_dir, previous_run=args.previous_run)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
