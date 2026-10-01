# SOBA reference TEST datasets

[![CI](https://github.com/umr-lops/soba_reference_repo/actions/workflows/ci.yml/badge.svg)](https://github.com/umr-lops/soba_reference_repo/actions/workflows/ci.yml)
![Tests](https://img.shields.io/badge/tests-54-blue)
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
   soba_reference_repo --recipe runs/recipe.toml
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

## Validate existing Parquets

The exporter checks the columns, metadata, and keys of each TEST/TARGET pair after writing it. Run the same check independently with:

```bash
soba_validate_parquets --test path/to/TEST.parquet --target path/to/TARGET.parquet
```

The validator currently supports SWOT TEST/TARGET pairs. CHALLENGER validation can be added when its schema and prediction source are defined; it does not produce CHALLENGER files.

## Development

Run `python -m pytest -q` in an environment with the development dependencies. The SOBA report style and logo live in `assets/latex/`.

Licensed under [MIT](LICENSE).
