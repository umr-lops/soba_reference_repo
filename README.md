# SOBA reference TEST datasets

[![CI](https://github.com/umr-lops/soba_reference_repo/actions/workflows/ci.yml/badge.svg)](https://github.com/umr-lops/soba_reference_repo/actions/workflows/ci.yml)
![Tests](https://img.shields.io/badge/tests-159-blue)
[![Build](https://github.com/umr-lops/soba_reference_repo/actions/workflows/build.yml/badge.svg)](https://github.com/umr-lops/soba_reference_repo/actions/workflows/build.yml)
[![conda-forge build](https://github.com/conda-forge/soba_reference_repo-feedstock/actions/workflows/conda-build.yml/badge.svg)](https://github.com/conda-forge/soba_reference_repo-feedstock/actions/workflows/conda-build.yml)
[![Python](https://img.shields.io/badge/python-3.11-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![pre-commit](https://img.shields.io/badge/pre--commit-enabled-brightgreen?logo=pre-commit)](https://github.com/pre-commit/pre-commit)
[![Docs](https://readthedocs.org/projects/soba-reference-repo/badge/?version=latest)](https://soba-reference-repo.readthedocs.io/en/latest/?badge=latest)

Create Sentinel-1 Wave Mode (WV) reference TEST/TARGET Parquets and a SOBA report from co-aligned catalogues. SWOT runs apply the existing 13 filters. HSCAT runs support wind speed and wind direction; wind-speed runs apply the ECMWF/HSCAT speed-difference filter.

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

2. Edit `runs/recipe.toml`: replace the S1A, S1C, and S1D `path` values with your WV SWOT Parquet files. Check `production_date`, `version`, and `output_dir`. The output directory **must be new**; the tool will not overwrite a run. Set `compile = true` if you have `pdflatex` or Tectonic and want a PDF. Otherwise, it writes the editable LaTeX report without compiling it.

3. Run:

   ```bash
   soba_create_test_dataset --recipe runs/recipe.toml
   ```

The recipe selects SWOT WV or HSCAT. The old `soba_reference_repo` command remains an alias. The Python client is now `soba_reference_repo.create_test_dataset`.

The example writes to `runs/swot-WV-20260930-0.1/` (paths resolve relative to the recipe). The run contains:

```text
recipe.toml                 Copy of the run settings
curated/                    One Curated Parquet per satellite
datasets/                   Paired TEST and TARGET Parquets
report/                     TeX, optional PDF, figures, and report assets directly
manifest.json               Source files, rules, counts, and output paths
```

Each satellite starts with 13 predefined SWOT filters. A `[[catalogues.rules]]` entry changes a default filter when its `id` matches a default rule; a new `id` adds a filter after the defaults. The example includes commented overrides and additions. Filters run in order: report counts read `remaining / removed` after each filter. Curated counts can exceed TEST counts because the exporter also checks required fields and duplicate keys. A satellite can contribute zero rows if no input row passes its filters. SCAT rule clauses also support `abs_diff_le` for absolute differences between two columns.

## Run an HSCAT recipe

Copy `examples/hscat-curation.toml` to `runs/`, update the HSCAT paths, and choose `windspeed` or `winddirection`. Each run writes paired TEST/TARGET Parquets for HSCAT. Wind-speed runs apply only `abs(ecmwf_wind_speed - ref_param_2) <= 2 m/s`; no rain filter is built in. Other filters can be added in the recipe once their definitions are settled. Rows with duplicate `primary_key` values after curation are excluded as a group from both outputs; the manifest and report record those integrity exclusions separately from filter counts.

The example has no TOML filter clauses because the **wind-speed difference rule is built** in.

## Build a CHALLENGER

Copy `examples/cci-seastate-challenger.toml`, set its paths and run settings, then run:

```bash
soba_produce_challenger --recipe path/to/challenger.toml
```

## Validate existing Parquets

Validate supported TEST, TARGET, and CHALLENGER files individually:

```bash
soba_validate_parquets --type test --file path/to/SWOT_TEST.parquet
soba_validate_parquets --type target --file path/to/SWOT_TARGET.parquet
soba_validate_parquets --type test --file path/to/SCAT_TEST.parquet --reference scat
soba_validate_parquets --type target --file path/to/SCAT_TARGET.parquet --reference scat
soba_validate_parquets --type challenger --file path/to/CHALLENGER.parquet
```

`--reference swot` is the default. Use `--reference scat` for SCAT TEST/TARGET files.

The default report shows the filename, row and column counts, and a `[PASS]` checklist of completed checks. It also identifies skipped checks and single-file limitations. Failed validation reports the error without printing a success checklist.

The validator accepts one file at a time through `--type` and `--file`. Each report includes a `Schema checked:` line with the checked column names and accepted types. Single-file validation does not verify alignment with another file.

## Development

Run `python -m pytest -q` in an environment with the development dependencies. The SOBA report style and logo live in `assets/latex/`.

Licensed under [MIT](LICENSE).
