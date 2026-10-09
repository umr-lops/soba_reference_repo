Install and run
===============

Install the package in a Python 3.11+ environment::

   python -m pip install -e .

Create a reference pair
-----------------------

Copy ``examples/swot-curation.toml`` to a local recipe. Set ``reference = "swot"``,
point each ``[[catalogues]]`` path to a WV co-aligned Parquet, and choose a new
``output_dir``. Paths in the recipe are resolved relative to that file. Then run::

   soba_create_test_dataset --recipe path/to/recipe.toml

The recipe selects SWOT WV or HSCAT. ALTI is not supported. ``soba_reference_repo`` remains an alias for the same command. The recipe client module is ``soba_reference_repo.create_test_dataset``.

The recipe supports any nonempty subset of S1A through S1D. Dates use ``YYYYMMDD``
and versions use ``X.Y``. The output directory must not already exist. The tool
saves one Curated Parquet per satellite and writes aligned TEST and TARGET Parquets.
It uses a temporary merge for pairing but does not persist a merged Curated file.
The run's ``report/`` directory holds the editable TeX, optional PDF, figures, and
template assets directly. Set ``compile = true`` to request a PDF if pdflatex or
Tectonic is installed.

Each SWOT catalogue starts with 13 ordered quality filters. Change a default
filter under its catalogue with ``[[catalogues.rules]]`` and the default's ``id``;
set ``enabled = false`` to skip it. A new ``id`` with ``name`` and ``clauses``
appends a rule. Operators are ``eq``, ``gt``, ``ge``, ``lt``, ``le``, ``in``,
``present``, ``date_gt``, ``date_lt``, and ``abs_diff_le``. Date operators parse
values as UTC: ``date_gt`` keeps timestamps strictly after midnight UTC on the
given date, while ``date_lt`` keeps timestamps strictly before that midnight.
Missing or invalid timestamps do not match. Unsupported references fail before
output files are written.

For HSCAT, copy ``examples/hscat-curation.toml`` and set each ``path`` and a new
``output_dir``. Choose one ``reference_variable`` per run: ``windspeed`` or
``winddirection``. Wind-speed runs apply only
``abs(ecmwf_wind_speed - ref_param_2) <= 2 m/s``. No rain filter is built in;
other filters can be added in the recipe once their definitions are settled. Rows
with duplicate ``primary_key`` values after curation are excluded as a group from
both outputs; the manifest and report record those integrity exclusions separately
from filter counts. HSCAT and SWOT TEST/TARGET filenames share the compact pattern
``S1_reference_test_dataset_WV_<production-date>_<variable>_<version>.parquet`` and
``S1_target_dataset_WV_<production-date>_<variable>_<version>.parquet``. The report
``.tex`` and optional PDF use the TEST filename stem. Source coverage, polarization,
and reference-product details remain in the run manifest.
Reference-distribution plots show each mission as a distinct step line, normalized
to percentages on shared bins, with sample counts in a centered legend.
Wind-direction bins span 0–360 degrees.

Validate Parquets
-----------------

Validate one TEST, TARGET, or CHALLENGER file at a time::

   soba_validate_parquets --type test --file path/to/SWOT_TEST.parquet
   soba_validate_parquets --type target --file path/to/SWOT_TARGET.parquet
   soba_validate_parquets --type test --file path/to/SCAT_TEST.parquet --reference scat
   soba_validate_parquets --type target --file path/to/SCAT_TARGET.parquet --reference scat
   soba_validate_parquets --type challenger --file path/to/CHALLENGER.parquet

The default output shows the file path, row and column counts, and a ``[PASS]``
checklist after validation succeeds. Notes identify single-file limitations. Invalid
files produce an error without a success checklist.

TEST and TARGET checks cover their exported schemas, metadata, and individual keys.
CHALLENGER checks cover the ``primary_key`` and nullable float64 ``swh`` contract;
missing predictions are allowed, but non-null predictions must be finite. Single-file
validation does not establish alignment with another file or prove prediction coverage.
Each report includes a ``Schema checked:`` line with checked column names and
accepted types. These product validators require exact column names and order; the
exporter retains its internal TEST/TARGET alignment check after writing its outputs.

Both ``--type`` and ``--file`` are required. ``--reference`` defaults to ``swot``;
use ``--reference scat`` for SCAT TEST/TARGET files. A successful validation exits
0, invalid data exits 1, and invalid command arguments exit 2.

Create a CHALLENGER
-------------------

Copy ``examples/cci-seastate-challenger.toml``, set its paths and run
settings, then run::

   soba_produce_challenger --recipe path/to/challenger.toml

Use the run's ``audit.json`` to investigate missing predictions. Passing a
single-file CHALLENGER check does not establish a match to a particular TEST file.
