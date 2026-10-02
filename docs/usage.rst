Install and run
===============

Install the package in a Python 3.11+ environment::

   python -m pip install -e .

Create a reference pair
-----------------------

Copy ``examples/swot-curation.toml`` to a local recipe. Set ``reference = "swot"``,
point each ``[[catalogues]]`` path to a WV co-aligned Parquet, and choose a new
``output_dir``. Paths in the recipe are resolved relative to that file. Then run::

   soba_reference_repo --recipe path/to/recipe.toml

The recipe supports any nonempty subset of S1A through S1D. Dates use ``YYYYMMDD``
and versions use ``X.Y``. The output directory must not already exist. The tool
saves one Curated Parquet per satellite, merges them, and writes aligned TEST and
TARGET Parquets, figures, an editable TeX report, and a manifest. Set
``compile = true`` to request a PDF if pdflatex or Tectonic is installed.

Each SWOT catalogue starts with 13 ordered quality filters. Change a default
filter under its catalogue with ``[[catalogues.rules]]`` and the default's ``id``;
set ``enabled = false`` to skip it. A new ``id`` with ``name`` and ``clauses``
appends a rule. Operators are ``eq``, ``gt``, ``ge``, ``lt``, ``le``, ``in``, and
``present``. The 13 defaults are SWOT-specific. ALTI and other references are
not implemented: selecting one fails before output files are written.

Validate Parquets
-----------------

The exporter validates TEST and TARGET columns, metadata, unique keys, and their
alignment after writing. To validate an existing SWOT pair independently::

   soba_validate_parquets --test path/to/TEST.parquet --target path/to/TARGET.parquet

Create a CHALLENGER
-------------------

Copy ``examples/cci-seastate-challenger.toml``, set its paths and run
settings, then run::

   soba_produce_challenger --recipe path/to/challenger.toml

The independent ``soba_validate_parquets`` command validates TEST/TARGET
pairs, not CHALLENGER.
