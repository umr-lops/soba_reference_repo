soba_reference_repo
===================

Create a SOBA reference TEST/TARGET pair and report from a TOML recipe. The current exporter curates Sentinel-1 WV catalogues co-aligned with SWOT KaRIn
or HSCAT. It applies 13 SWOT-specific quality filters by default and the defined
ECMWF/HSCAT wind-speed difference filter to HSCAT wind-speed runs, saves Curated
Parquets, and validates exported pairs.

.. code-block:: bash

   soba_create_test_dataset --recipe path/to/recipe.toml

Use ``soba_validate_parquets`` to validate individual files by type. The exporter checks TEST/TARGET alignment before saving the pair.

Test suite: 159 collected tests.

.. toctree::
   :maxdepth: 2

   usage
   api/soba_reference_repo
