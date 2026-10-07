# SOBA reference TEST datasets

[![CI](https://github.com/umr-lops/soba_reference_repo/actions/workflows/ci.yml/badge.svg)](https://github.com/umr-lops/soba_reference_repo/actions/workflows/ci.yml)
![Tests](https://img.shields.io/badge/tests-158-blue)
[![Build](https://github.com/umr-lops/soba_reference_repo/actions/workflows/build.yml/badge.svg)](https://github.com/umr-lops/soba_reference_repo/actions/workflows/build.yml)
[![conda-forge build](https://github.com/conda-forge/soba_reference_repo-feedstock/actions/workflows/conda-build.yml/badge.svg)](https://github.com/conda-forge/soba_reference_repo-feedstock/actions/workflows/conda-build.yml)
[![Python](https://img.shields.io/badge/python-3.11-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![pre-commit](https://img.shields.io/badge/pre--commit-enabled-brightgreen?logo=pre-commit)](https://github.com/pre-commit/pre-commit)
[![Docs](https://readthedocs.org/projects/soba-reference-repo/badge/?version=latest)](https://soba-reference-repo.readthedocs.io/en/latest/?badge=latest)

Create Sentinel-1 Wave Mode (WV) reference TEST/TARGET Parquets and a SOBA report from co-aligned catalogues. The recipe filters SWOT catalogues separately for each satellite, saves the Curated Parquets, merges them, and exports one paired TEST/TARGET dataset. ALTI and IW exports are not supported yet; the 13 predefined filters apply only to SWOT.

## Install

Install the published package with pip:

```bash
python -m pip install soba_reference_repo
```

Or, once the first conda-forge build is published, install it with conda:

```bash
conda install -c conda-forge soba_reference_repo
```

For development from a checkout, install it from the repository root instead:

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
   soba_create_test_dataset --recipe runs/recipe.toml
   ```

The recipe selects the reference product; only SWOT WV is supported today. The old `soba_reference_repo` command remains an alias. The Python client is now `soba_reference_repo.create_test_dataset`.

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

## Build a CHALLENGER

Copy `examples/cci-seastate-challenger.toml`, set its paths and run settings, then run:

```bash
soba_produce_challenger --recipe path/to/challenger.toml
```

## Validate existing Parquets

Validate any supported file individually:

```bash
soba_validate_parquets --type catalogue --file path/to/catalogue.parquet
soba_validate_parquets --type curated --file path/to/curated.parquet
soba_validate_parquets --type test --file path/to/TEST.parquet
soba_validate_parquets --type target --file path/to/TARGET.parquet
soba_validate_parquets --type challenger --file path/to/CHALLENGER.parquet
```

The default report shows the filename, row and column counts, and a `[PASS]` checklist of completed checks. It also identifies skipped checks and single-file limitations. Failed validation reports the error without printing a success checklist.

Checks follow each file's current contract and never rewrite it. Catalogue/Curated validation covers the SWOT WV source fields, allows additional columns, and does not reapply recipe quality filters. TEST/TARGET checks cover schema, metadata, and individual keys. CHALLENGER checks cover `primary_key` and nullable float64 `swh`: missing predictions are allowed, but non-null predictions must be finite. Use `audit.json` for prediction coverage and null reasons.

The validator accepts one file at a time through `--type` and `--file`; pair mode is removed. Each report includes a `Schema checked:` line with the checked column names and accepted types. Catalogue/Curated permit extra columns in any order; TEST/TARGET/CHALLENGER require exact column names and order. Single-file validation does not verify alignment with another file. The dataset exporter retains its internal TEST/TARGET alignment check.

Only `--reference swot` is currently supported (and is the default). Validation exits 0 on success, 1 for invalid data, and 2 for invalid arguments.

## Development

Run `python -m pytest -q` in an environment with the development dependencies. The SOBA report style and logo live in `assets/latex/`.

Licensed under [MIT](LICENSE).
