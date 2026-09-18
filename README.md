# soba_reference_repo

[![CI](https://github.com/umr-lops/soba_reference_repo/actions/workflows/ci.yml/badge.svg)](https://github.com/umr-lops/soba_reference_repo/actions/workflows/ci.yml)
[![Build](https://github.com/umr-lops/soba_reference_repo/actions/workflows/build.yml/badge.svg)](https://github.com/umr-lops/soba_reference_repo/actions/workflows/build.yml)
[![Python](https://img.shields.io/badge/python-3.11-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![pre-commit](https://img.shields.io/badge/pre--commit-enabled-brightgreen?logo=pre-commit)](https://github.com/pre-commit/pre-commit)

Generates a SOBA **reference TEST dataset** report from two co-aligned catalogues: it crosses a
scatterometer catalogue and the SWOT KaRIn catalogue through the common Sentinel-1 Wave Mode
imagettes, applies the collocation filters, draws three figures, fills the SOBA LaTeX template
with those figures and the run's real metadata, compiles a PDF, and exports the
spec-conformant WV TEST parquet under the SOBA dataset naming convention.

Reference: *Format Description for parquet co-aligned datasets* (SOBA WP3, v1.1.0).

Documentation sources live in `docs/` — `make -C docs html` builds the site.

The bundled `assets/latex/template.tex` is the SOBA template **rewritten for reference TEST datasets**

## Install

Install into an environment that already has the SOBA stack (numpy, pandas, pyarrow,
matplotlib, geopandas):

```bash
cd soba_reference_repo
python -m pip install -e . --no-deps
```

`--no-deps` because that environment already satisfies every dependency. Python 3.11 or newer.

## Run

```bash
soba_reference_repo \
  --satellite S1D --scatterometer ASCAT \
  --scat  <data-dir>/S1D_coaligned_catalogue_WV_20260107_20260414_20260908_SV_KNMI-ASCAT-METOP-12.5km_0.2.parquet \
  --swot  <data-dir>/S1D_coaligned_catalogue_WV_20260107_20260808_20260902_SV_PODAAC-SWOT-KARIN-L2-WINDWAVE-D0_0.1.parquet \
  --test-dir test_datasets
```

Without the install, `PYTHONPATH=src python -m soba_reference_repo.cli …` works the same way.
If `pdflatex` is not on `PATH`, point the tool at it — `--miktex-bin /path/to/miktex/bin/x64`, or
export `MIKTEX_BIN` once in your shell profile.

### Flags

| Flag | Default | Meaning |
| --- | --- | --- |
| `--scat` | **required** | scatterometer catalogue parquet |
| `--swot` | **required** | SWOT KaRIn catalogue parquet |
| `--satellite` | **required** | Sentinel-1 platform; checked against both catalogue names and the SAFE identifiers |
| `--scatterometer` | **required** | `ASCAT` or `HSCAT` |
| `--scene-key` | off | match the two catalogues on the normalised imagette key instead of their own `primary_key` |
| `--label` | `<satellite>_swot_<scatterometer>` | names the report |
| `--overlap-min-pct` | `100` | required SAR/SWOT footprint overlap |
| `--rain-max-mm-h` | `0.3` | maximum IMERG mean rain rate |
| `--time-max-min` | `120` | maximum direct scatterometer–SWOT time difference |
| `--output-dir` | `runs/<label>` | report build directory |
| `--test-dir` | `test_datasets` | directory for the TEST parquet and the run manifest |
| `--test-name` | generated | override the TEST filename entirely |
| `--dataset-version` | `0.1` | `<version>` field of the TEST filename |
| `--latex-dir` | `assets/latex` | holds `template.tex` and its companions |
| `--land-map` | `assets/ne_110m_land.geojson` | grey base map for the geography figure |
| `--miktex-bin` | `$MIKTEX_BIN`, else `PATH` | directory holding the `pdflatex` executable |
| `--compile` / `--no-compile` | `--compile` | skip LaTeX to iterate on figures fast |
| `--keep-intermediates` | off | keep the LaTeX byproducts (`.aux`, `.log`, `.out`, `.toc`) |
| `--challenger` | off | also write the CHALLENGER dataset beside the TEST parquet |
| `--validate` | off | run the SOBA parquet validator on the TEST file (and the CHALLENGER, if written); exit 1 if either fails |
| `--validator` | bundled copy | path to a validator module to use instead of that copy |

## Outputs

**Report directory (`runs/<label>/`)** — after a successful compile the LaTeX byproducts are
deleted, and everything the document was built from is kept so the report can be hand-edited
and recompiled:

```
<test dataset file name>.pdf    the report, named after the parquet it documents
<test dataset file name>.tex    filled template, editable
images_<dataset file name>/*.png  the three figures
soba.sty  logo_soba.png
```

Pass `--keep-intermediates` to also keep the `.aux/.log/.out/.toc`; `--no-compile` skips LaTeX
(and the cleanup) entirely.

**Deliverables directory (`--test-dir`, default `test_datasets/`):**

```
S1D_reference_test_dataset_WV_20260112_20260227_20260916_SV_KNMI-ASCAT-METOP-12.5km_PODAAC-SWOT-KARIN-L2-WINDWAVE-D0_0.1.parquet
S1D_reference_test_dataset_WV_20260112_20260227_20260916_SV_KNMI-ASCAT-METOP-12.5km_PODAAC-SWOT-KARIN-L2-WINDWAVE-D0_0.1_manifest.json
```

The manifest is named after the parquet it describes (same convention, `_manifest.json` suffix), so
a dataset file and its provenance always travel together — including when `--test-name` overrides
the generated name.

### TEST dataset filename

```
S1{A,B,C,D}_reference_test_dataset_<sarmode>_<startdate>_<stopdate>_<productiondate>_<polarization>_<refproductname1>_<refproductname2>_<version>.parquet
```

| Field | Source |
| --- | --- |
| `S1{A,B,C,D}` | `--satellite` |
| `<sarmode>` | read from the crossing's SLC SAFE identifiers (`WV`) |
| `<startdate>`, `<stopdate>` | first and last SAR starting date **in the TEST dataset itself** (YYYYMMDD) |
| `<productiondate>` | the day the file is written (YYYYMMDD) |
| `<polarization>` | read from the SAFE identifiers (`SV`, `SH`, `DV`, `DH`) |
| `<refproductname1>` | the scatterometer reference, parsed from its catalogue filename |
| `<refproductname2>` | the SWOT reference, parsed from its catalogue filename |
| `<version>` | `--dataset-version` |

Both reference product names appear because the crossing carries two references. They are
parsed from the catalogue filenames, so those must follow the co-aligned naming convention;
otherwise pass `--test-name` explicitly.

## Workflow

`main` is the trunk and stays runnable: nothing lands until `python -m pytest -q` passes.

- A change that alters behaviour gets a branch — `feat/…`, `fix/…`, `docs/…`, `chore/…` — one
  topic per branch. Trivial touch-ups (typos, docstrings, wording) go straight to `main`.
- Merges use `git merge --no-ff`, so a topic reads as one unit in the log while keeping its
  individual commits. Squash a branch that got noisy, and delete it once merged.
- Pull requests are for changes worth showing someone; solo work does not need one.
- Remote writes are explicit: pushing a branch, opening a PR, merging on GitHub or deleting a
  remote branch each get confirmed first.

## Notes

- **LaTeX toolchain.** `template.tex` is a pdfLaTeX document, and the tool finds pdflatex
  through `--miktex-bin` / `$MIKTEX_BIN` or, failing that, on `PATH` — so TeX Live on Linux or
  macOS needs no configuration. It compiles twice, from the build directory, so the table of
  contents settles. Nothing machine-specific is baked into the package.
- **Vendored template.** `assets/latex/*` are copies of the SOBA project's files, with the
  template rewritten for TEST datasets. Refresh the unmodified companions with:

  ```bash
  cp <path-to-the-SOBA-project>/{soba.sty,logo_soba.png} assets/latex/
  ```

- **Column names follow the validator.** The *Format Description* writes the path and SAFE
  columns with hyphens (`sar-path-ocn`), but the source catalogues and the consortium
  validator use underscores (`sar_path_ocn`), and the validator is the acceptance gate — so
  the export uses underscores. The reasoning sits next to `WV_MANDATORY_COLUMNS` in
  `report.py`.
- **Vendored validator.** `src/soba_reference_repo/validator.py` is the consortium validation
  gist, kept verbatim below its header apart from one marked change — the reference family, see
  above. `--validate` runs it against the file just written and exits non-zero when it fails;
  `--validator PATH` runs a newer copy from disk. Refresh by re-copying the gist and reapplying
  the marked change.
- **WV only.** The tool implements the WV TEST layout (`:WV_<imagette>`, SLC/OCN SAFE pattern,
  WV mandatory column list). The spec's IW layout (GRD paths, `:IW2`, `ref_geometry`) is not
  implemented.
- **How the two catalogues are matched.** By default the crossing pairs rows on the catalogues'
  own `primary_key` — the identifier both sides ship (SLC SAFE name, imagette number and the
  reference position) and agree on for a shared imagette. `--scene-key` matches on the normalised
  imagette key instead, which also bridges a SAFE-form or reference-point disagreement between the
  two. On the S1A/HSCAT pair the flag recovers matches the plain key cannot see (54,551 shared
  imagettes against 53,401, and 6,951 rows against 6,800 after filtering); on S1D/ASCAT both modes
  deliver the same 7 rows. The mode used is recorded in the manifest as `match_on`.
- **Reference and ancillary names.** Spec v1.1.0 names every reference column after its source —
  `scat_lon`, `scat_lat`, `scat_time`, `scat_flag`, `scat_id`, `scat_windspeed`,
  `scat_winddirection` on the scatterometer side and `swot_lon`, `swot_lat`, `swot_time`,
  `swot_flag`, `swot_waveheight` on the SWOT side — with the rain rate and the footprint overlap
  (`ecmwf_rain_rate`, `ecmwf_overlap`) in an **Ancillary** group under the second reference in the
  columns table. The SAFE columns carry the SAFE name alone, with no archive path prefix, because
  the validator's patterns anchor the collection tag immediately after the satellite prefix.
- **The bundled validator takes a reference family.** Its mandatory reference columns follow the
  source — `scat_lon`/`scat_lat`/`scat_time` and `swot_*` — instead of the retired `ref_*`.
  `--validate` passes `scat` (the reference this crossing is built on); the standalone validator
  exposes it as `--reference`. The retired `ref_lon`/`ref_lat`/`ref_time` names still satisfy
  one family's columns when the `<ref>_` form is absent, so files written before the rename
  validate too — while a second reference the file does not carry is still reported missing.
  That is a marked local deviation from the gist, which still asks for `ref_*`; the header
  of `validator.py` says so and the tests cover both families.
- **Global attributes.** `write_test_parquet` puts the five mandatory attributes into the
  parquet's own metadata under the spec's wording: `source scat`, `source ancillary datasets`,
  `library used to produce the parquet`, `library version` (the git commit when running from a
  checkout) and `creation date`.
- **Two references, one `<ref>_` family.** A SCAT+SWOT crossing carries two references but the
  spec has a single reference family. `<ref>_` is the scatterometer side (`scat_id` and the wind
  params are SCAT-native); the SWOT wave height is carried as `swot_waveheight` with its own
  coordinates.
