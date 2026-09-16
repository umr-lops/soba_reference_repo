# soba-catalogue-report

Generates a SOBA dataset report from two co-aligned catalogues: it crosses a scatterometer
catalogue and the SWOT KaRIn catalogue through the common Sentinel-1 Wave Mode imagettes,
applies the collocation filters, draws three figures, fills the SOBA LaTeX template with
those figures and the run's real metadata, compiles a PDF, and exports the
spec-conformant WV **TEST** parquet.

Reference: *Format Description for parquet co-aligned datasets* (SOBA WP3, v1.0.3).

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
  --scat /home/il/SOBA_datafiles/S1D_coaligned_catalogue_WV_20260107_20260414_20260908_SV_KNMI-ASCAT-METOP-12.5km_0.2.parquet \
  --swot /home/il/SOBA_datafiles/S1D_coaligned_catalogue_WV_20260107_20260808_20260902_SV_PODAAC-SWOT-KARIN-L2-WINDWAVE-D0_0.1.parquet \
  --test-dir /home/il/projects/SOBA/test_datasets
```

Without the install, `PYTHONPATH=src /home/il/miniforge3/envs/SOBA/bin/python -m soba_catalogue_report.cli …` works the same way.

### Flags

| Flag | Default | Meaning |
| --- | --- | --- |
| `--scat` | required | scatterometer catalogue parquet |
| `--swot` | required | SWOT KaRIn catalogue parquet |
| `--satellite` | `S1D` | Sentinel-1 platform (also the SAFE-prefix check) |
| `--scatterometer` | `ASCAT` | `ASCAT` or `HSCAT` |
| `--label` | `<satellite>_swot_<scatterometer>` | names the outputs |
| `--overlap-min-pct` | `100` | required SAR/SWOT footprint overlap |
| `--rain-max-mm-h` | `0.3` | maximum IMERG mean rain rate |
| `--time-max-min` | `120` | maximum direct scatterometer–SWOT time difference |
| `--output-dir` | `runs/<label>` | report build directory |
| `--test-dir` | `--output-dir` | where the TEST parquet is written |
| `--test-name` | `<label>_test.parquet` | TEST parquet filename |
| `--latex-dir` | `assets/latex` | holds `template.tex` and its companions |
| `--land-map` | `assets/ne_110m_land.geojson` | grey base map for the geography figure |
| `--miktex-bin` | Windows MiKTeX `x64` bin | directory holding `pdflatex.exe` |
| `--compile` / `--no-compile` | `--compile` | skip LaTeX to iterate on figures fast |

## Outputs (`runs/<label>/`)

```
<label>_catalogue_report.tex   filled template
<label>_catalogue_report.pdf   compiled report (9 pages)
<label>_test.parquet           WV TEST dataset, mandatory columns verified before writing
manifest.json                  inputs, thresholds, counts, output paths
figures/                       geographical_distribution.png
                               monthly_distribution.png
                               reference_distributions.png
soba.sty logo_soba.png schema_dataflow.tex cpcd_definition.tex
```

## Notes

- **LaTeX toolchain.** `template.tex` is a pdfLaTeX document. There is no LaTeX in WSL, so
  the tool calls the Windows MiKTeX binary (`pdflatex.exe`), twice, from the build directory.
  `pdflatex.exe` accepts a WSL working directory as-is. Point `--miktex-bin` elsewhere (or at
  a WSL `pdflatex`) to compile on another machine.
- **Vendored template.** `assets/latex/*` are copies of the SOBA project's files. Refresh them
  when the originals change, or run with `--latex-dir /home/il/projects/SOBA` to use the live ones:

  ```bash
  cp /home/il/projects/SOBA/{template.tex,soba.sty,logo_soba.png,schema_dataflow.tex,cpcd_definition.tex} assets/latex/
  ```

- **WV only.** The tool implements the WV TEST layout (`:WV_<imagette>`, SLC/OCN SAFE pattern,
  WV mandatory column list). The spec's IW layout (GRD paths, `:IW2`, `ref_geometry`) is not
  implemented.
- **Two references, one `ref_*` family.** A SCAT+SWOT crossing carries two references but the
  spec has a single `ref_*` set. `ref_*` is the scatterometer wind side (`ref_id` and the wind
  params are SCAT-native); the SWOT wave height is carried as `waveheight_swot` with its own
  coordinates.
- See `docs/usage.md` for how to add a filter and how to troubleshoot a failed compile.
