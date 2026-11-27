# soba_reference_repo

[![CI](https://github.com/umr-lops/soba_reference_repo/actions/workflows/ci.yml/badge.svg)](https://github.com/umr-lops/soba_reference_repo/actions/workflows/ci.yml)
[![Coverage](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/umr-lops/soba_reference_repo/badges/coverage.json)](https://github.com/umr-lops/soba_reference_repo/actions/workflows/ci.yml)
[![Tests](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/umr-lops/soba_reference_repo/badges/tests.json)](https://github.com/umr-lops/soba_reference_repo/actions/workflows/ci.yml)
[![Build](https://github.com/umr-lops/soba_reference_repo/actions/workflows/build.yml/badge.svg)](https://github.com/umr-lops/soba_reference_repo/actions/workflows/build.yml)
[![conda-forge build](https://github.com/conda-forge/soba_reference_repo-feedstock/actions/workflows/conda-build.yml/badge.svg)](https://github.com/conda-forge/soba_reference_repo-feedstock/actions/workflows/conda-build.yml)
[![Python](https://img.shields.io/badge/python-3.11-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![pre-commit](https://img.shields.io/badge/pre--commit-enabled-brightgreen?logo=pre-commit)](https://github.com/pre-commit/pre-commit)
[![Docs](https://readthedocs.org/projects/soba-reference-repo/badge/?version=latest)](https://soba-reference-repo.readthedocs.io/en/latest/?badge=latest)

Recipe-driven production of the SOBA (SAR Ocean Benchmarks Analysis) reference datasets for
Sentinel-1, from the `soba_matchups` STAC matchups to the files published for the benchmarks:

1. **co-aligned catalogues** (WV and IW): every matchup with its SAR metadata and ancillary data;
2. **curated datasets**: the catalogues filtered by recipe rules, with a report;
3. **TEST / TARGET datasets**: the benchmark pairs (SWOT, HSCAT) and their report;
4. **CHALLENGER** files: predictions to compare with a TEST dataset;
5. **validation** of every file against the
   [SOBA parquet format specification](https://soba.ifremer.fr/parquet_format_specification.html).

Recipes are TOML files; relative paths in them resolve against the recipe file. Runs never
overwrite an existing output.

## Contents

- [Install](#install)
- [Commands](#commands)
- [Co-aligned catalogues](#co-aligned-catalogues)
- [Curated datasets](#curated-datasets)
- [TEST and TARGET datasets](#test-and-target-datasets)
- [CHALLENGER](#challenger)
- [Validation](#validation)
- [Development](#development)

## Install

```bash
python -m pip install soba_reference_repo          # published package
conda install -c conda-forge soba_reference_repo   # once the first conda-forge build is out
python -m pip install -e .                          # development, from a checkout
```

Optional extras: `.[challenger]` (netCDF4, for the CHALLENGER and the ancillary readers),
`.[iw]` (cdsodatacli, for the OCN / SLC twins of the IW catalogues).

## Commands

| Step | Command | Example recipe |
|---|---|---|
| Co-aligned catalogues (WV, IW) | `soba_build_coalign --recipe r.toml` | [coalign.toml](examples/coalign.toml), [coalign-iw.toml](examples/coalign-iw.toml) |
| Co-aligned catalogue report | `soba_catalogue_report --recipe r.toml \| --run-dir run [--html]` | – |
| Curated datasets (WV altimeter) | `soba_curate_dataset --recipe r.toml \| --run-dir run [--html]` | [alti-curation.toml](examples/alti-curation.toml), [wv-alti-curation-v2.0.toml](examples/wv-alti-curation-v2.0.toml) |
| Curated + TEST / TARGET (SWOT, HSCAT) | `soba_create_test_dataset --recipe r.toml` | [swot-curation.toml](examples/swot-curation.toml), [hscat-curation.toml](examples/hscat-curation.toml) |
| TARGET from published TEST files | `soba_create_target_dataset --test-dir d --out-dir d` | – |
| Interactive HTML producer report | `soba_html_report --run-dir run` | – |
| CHALLENGER | `soba_produce_challenger --recipe r.toml` | [cci-seastate-challenger.toml](examples/cci-seastate-challenger.toml) |
| Validation | `soba_validate_parquets --type <type> --file f.parquet ...` | – |

`soba_reference_repo` remains an alias of `soba_create_test_dataset`.

## Co-aligned catalogues

`soba_build_coalign` reads the `soba_matchups` day folders of one or more collections
(`products`, under `stac_root`) and writes one Parquet per satellite, polarization and reference
product, named `S1X_coaligned_catalogue_<mode>_<start>_<stop>_<production>_<pol>_<product>_<X.Y>`.

- Both matchup layouts are read: the common one, `<collection>/<YYYY-MM-DD>/<product_type>/<mode>/`,
  and the older one where the day folder holds the run.
- Rows are streamed (memory does not grow with the period), sorted by SAR time; a repeated
  `primary_key` is kept once and counted in the file metadata; a failed run leaves no file.
- The catalogues need SAR **metadata only**: `sar_path_*` is the SAFE path when it is on disk,
  empty otherwise; the row is kept.
- `sar_lat` / `sar_lon` are not written (spec 1.5.0): the reference point locates the row.

### WV (default, `sar_mode = "WV"`)

One row per reference point and WV imagette, from the OCN WV matchups (`<day>/ocn/wv`); the
`primary_key` is built on the SLC twin of the OCN SAFE, found in the archive
(`<SLC SAFE>:WV_<NNN>_<lon>_<lat>`). SWOT catalogues are named after the release of the matchups,
`PODAAC-SWOT-KARIN-L2-WINDWAVE-D0` / `-D2`.

### IW (`sar_mode = "IW"`)

One row per reference point and IW **GRD** scene, from the GRD matchups (`<day>/grd/iw`); the
`primary_key` is the GRD SAFE name + `ref_lon` + `ref_lat` (spec 1.5.0).

- The OCN and SLC twins of each GRD (one acquisition, slice times a second or so apart) come from
  the CDSE daily metadata Parquets through cdsodatacli (>= 2026.11.27, `find_products_in_parquet`:
  same platform, orbit, datatake and polarization, closest start within 8 s), when
  `sar_parquet_root` is set; otherwise their columns are empty.
- `[archives]` lists where SAFEs are looked for (`ocn`: the provider L2 tree; `l1`: MPC-layout
  roots, in order); a SAFE found nowhere gives an empty `sar_path_*`.
- SWOT parameters: `swh_swot`, `windspeed_swot` (`wind_speed_karin`), `windspeed_2_swot`
  (`wind_speed_karin_2`); `ref_flag` is `swh_karin_qual`; the other KaRIn flags, the distances
  to the footprint edges and `ref_distance_to_coast` (NASA GSFC grid) are kept.
- File names carry the SWOT release (`-D0` / `-D2`) and the GRD polarization (`SV`, `SH`, `DV`,
  `DH`; a partial dual-polarization `VV` / `HH` product is filed under `SV` / `SH`).

### Ancillary sources

Listed under `[ancillary]` (source = data root); a listed path must exist. A value that cannot be
read (no file, land, outside the grid) stays empty and is counted in the run summary. In IW
catalogues, the spec ancillary columns whose source is not given are written empty and listed in
the `ancillary columns not filled` global attribute.

| Key | Columns | Source | Rule | Modes |
|---|---|---|---|---|
| `tengeop` | `class_1`, `pos_1` | CLS TenGeoP SAR surface roughness classification | class of the WV imagette | WV |
| `ecmwf` | `ECMWF_Wind_Speed`, `ECMWF_Wind_Direction` | ECMWF operational forecast 10 m wind, 0.1°, hourly | nearest hour, nearest cell; direction "to" | WV, IW |
| `era5` | `ERA5_Wind_Speed`, `ERA5_Wind_Direction` | ERA5 reanalysis 10 m wind, 0.25°, hourly | nearest hour, nearest cell; direction "to" | WV, IW |
| `ww3` | `WW3_Hs`, `WW3_Tp` | WAVEWATCH III CCI sea state run GLOB-30M, 0.5°, hourly | nearest hour, nearest cell; `Tp = 1 / fp` | WV, IW |
| `gebco` | `bathymetry` | GEBCO grid, 15 arc-second | `-elevation` at the nearest cell (negative on land) | WV, IW |
| `seaice` | `sea_ice_concentration` | Bremen ASI v5.4 AMSR2, 6.25 km, daily | day and hemisphere file, nearest cell (fraction 0–1) | WV, IW |
| `imerg` | `imerg_rainrate` | IMERG Late V07B, 0.1°, half-hourly | half-hour file containing `ref_time`, nearest cell (mm/h) | WV, IW |

The co-aligned catalogue report (`soba_catalogue_report`) documents the raw catalogues with the
same figures and template as the curated reports.

## Curated datasets

### WV altimeter (`soba_curate_dataset`)

Copy [examples/alti-curation.toml](examples/alti-curation.toml) to `runs/`, update the
timestamped CCI / CMEMS catalogue paths and run settings, choose a new output directory, and run:

```bash
soba_curate_dataset --recipe runs/alti-recipe.toml
```

Keep one input per satellite and product (CCI and CMEMS may share a satellite). Only recipe
filters apply; the example keeps `imerg_rainrate == 0`. The outputs are one Curated Parquet per
input and a manifest (no TEST / TARGET).

For a fuller reference-quality screen (altimeter vs WW3 agreement, mission / date blackouts such
as CFOSAT pre-2024 or the HY2B 2023-02-19 frozen-value event), copy
[examples/wv-alti-curation-v2.0.toml](examples/wv-alti-curation-v2.0.toml). It uses two clause
types added for this use case:

```toml
# reject when |col - ref| exceeds `tolerance` *as a fraction* of `ref` (> 0)
clauses = [["ref_hs_alti_closest", "rel_diff_le", ["ww3_swh", 0.30]]]

# reject rows whose `fname` matches any reference-name pattern while the
# row time (e.g. `sar_time`) is in the half-open window [from, to)
clauses = [["fname", "not_in_window_for",
            ["sar_time", ["SEASTATE-L2P-SWH-CFOSAT-", "global_vavh_l3_rt_cfo_"],
             "2019-01-01", "2024-01-01"]]]
```

The `not_in_window_for` patterns match the reference filename that identifies the altimeter
(CCI `SEASTATE-L2P-SWH-<sat>-…` and CMEMS ALTI-L3-VAVH `global_vavh_l3_rt_<token>_…` names), so a
single rule covers both databases.

### Curated report (PDF)

Set `report = true` in the recipe (and `compile = true` for a PDF) and re-run the same command,
or rebuild the report of an already-curated run without curating again:

```bash
soba_curate_dataset --run-dir path/to/curated-run [--no-compile]
```

The report is one document describing every Curated Parquet of the run; when a satellite has
both a CCI and a CMEMS catalogue, each gets its own labelled column. The reference-distribution
figure uses `ref_hs_alti_closest` (altimeter SWH). A PDF is compiled with `pdflatex`, falling
back to `tectonic`. The same shared template drives the SWOT and HSCAT Curated documents.
`--no-compile` also applies with `--recipe`. See [report details](docs/usage.rst).

### Interactive HTML producer report

The PDF is the dataset's business card; the HTML page is for the producers, to check that the
curation choices are relevant and that the columns hold what is expected. Add `--html` to
`soba_curate_dataset` or `soba_catalogue_report` (or run `soba_html_report --run-dir
path/to/run`) to write `report/<document>_<version>.html`, rendered with Plotly.js (pinned CDN
build, so the page needs internet; only aggregates are embedded):

* sub-datasets (SAR unit x reference database x altimeter for WV-alti) with their row counts and
  time span;
* histogram, monthly counts and 2-degree density map of the reference variable (WV-alti
  `hs_alti_mean` then `ref_hs_alti_closest`, WV-SWOT KaRIn Hs, WV-HSCAT wind speed / direction),
  per sub-dataset;
* curated runs: the filter cascade replayed on the source catalogues (rows removed per rule and
  catalogue) and the same figures for the rows removed by any rule, compared with the kept rows;
* `--previous-run <run of version N-1>`: rules added / removed / changed and the row counts
  N-1 vs N;
* column conformity: type, missing fraction, range / distinct values and warnings (all missing,
  constant, latitude / longitude out of range, `UNKNOWN` strings).

### Versions

Curated filenames retain the source catalogue coverage dates, polarization and reference product;
the production date and the `X.Y` version come from the recipe:

- `X` (major) bumps when the curated **content** changes in a way visible to users, e.g. adding,
  removing or tightening curation rules so that different rows are kept or dropped;
- `Y` (minor) bumps only when the build script changes but the resulting Parquet content is
  unchanged.

Example: adding the `alti_ww3_30pct`, `cfosat_pre2024` and `hy2b_frozen_20230219` rules to the
v1.0 "no_rain" curation drops rows, so the output is v2.0 (not v1.1).

## TEST and TARGET datasets

`soba_create_test_dataset` curates SWOT or HSCAT WV catalogues and writes the TEST / TARGET pair
and its report; the recipe selects the reference.

### SWOT

1. Copy the example; keep your edited recipe in `runs/` so it stays out of Git:

   ```bash
   mkdir -p runs
   cp examples/swot-curation.toml runs/recipe.toml
   ```

2. Edit `runs/recipe.toml`: replace the S1A, S1C and S1D `path` values with your WV SWOT Parquet
   files. Check `production_date`, `version` and `output_dir` (it **must be new**). Set
   `compile = true` if you have `pdflatex` or Tectonic and want a PDF; otherwise the editable
   LaTeX report is written without compiling it.

3. Run:

   ```bash
   soba_create_test_dataset --recipe runs/recipe.toml
   ```

The example writes to `runs/swot-WV-20260930-0.1/`:

```text
recipe.toml                 Copy of the run settings
curated/                    One Curated Parquet per satellite
datasets/                   Paired TEST and TARGET Parquets
report/                     TeX, optional PDF, and images_<report-stem>/ assets
manifest.json               Source files, rules, counts, and output paths
```

Each satellite starts with 13 predefined SWOT filters. A `[[catalogues.rules]]` entry changes a
default filter when its `id` matches a default rule; a new `id` adds a filter after the defaults.
The example includes commented overrides and additions. Filters run in order: report counts read
`remaining / removed` after each filter. Curated counts can exceed TEST counts because the
exporter also checks required fields and duplicate keys. A satellite can contribute zero rows if
no input row passes its filters. Recipe rules support UTC `date_gt` and `date_lt` comparisons:
they select timestamps strictly later or earlier than midnight UTC on the stated date; invalid or
missing timestamps do not match.

Reports describe Curated observations, keep sequential filter counts, and document the generated
observation schema without assigning a final split. TeX and image-folder names stay versionless;
only PDFs carry the recipe version.

### HSCAT

Copy [examples/hscat-curation.toml](examples/hscat-curation.toml) to `runs/`, update the paths,
and choose `windspeed` or `winddirection` (the files are named `wind-speed` / `wind-direction`, as
in the spec). Wind-speed runs apply the ECMWF / HSCAT speed-difference filter; SCAT rule clauses
also support `abs_diff_le` for absolute differences between two columns. The example rejects
imagettes whose eligible SCAT matchup directions have a circular STD above 10°; keep this
configurable rule last. See [usage details](docs/usage.rst).

### TARGET only

`soba_create_target_dataset --test-dir <dir> --out-dir <dir> [--only PATTERN]` writes the TARGET
of already published TEST files.

## CHALLENGER

Copy [examples/cci-seastate-challenger.toml](examples/cci-seastate-challenger.toml), set its paths
and run settings, then run:

```bash
soba_produce_challenger --recipe path/to/challenger.toml
```

The output follows the spec naming convention,
`S1_challenger_<challenger-name>_WV_<productiondate>_<version>.parquet`: `version` is `v1`, `v2`,
... (incremental) and `challenger_name` (optional, default `ESACCI-SEASTATE`) names the team or
model, without `_`. Each TEST row is matched at its reference point.

## Validation

### TEST, TARGET and CHALLENGER

One file at a time:

```bash
soba_validate_parquets --type test --file path/to/SWOT_TEST.parquet
soba_validate_parquets --type target --file path/to/SWOT_TARGET.parquet
soba_validate_parquets --type test --file path/to/SCAT_TEST.parquet --reference scat
soba_validate_parquets --type target --file path/to/SCAT_TARGET.parquet --reference scat
soba_validate_parquets --type challenger --file path/to/CHALLENGER.parquet
```

`--reference swot` is the default; use `--reference scat` for SCAT TEST / TARGET files. The report
shows the filename, row and column counts, a `[PASS]` checklist and a `Schema checked:` line with
the checked column names and accepted types; skipped checks and single-file limitations are named.
A failed validation reports the error without a success checklist. Single-file validation does not
check the alignment with another file (the exporter checks TEST / TARGET alignment before saving).

### Co-aligned catalogues and curated datasets

`--type catalogue` and `--type curated` check any reference (altimeter, SWOT, HSCAT / ASCAT) and
accept several files:

```bash
soba_validate_parquets --type catalogue --file /path/to/catalogue/s1-wv/*_2.0.parquet
soba_validate_parquets --type curated --file /path/to/curated/S1?/*_5.0.parquet
```

Each check is reported `[PASS]`, `[WARN]` or `[FAIL]`; the command exits 1 when any file has a
`[FAIL]`.

The checks follow the parquet format specification (`src/soba_reference_repo/spec.py`, spec
1.7.0); the last two rows are SOBA production checks outside the spec.

| Check | catalogue | curated |
|---|---|---|
| Filename convention (`S1X_<kind>_<mode>_<start>_<stop>_<production>_<pol>_<product>_<X.Y>`, dates `YYYYMMDD`) | FAIL | FAIL |
| `<product>` in the spec list (SWOT: `PODAAC-SWOT-KARIN-L2-WINDWAVE-<release>`) | FAIL | FAIL |
| Parquet readable, at least one row | FAIL | FAIL |
| Global attributes: `source <ref>`, `source ancillary datasets`, `library used to produce the parquet`, `library version`, `creation date`; curated: `source_catalogue_dataset` | FAIL | FAIL |
| Mandatory columns of the mode's spec table (WV; IW, also used for EW / SM); `ref_param` columns are named per dataset and not checked; `sar_lat`/`sar_lon` optional | FAIL | FAIL |
| `sar_time` non-null | FAIL | FAIL |
| Filename dates vs data | FAIL if data outside, WARN if name wider | FAIL unless first/last kept SAR dates |
| SAR positions (`\|sar_lat\| <= 90`, `-180 <= sar_lon <= 180`), only in older files that still have the optional `sar_lat`/`sar_lon` | WARN | FAIL |
| SAFE identities (missing / `UNKNOWN`) | WARN | WARN |
| Unit of the key SAFE (WV: SLC, IW: GRD) matches the filename unit | FAIL | FAIL |
| Primary keys non-null and unique | FAIL | FAIL |
| Primary key `ref_lon` / `ref_lat` with at least 2 decimals (every reference, SWOT included) | FAIL | FAIL |
| Curation provenance (`_curation_mission` = unit, `_curation_row` >= 0, unique); WARN when the columns are absent | – | FAIL |
| `dataset_version` / `production_date` metadata match the filename | – | FAIL (WARN if absent) |

## Development

```bash
python -m pip install -e '.[challenger]' pytest pytest-cov pre-commit
python -m pytest -q --cov=soba_reference_repo     # tests and coverage
pre-commit run --all-files                         # ruff and file checks (git add new files first)
```

CI runs both on every push and pull request; on `main` it also updates the coverage and test-count
badges (`badges` branch, read by shields.io). The SOBA report style and logo live in
`assets/latex/`.

Code layout of the co-aligned catalogues: `coalign/catalogue.py` is shared by every mode
(streaming writer, file names, global attributes, ancillary sources); `coalign/wv.py` and
`coalign/iw.py` hold the mode-specific rows, columns and checks; `coalign/ancillary/` one reader
per source; `rows.py` / `sar.py` / `stac.py` the shared value, SAR and matchup helpers.

Licensed under [MIT](LICENSE).
