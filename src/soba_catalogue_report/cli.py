"""Generate the SOBA catalogue report: figures, LaTeX PDF and TEST parquet.

Example:

    python -m soba_catalogue_report.cli \\
        --scat /path/to/SCAT_catalogue.parquet \\
        --swot /path/to/SWOT_catalogue.parquet \\
        --satellite S1D --scatterometer ASCAT

After a successful compile the build directory is cleaned: only the PDF is left.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .report import (
    DEFAULT_LAND_MAP,
    DEFAULT_LATEX_DIR,
    DEFAULT_MIKTEX_BIN,
    DEFAULT_TEST_DIR,
    ReportConfig,
    build_report_tex,
    build_test_frame,
    compile_pdf,
    default_test_name,
    plot_figures,
    purge_latex_byproducts,
    run_crossing,
    stage_latex_assets,
    write_test_parquet,
)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scat", required=True, help="scatterometer catalogue parquet")
    parser.add_argument("--swot", required=True, help="SWOT KaRIn catalogue parquet")
    parser.add_argument("--satellite", required=True,
                        help="Sentinel-1 platform, e.g. S1D")
    parser.add_argument("--scatterometer", required=True,
                        help="scatterometer mission, ASCAT or HSCAT")
    parser.add_argument("--label", default=None,
                        help="defaults to <satellite>_swot_<scatterometer>")

    # collocation filters
    parser.add_argument("--overlap-min-pct", type=float, default=100.0,
                        help="required SAR/SWOT footprint overlap percentage")
    parser.add_argument("--rain-max-mm-h", type=float, default=0.3,
                        help="maximum IMERG mean rain rate in mm/h")
    parser.add_argument("--time-max-min", type=float, default=120.0,
                        help="maximum direct scatterometer-SWOT time difference in minutes")

    # outputs
    parser.add_argument("--output-dir", default=None,
                        help="report build directory; defaults to runs/<label>")
    parser.add_argument("--test-dir", default=str(DEFAULT_TEST_DIR),
                        help="directory for the TEST parquet and the run manifest")
    parser.add_argument("--test-name", default=None,
                        help="override the generated TEST filename")
    parser.add_argument("--dataset-version", default="0.1",
                        help="<version> field of the TEST filename (X.Y)")

    # inputs
    parser.add_argument("--latex-dir", default=str(DEFAULT_LATEX_DIR),
                        help="directory holding template.tex and its companion files")
    parser.add_argument("--land-map", default=str(DEFAULT_LAND_MAP))
    parser.add_argument("--miktex-bin", default=str(DEFAULT_MIKTEX_BIN),
                        help="directory holding pdflatex.exe (or a WSL pdflatex)")
    parser.add_argument("--compile", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--keep-intermediates", action="store_true",
                        help="keep the LaTeX byproducts (.aux, .log, .out, .toc)")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    label = args.label or f"{args.satellite.lower()}_swot_{args.scatterometer.lower()}"
    output_dir = Path(args.output_dir) if args.output_dir else Path("runs") / label
    test_dir = Path(args.test_dir)
    latex_dir = Path(args.latex_dir)

    config = ReportConfig(
        overlap_min_pct=args.overlap_min_pct,
        rain_max_mm_h=args.rain_max_mm_h,
        time_max_min=args.time_max_min,
    )

    scat_path, swot_path = Path(args.scat), Path(args.swot)
    result = run_crossing(scat_path, swot_path, args.satellite, config, args.scatterometer)
    print(f"common scenes: {len(result.crossing)}  filtered: {len(result.filtered)}")

    figures = plot_figures(result, output_dir / "figures", Path(args.land_map))
    stage_latex_assets(latex_dir, output_dir)

    tex = build_report_tex(
        latex_dir / "template.tex",
        result,
        label,
        [path.name for path in figures],
        config,
        scat_path.name,
        swot_path.name,
    )
    tex_path = output_dir / f"{label}_catalogue_report.tex"
    tex_path.write_text(tex, encoding="utf-8")

    test_name = args.test_name or default_test_name(
        result, scat_path.name, swot_path.name, args.satellite, args.dataset_version
    )
    test_path = write_test_parquet(build_test_frame(result), test_dir / test_name)

    pdf_path = None
    if args.compile:
        report_stem = f"{label}_catalogue_report"
        pdf_path = compile_pdf(output_dir, report_stem, Path(args.miktex_bin))
        if not args.keep_intermediates:
            # keep the .tex, figures and assets so the report can be hand-edited
            purge_latex_byproducts(output_dir, report_stem)

    manifest = test_dir / f"{label}_manifest.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({
        "scat": str(scat_path),
        "swot": str(swot_path),
        "satellite": result.satellite,
        "scatterometer": result.scatterometer,
        "config": config.__dict__,
        "scat_rows": result.scat_rows,
        "swot_rows": result.swot_rows,
        "common_scenes": len(result.crossing),
        "filtered_rows": len(result.filtered),
        "figures": [str(path) for path in figures],
        "tex": str(tex_path),
        "pdf": str(pdf_path) if pdf_path else None,
        "test_parquet": str(test_path),
    }, indent=2))

    print(f"pdf:  {pdf_path}")
    print(f"test: {test_path}")
    print(f"run:  {manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
