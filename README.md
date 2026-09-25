# SOBA reference TEST datasets

[![CI](https://github.com/umr-lops/soba_reference_repo/actions/workflows/ci.yml/badge.svg)](https://github.com/umr-lops/soba_reference_repo/actions/workflows/ci.yml)
[![Build](https://github.com/umr-lops/soba_reference_repo/actions/workflows/build.yml/badge.svg)](https://github.com/umr-lops/soba_reference_repo/actions/workflows/build.yml)
[Documentation](https://soba-reference-repo.readthedocs.io/en/latest/) · [License](LICENSE)

Generate Sentinel-1 Wave Mode (WV) reference TEST datasets and SOBA-format reports from co-aligned catalogues. The CLI supports two distinct workflows: merge S1A/S1C/S1D SWOT KaRIn catalogues into an aligned TEST/TARGET pair, or cross one Sentinel-1 scatterometer catalogue with one SWOT catalogue. Both workflows produce figures and editable LaTeX; PDF compilation is optional.

The merged export applies thirteen ordered quality filters. Its source catalogues lack a direct SWOT land flag and SWOT nadir height: it uses positive SAR coast distance and the nearest altimeter height as documented proxies. It treats `overlap_pct >= 100` as full overlap because the source values never exceed 100. S1D has no classified rows in the current input catalogues, so none pass the strict filter set. Do not compare its output count directly with NECTAR counts from another sample.

## Install

Use Python 3.11 with NumPy, pandas, PyArrow, Matplotlib, and GeoPandas. In an environment that already contains those dependencies:

```bash
python -m pip install -e . --no-deps
```

Alternatively, run from the repository root with `PYTHONPATH=src python -m soba_reference_repo.cli` instead of `soba_reference_repo` in the commands below. PDF output requires `pdflatex` on `PATH` (or `--miktex-bin` / `MIKTEX_BIN`); the merged export can also use Tectonic if `pdflatex` is absent. Pass `--no-compile` to produce the LaTeX and figures without a TeX installation.

## Merge SWOT catalogues

Supply exactly one WV SWOT co-aligned Parquet for each of S1A, S1C, and S1D. Each glob below must resolve to exactly one file; adjust the directory and names for your catalogues.

```bash
DATA_DIR=/path/to/coaligned
soba_reference_repo swot-test \
  --swot-catalogue "$DATA_DIR"/S1A_coaligned_catalogue_WV_*_PODAAC-SWOT-KARIN-L2-WINDWAVE-D0_0.1.parquet \
  --swot-catalogue "$DATA_DIR"/S1C_coaligned_catalogue_WV_*_PODAAC-SWOT-KARIN-L2-WINDWAVE-D0_0.1.parquet \
  --swot-catalogue "$DATA_DIR"/S1D_coaligned_catalogue_WV_*_PODAAC-SWOT-KARIN-L2-WINDWAVE-D0_0.1.parquet \
  --test-dir test_datasets --report-dir runs/swot_merged --no-compile
```

The command defaults to the current UTC production date and version `0.1`; use `--production-date YYYYMMDD` and `--version X.Y` to choose them. With `--no-compile`, it writes:

```text
test_datasets/S1_WV_<date>_swh_0.1/
  S1_reference_test_dataset_WV_<date>_swh_0.1.parquet
  S1_target_dataset_WV_<date>_swh_0.1.parquet
runs/swot_merged/S1_reference_test_dataset_WV_<date>_swh_0.1/
  S1_reference_test_dataset_WV_<date>_swh_0.1.tex
  S1_reference_test_dataset_WV_<date>_swh_0.1_manifest.json
  images_S1_reference_test_dataset_WV_<date>_swh_0.1/*.png
```

Omit `--no-compile` to add the PDF beside the LaTeX source. The manifest records source paths, per-mission totals, cumulative filter counts, exclusions, and output paths. The coverage figure uses one color for all retained rows; monthly counts and wave-height distributions distinguish missions that pass the filters.

TEST carries the SAR identifiers, geometry, time and coast distance, plus `swot_lon`, `swot_lat`, `swot_waveheight`, `swot_time`, and `swot_source`. TARGET contains the same key, SAFE identifiers, SWOT position, and time. The exporter checks both schemas, metadata, and aligned keys. `sar_ground_heading` remains null because the inputs do not provide it. Source `ref_*` columns map to the `swot_*` output family; `sar_safe_slc` and `sar_safe_ocn` retain their underscore names.

Filters run in order: land/coast proxy; no dynamic ice; present time delta; positive KaRIn, OCN, nearest-altimeter and WW3 wave heights; present S1 classification; time separation under two hours; complete overlap; no IMERG rain; no SWOT rain flag; and S1 classes `AF`, `BS`, `MCC`, `OF`, `POS`, `RC`, or `WS`. The exporter then removes incomplete rows, keeps the closest-time duplicate within a mission, and rejects cross-mission key collisions. The [usage guide](docs/usage.rst) describes the source fields and limitations.

## Cross scatterometer and SWOT catalogues

The original command crosses one scatterometer and one SWOT catalogue for a single Sentinel-1 mission. Supply the matching co-aligned WV files:

```bash
soba_reference_repo \
  --satellite S1D --scatterometer ASCAT \
  --scat /path/to/S1D_coaligned_catalogue_WV_..._KNMI-ASCAT-METOP-12.5km_0.2.parquet \
  --swot /path/to/S1D_coaligned_catalogue_WV_..._PODAAC-SWOT-KARIN-L2-WINDWAVE-D0_0.1.parquet \
  --test-dir test_datasets --no-compile
```

This workflow writes a WV TEST Parquet and a run manifest to `--test-dir`; the report and figures go to `runs/<label>/` by default. Use `--validate` for the bundled SOBA SCAT/SWOT validator, or `--challenger` to also write a CHALLENGER Parquet. Its filters and schema differ from the merged SWOT-only export. See the [usage guide](docs/usage.rst) and [API documentation](docs/api/soba_reference_repo.rst) for its flags and matching rules.

## Development and provenance

Run `python -m pytest -q` from the repository root. The `assets/latex/` SOBA style and logo are retained with the report templates; `src/soba_reference_repo/validator.py` is a vendored consortium validator with a marked local reference-family adaptation. Keep that provenance when updating either asset. This project implements WV exports, not the IW layout in the SOBA format description.

Licensed under [MIT](LICENSE).
