# Usage notes

## Adding a collocation filter

The three filters are one expression in `run_crossing`, so adding a fourth is four small edits.
Worked example — a minimum distance-to-coast of 10 km:

1. `src/soba_reference_repo/report.py`, `ReportConfig`: add the field.

   ```python
   @dataclass(frozen=True)
   class ReportConfig:
       overlap_min_pct: float = 100.0
       rain_max_mm_h: float = 0.3
       time_max_min: float = 120.0
       min_coast_km: float = 10.0
   ```

2. Same file, `run_crossing`: add the predicate to the `filtered = matches[…]` expression.

   ```python
   filtered = matches[
       (matches["overlap_pct"] == config.overlap_min_pct)
       & (matches["mean_rainrate_IMERG"] < config.rain_max_mm_h)
       & (matches["scat_swot_time_delta_min"] < config.time_max_min)
       & (matches["sar_distance_to_coast_swot"] >= config.min_coast_km)
   ].copy()
   ```

3. `src/soba_reference_repo/cli.py`: add the flag and thread it into `ReportConfig`.

   ```python
   parser.add_argument("--min-coast-km", type=float, default=10.0)
   ...

   config = ReportConfig(
       overlap_min_pct=args.overlap_min_pct,
       rain_max_mm_h=args.rain_max_mm_h,
       time_max_min=args.time_max_min,
       min_coast_km=args.min_coast_km,
   )
   ```

4. `tests/test_report.py`: extend `test_run_crossing_drops_a_row_that_fails_one_filter` with the
   new case, and give `_write_pair` a knob for the column you are filtering on.

5. Mention the new value in `build_report_tex`: add it to the `filters:` block of the generated
   `config.yaml` listing, and to the relevant criteria sentence, so the PDF matches the run.

Then `python -m pytest -q` — the whole suite must pass.

## Why the anchors are strict

`build_report_tex` replaces a list of `(anchor, replacement)` pairs and calls `_replace_once`
for each one, which raises when an anchor does not match **exactly once**. This is deliberate:
`\hl{to be filled}` appears four times in `template.tex` with four different meanings, and a
naive global substitution would silently produce a wrong document. If you edit the template,
expect to update the anchor list — and let the failure tell you where.

Two conventions the anchors depend on: `template.tex` has doubled spaces after
`co-location criteria:}` and after `1.0.2 &`. Copy them exactly.

## LaTeX specials

Any value injected into the document must survive text mode. `_tex_escape` handles the
general case; long catalogue filenames use the `path` package's `\path|…|` (already loaded by
`soba.sty`) so they can break across lines. Do not put a raw filename into `\texttt` — the
underscores abort the compile with `! Missing $ inserted`.

## Troubleshooting a failed compile

`compile_pdf` raises `RuntimeError` with the tail of the `.log`, so the failing line is in the
message. Recurring causes:

| Symptom | Cause | Fix |
| --- | --- | --- |
| `! Missing $ inserted` | an unescaped `_`, `&`, `%`, `#` or `$` in injected text | escape it (`\_`, `\%`, …) or wrap file names in `\path|…|` |
| `Something's wrong--perhaps a missing \item` | a replacement inserted a nested list inside the template's own list | replace only the `\item` lines |
| `File '…' not found` | a figure or companion file missing from the build dir | `stage_latex_assets` copies four files; figures are written first |
| `I can't write on file` / UNC path error | a Windows pdflatex rejecting a WSL working directory | build under a Windows-visible temp directory (`/mnt/c/…`) and copy the PDF back |
| `no pdflatex in …` / `pdflatex is not on PATH` | `--miktex-bin` wrong, or no TeX install | point `--miktex-bin` at the directory holding `pdflatex`/`pdflatex.exe`, or install TeX Live so it is on `PATH` |

`Overfull \hbox` warnings are cosmetic and safe to ignore — the template's own version-history
table produces them too.

## Iterating quickly

`--no-compile` skips LaTeX entirely and still writes the figures, the `.tex` and the TEST
parquet, which is the fast loop when tuning a plot. It also skips the byproduct cleanup.

## Generated LaTeX

Three things in the report are built at run time rather than written in the template:

| Element | Anchor in `template.tex` | Source |
| --- | --- | --- |
| Figure images | the three `\fbox{...\textit{[Insert … Here]}...}` placeholders | the run's figures |
| §1.3 reference statistics table | `\hl{reference parameter statistics}` | min/max/mean/median of the filtered cohort, `STATS_DECIMALS` decimals |
| Version-history row | `\caption{Versioning of the documentation}` | run label, row count, date |
| Dataset-file row | `\caption{Versioning of test catalogue files}` | produced parquet name, date, filters applied |

Table rows are appended before the table's `\bottomrule`; the table is located through its
caption, so you can add, reorder or delete the template's own rows freely.

The §1.4 catalogue-columns table is *not* generated: it is written in the template, and
`test_the_template_column_table_documents_every_exported_column` fails if it stops listing a
column the tool exports. `reference_statistics_table` has its own test asserting the four
statistics per reference variable.

## Matching the two catalogues

The crossing pairs the scatterometer and SWOT rows on the catalogues' own `primary_key` by default
— the identifier both sides ship (SLC SAFE name, imagette number, reference position) and agree on
for a shared imagette. `--scene-key` matches on the normalised imagette key instead, which also
bridges a SAFE-form or reference-point disagreement between the two catalogues. Whichever mode
runs is recorded in the manifest's `match_on`.

## Validating the output

`--validate` runs the consortium SOBA validator against the parquet the run has just written and
prints its report; the process exits `1` when the file fails, so it works as a gate in a script:

```bash
soba_reference_repo --scat … --swot … --satellite S1D --scatterometer ASCAT --validate
```

Warnings do not fail the file — only errors do. `src/soba_reference_repo/validator.py` is the
consortium gist vendored verbatim below its provenance header; `--validator /path/to/newer.py`
runs an updated copy without touching the package. To refresh the bundled one, re-copy the gist
and keep the diff to that header only.

## Where each file ends up

After a successful compile the document sources stay next to the PDF so it can be hand-edited
and recompiled (`cd runs/<label> && pdflatex "<test dataset file name>.tex"`, or
`"$MIKTEX_BIN/pdflatex.exe"` on Windows):

| File | After a successful compile | With `--no-compile` / `--keep-intermediates` |
| --- | --- | --- |
| `<test dataset file name>.pdf` | kept | absent (`--no-compile`) |
| `<test dataset file name>.tex` | kept | kept |
| `images_<dataset file name>/*.png` | kept | kept |
| `soba.sty`, `logo_soba.png`, `schema_dataflow.tex`, `cpcd_definition.tex` | kept | kept |
| `.aux`, `.log`, `.out`, `.toc` | deleted | kept |
| TEST parquet + `<label>_manifest.json` | written to `--test-dir` | same |

