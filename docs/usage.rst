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

The recipe selects the reference product. Only SWOT WV is currently supported;
SCAT and ALTI recipes are not implemented yet. ``soba_reference_repo`` remains
an alias for the same command. The recipe client module is
``soba_reference_repo.create_test_dataset``.

The recipe supports any nonempty subset of S1A through S1D. Dates use ``YYYYMMDD``
and versions use ``X.Y``. The output directory must not already exist. The tool
saves one Curated Parquet per satellite, merges them, and writes aligned TEST and
TARGET Parquets, figures, an editable TeX report, and a manifest. Set
``compile = true`` to request a PDF if pdflatex or Tectonic is installed.

Each SWOT catalogue starts with 13 ordered quality filters. Change a default
filter under its catalogue with ``[[catalogues.rules]]`` and the default's ``id``;
set ``enabled = false`` to skip it. A new ``id`` with ``name`` and ``clauses``
appends a rule. Operators are ``eq``, ``gt``, ``ge``, ``lt``, ``le``, ``in``,
``present``, and ``date_gt``. Unsupported references fail before output files
are written.

Validate Parquets
-----------------

Validate a single file by its declared type::

   soba_validate_parquets --type catalogue --file path/to/catalogue.parquet
   soba_validate_parquets --type curated --file path/to/curated.parquet
   soba_validate_parquets --type test --file path/to/TEST.parquet
   soba_validate_parquets --type target --file path/to/TARGET.parquet
   soba_validate_parquets --type challenger --file path/to/CHALLENGER.parquet

The default output shows the file path, row and column counts, and a ``[PASS]``
checklist after validation succeeds. Notes identify skipped checks and single-file
limitations. Invalid files produce an error without a success checklist.

Validation reads files without modifying them. Catalogue and Curated checks
cover the current SWOT WV source contract, accept additive columns, and do not
reapply recipe quality filters. TEST and TARGET checks cover their exported
schemas, metadata, and individual keys. CHALLENGER checks cover the current
``primary_key`` and nullable float64 ``swh`` contract; missing predictions are
allowed, but non-null predictions must be finite. Single-file validation does
not establish alignment with another file or prove prediction coverage.

Each report includes a ``Schema checked:`` line with checked column names and
accepted types. Catalogue/Curated allow extra columns and unrestricted order;
TEST/TARGET/CHALLENGER require exact column names and order.

The validator accepts one file at a time; pair mode is removed. The exporter
retains its internal TEST/TARGET alignment check after writing its outputs.

Both ``--type`` and ``--file`` are required. ``--reference`` defaults
to ``swot``; other references are not yet supported. A successful validation exits
0, invalid data exits 1, and invalid command arguments exit 2.

Create a CHALLENGER
-------------------

Copy ``examples/cci-seastate-challenger.toml``, set its paths and run
settings, then run::

   soba_produce_challenger --recipe path/to/challenger.toml

Use the run's ``audit.json`` to investigate missing predictions. Passing a
single-file CHALLENGER check does not establish a match to a particular TEST file.
