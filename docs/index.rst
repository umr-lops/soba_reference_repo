soba_reference_repo
===================

Create a SOBA reference TEST/TARGET pair and report from a TOML recipe. The
current exporter curates Sentinel-1 WV catalogues co-aligned with SWOT KaRIn.
It applies 13 SWOT-specific quality filters by default, saves Curated Parquets,
and validates the exported pair. Other reference sources are not yet supported.

.. code-block:: bash

   soba_create_test_dataset --recipe path/to/recipe.toml

Use ``soba_validate_parquets`` to validate one file by type, or a TEST/TARGET pair.

Test suite: 158 collected tests.

.. toctree::
   :maxdepth: 2

   usage
   api/soba_reference_repo
