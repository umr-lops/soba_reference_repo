# SOBA reference TEST datasets

[![CI](https://github.com/umr-lops/soba_reference_repo/actions/workflows/ci.yml/badge.svg)](https://github.com/umr-lops/soba_reference_repo/actions/workflows/ci.yml)
[![Build](https://github.com/umr-lops/soba_reference_repo/actions/workflows/build.yml/badge.svg)](https://github.com/umr-lops/soba_reference_repo/actions/workflows/build.yml)
[![Python](https://img.shields.io/badge/python-3.11-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![pre-commit](https://img.shields.io/badge/pre--commit-enabled-brightgreen?logo=pre-commit)](https://github.com/pre-commit/pre-commit)
[![Docs](https://readthedocs.org/projects/soba-reference-repo/badge/?version=latest)](https://soba-reference-repo.readthedocs.io/en/latest/?badge=latest)

Create Sentinel-1 Wave Mode (WV) reference TEST/TARGET Parquets and a SOBA report from co-aligned catalogues. The main workflow filters SWOT catalogues separately for each satellite, saves the Curated Parquets, merges them, and exports one paired TEST/TARGET dataset. A separate command crosses scatterometer and SWOT catalogues. ALTI and IW exports are not supported.

## Install

Use Python 3.11. From the repository root, install the CLI and its Python dependencies:

```bash
python -m pip install -e .
```

## Run a SWOT recipe

1. Copy the example. Keep your edited recipe in `runs/` so it stays out of Git:

   ```bash
   mkdir -p runs
   cp examples/swot-curation.toml runs/recipe.toml
   ```

2. Edit `runs/recipe.toml`: replace the S1A, S1C, and S1D `path` values with your WV SWOT Parquet files. Check `production_date`, `version`, and `output_dir`. The output directory must be new; the tool will not overwrite a run. Set `compile = true` if you have `pdflatex` or Tectonic and want a PDF. Otherwise, it writes the editable LaTeX report without compiling it.

3. Run:

   ```bash
   soba_reference_repo swot-test --recipe runs/recipe.toml
   ```

The example writes to `runs/swot-WV-20260930-0.1/` (paths resolve relative to the recipe). The run contains:

```text
recipe.toml                 Copy of the run settings
curated/                    One named Curated Parquet per satellite
merged/                     Saved combined Parquet
datasets/                   Paired TEST and TARGET Parquets
report/                     Figures, editable .tex, and optional PDF
manifest.json               Source files, rules, counts, and output paths
```

Each satellite starts with 13 predefined SWOT filters. A `[[catalogues.rules]]` entry changes a default filter when its `id` matches a default rule; a new `id` adds a filter after the defaults. The example includes commented overrides and additions. Filters run in order: report counts read `remaining / removed` after each filter. Curated counts can exceed TEST counts because the exporter also checks required fields and duplicate keys. A satellite can contribute zero rows if no input row passes its filters.

## SAR/SCAT/SWOT Cross

To merge SWOT catalogues with the fixed default filters and without saved Curated files, use `soba_reference_repo swot-test --swot-catalogue FILE` once each for S1A, S1C, and S1D. See `soba_reference_repo swot-test --help` for output options.

To cross one scatterometer catalogue with one SWOT catalogue for the same satellite:

```bash
soba_reference_repo --satellite S1D --scatterometer HSCAT \
  --scat /path/to/S1D_coaligned_catalogue_WV_..._KNMI-HSCAT-HY2-25km_0.2.parquet \
  --swot /path/to/S1D_coaligned_catalogue_WV_..._PODAAC-SWOT-KARIN-L2-WINDWAVE-D0_0.1.parquet \
  --no-compile --validate
```

Use co-aligned WV files with the project naming convention, such as `S1D_coaligned_catalogue_WV_...parquet`. The crossing workflow has its own filters and output schema; it does not use the SWOT recipe. See the [usage guide](docs/usage.rst) for its flags, the output columns, and validation details.

## Development

Run `python -m pytest -q` in an environment with the development dependencies. The SOBA report style and logo live in `assets/latex/`. The bundled consortium validator in `src/soba_reference_repo/validator.py` retains its upstream provenance and a marked local adaptation.

Licensed under [MIT](LICENSE).
