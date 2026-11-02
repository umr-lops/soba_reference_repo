# AGENTS.md

Guidance for AI agents (and humans) working in `soba_reference_repo`.

## Project

Recipe-driven SOBA **reference TEST/TARGET dataset exporter** (Python package
`soba_reference_repo`, hatchling build). It cures coaligned Sentinel-1 WV
catalogues (SWOT / HSCAT / altimeter) and builds the accompanying Curated
dataset **documents** (figures + LaTeX + PDF) that ship to
`/scale/project/lops-siam-soba-public/documents/dataset_description/`.

## Python environment

- **Always** use the interpreter:
  `/home/agrouaze/micromamba/envs/dev-agent/bin/python` (do not use system
  `python`).
- Install/update editable: `pip install -e .`
- Keep scratch inputs/outputs in `/scratch/agrouaze/opencode_sandbox/tmp_data/`.

## Entry points (`[project.scripts]`)

- `soba_reference_repo` — TEST/TARGET pair + report; **dispatches on
  `reference`**: `"scat"` -> HSCAT exporter, else SWOT exporter.
- `soba_curate_dataset` — curation-only, for `reference = "altimeter"`; also
  supports `--run-dir` (rebuild the report from an existing curated run) and
  `--no-compile` (skip the PDF).

## Build recipes are NOT versioned — they live locally

The **production build recipes** are *not* committed to this repository. They are
kept **locally** (they hardcode machine-specific `/scale` source paths and
`/scratch` output paths) under the airflow configs:

```
/scale/project/lops-siam-airflow/configs_exploit/soba-reference-repo/
  s1-wv-alti-curation-recipe_2.0.toml   # reference = "altimeter" (curation + report)
  s1-wv-swot-curation-recipe_1.0.toml    # reference = "swot" (TEST/TARGET + report)
  s1-wv-hscat-curation-recipe_1.0.toml   # reference = "scat" (TEST/TARGET + report)
```

They are **not** in version control (only the `examples/` recipes in this repo are).
`scripts/recipes/*.toml` (local, untracked) are the working copies. Do not commit
these production recipes or their machine-specific paths.

## Testing & quality checks

- Tests: `/home/agrouaze/micromamba/envs/dev-agent/bin/python -m pytest`
  (`testpaths = ["tests"]`, `pythonpath = ["src"]`).
- Lint: `ruff check .` (config in `pyproject.toml`; line-length 100).
- Type: `mypy` is run ad-hoc and compared against the `main` baseline
  (pre-existing pyarrow stub errors are expected, not new).
- **Always** run the test suite + ruff after code changes and fix failures.

## Git workflow

- Private repo; the LLM-tagged branch is the working branch for a PR.
- Do not commit secrets or machine-specific paths; keep commits small.
