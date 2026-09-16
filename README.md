# soba-catalogue-report

Generates a SOBA **reference TEST dataset** report from two co-aligned catalogues: it crosses a
scatterometer catalogue and the SWOT KaRIn catalogue through the common Sentinel-1 Wave Mode
imagettes, applies the collocation filters, draws three figures, fills the SOBA LaTeX template
with those figures and the run's real metadata, compiles a PDF, and exports the
spec-conformant WV TEST parquet under the SOBA dataset naming convention.

Reference: *Format Description for parquet co-aligned datasets* (SOBA WP3, v1.0.3).

The bundled `assets/latex/template.tex` is the SOBA template **rewritten for reference TEST
datasets**: title line, scope, section wording, footer, a §1.3 *reference parameter statistics*
table (min/max/mean/median per reference variable, filled from the filtered cohort), and a §1.4
*Catalogue Columns* table (Column / Type / Description, grouped by role) mirroring the
co-aligned catalogue document's column table. The upstream co-aligned template in the SOBA
project is untouched — pass `--latex-dir /home/il/projects/SOBA` to use it instead.

## Install

The tool runs on the existing SOBA conda environment (numpy, pandas, pyarrow, matplotlib,
geopandas are already there):

```bash
cd /home/il/projects/soba-catalogue-report
/home/il/miniforge3/envs/SOBA/bin/python -m pip install -e . --no-deps
```

`--no-deps` because the SOBA env already satisfies every dependency.

## Run

```bash
/home/il/miniforge3/envs/SOBA/bin/soba-catalogue-report \
  --satellite S1D --scatterometer ASCAT \
  --scat /home/il/SOBA_datafiles/S1D_coaligned_catalogue_WV_20260107_20260414_20260908_SV_KNMI-ASCAT-METOP-12.5km_0.2.parquet \
  --swot /home/il/SOBA_datafiles/S1D_coaligned_catalogue_WV_20260107_20260808_20260902_SV_PODAAC-SWOT-KARIN-L2-WINDWAVE-D0_0.1.parquet \
  --test-dir /home/il/projects/SOBA/test_datasets
```

Without the install, `PYTHONPATH=src /home/il/miniforge3/envs/SOBA/bin/python -m soba_catalogue_report.cli …` works the same way.

### Flags

| Flag | Default | Meaning |
| --- | --- | --- |
| `--scat` | **required** | scatterometer catalogue parquet |
| `--swot` | **required** | SWOT KaRIn catalogue parquet |
| `--satellite` | **required** | Sentinel-1 platform; checked against both catalogue names and the SAFE identifiers |
| `--scatterometer` | **required** | `ASCAT` or `HSCAT` |
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
| `--miktex-bin` | Windows MiKTeX `x64` bin | directory holding `pdflatex.exe` |
| `--compile` / `--no-compile` | `--compile` | skip LaTeX to iterate on figures fast |
| `--keep-intermediates` | off | keep the LaTeX byproducts (`.aux`, `.log`, `.out`, `.toc`) |

## Outputs

**Report directory (`runs/<label>/`)** — after a successful compile the LaTeX byproducts are
deleted, and everything the document was built from is kept so the report can be hand-edited
and recompiled:

```
<label>_catalogue_report.pdf    the report
<label>_catalogue_report.tex    filled template, editable
figures/*.png                   the three figures
soba.sty  logo_soba.png  schema_dataflow.tex  cpcd_definition.tex
```

Pass `--keep-intermediates` to also keep the `.aux/.log/.out/.toc`; `--no-compile` skips LaTeX
(and the cleanup) entirely.

**Deliverables directory (`--test-dir`, default `test_datasets/`):**

```
S1D_reference_test_dataset_WV_20260112_20260227_20260916_SV_KNMI-ASCAT-METOP-12.5km_PODAAC-SWOT-KARIN-L2-WINDWAVE-D0_0.1.parquet
<label>_manifest.json
```

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

## Notes

- **LaTeX toolchain.** `template.tex` is a pdfLaTeX document. There is no LaTeX in WSL, so the
  tool calls the Windows MiKTeX binary (`pdflatex.exe`), twice, from the build directory.
  `pdflatex.exe` accepts a WSL working directory as-is. Point `--miktex-bin` elsewhere (or at a
  WSL `pdflatex`) to compile on another machine.
- **Vendored template.** `assets/latex/*` are copies of the SOBA project's files, with the
  template rewritten for TEST datasets. Refresh the unmodified companions with:

  ```bash
  cp /home/il/projects/SOBA/{soba.sty,logo_soba.png,schema_dataflow.tex,cpcd_definition.tex} assets/latex/
  ```

- **Two generated tables.** §1.3's statistics table is built at run time from the filtered
  cohort (rounding set by `STATS_DECIMALS` in `report.py`, default 2 decimals); §1.4's column
  table is written in the template and a test fails if it stops documenting every exported
  column.
- **Version-history rows.** The run appends its own row to the template's version table,
  anchored on the table environment rather than on a specific row — so hand-editing the
  template's version rows never breaks the build.
- **WV only.** The tool implements the WV TEST layout (`:WV_<imagette>`, SLC/OCN SAFE pattern,
  WV mandatory column list). The spec's IW layout (GRD paths, `:IW2`, `ref_geometry`) is not
  implemented.
- **Two references, one `ref_*` family.** A SCAT+SWOT crossing carries two references but the
  spec has a single `ref_*` set. `ref_*` is the scatterometer wind side (`ref_id` and the wind
  params are SCAT-native); the SWOT wave height is carried as `waveheight_swot` with its own
  coordinates.
- See `docs/usage.md` for how to add a filter and how to troubleshoot a failed compile.
