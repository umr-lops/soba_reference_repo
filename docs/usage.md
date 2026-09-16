# Usage notes

## Adding a collocation filter

The three filters are one expression in `run_crossing`, so adding a fourth is four small edits.
Worked example — a minimum distance-to-coast of 10 km:

1. `src/soba_catalogue_report/report.py`, `ReportConfig`: add the field.

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

3. `src/soba_catalogue_report/cli.py`: add the flag and thread it into `ReportConfig`.

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

Then `/home/il/miniforge3/envs/SOBA/bin/python -m pytest -q` — 16+ tests must pass.

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
| `! Missing $ inserted` | an unescaped `_`, `&`, `%`, `#` or `$` in injected text | escape it (`_tex_escape`) or use `\path|…|` |
| `Something's wrong--perhaps a missing \item` | a replacement inserted a nested list inside the template's own list | replace only the `\item` lines |
| `File '…' not found` | a figure or companion file missing from the build dir | `stage_latex_assets` copies four files; figures are written first |
| `I can't write on file` / UNC path error | MiKTeX rejecting the WSL working directory | build under `/mnt/c/Users/ilias/AppData/Local/Temp/…` or `/mnt/z/shared/…` and copy the PDF back |
| `pdflatex not found` | `--miktex-bin` wrong | point it at the directory holding `pdflatex.exe` |

`Overfull \hbox` warnings are cosmetic and safe to ignore — the template's own version-history
table produces them too.

## Iterating quickly

`--no-compile` skips LaTeX entirely and still writes the figures, the `.tex` and the TEST
parquet, which is the fast loop when tuning a plot. It also skips the cleanup: after a real
compile the build directory is purged down to the PDF, so use `--no-compile` or
`--keep-intermediates` whenever you need to read the generated `.tex` or the figures.

## Where each file ends up

| File | After a successful compile | With `--no-compile` / `--keep-intermediates` |
| --- | --- | --- |
| `<label>_catalogue_report.pdf` | kept | absent (`--no-compile`) |
| `<label>_catalogue_report.tex` | deleted | kept |
| `figures/*.png` | deleted | kept |
| `soba.sty`, `logo_soba.png`, `schema_dataflow.tex`, `cpcd_definition.tex` | deleted | kept |
| `.aux`, `.log`, `.out`, `.toc` | deleted | kept |
| TEST parquet + `<label>_manifest.json` | written to `--test-dir` | same |

