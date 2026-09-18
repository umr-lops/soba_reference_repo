soba_reference_repo
===================

Generates a SOBA **reference TEST dataset** report from two co-aligned catalogues: it crosses a
scatterometer catalogue and the SWOT KaRIn catalogue through the common Sentinel-1 Wave Mode
imagettes, applies the collocation filters, draws three figures, fills the SOBA LaTeX template
with the run's real metadata, compiles a PDF, and exports the spec-conformant WV TEST parquet
under the SOBA dataset naming convention.

Reference: *Format Description for parquet co-aligned datasets* (SOBA WP3, v1.1.0).

.. code-block:: bash

   soba_reference_repo \
     --satellite S1D --scatterometer ASCAT \
     --scat  <data-dir>/S1D_coaligned_catalogue_WV_..._KNMI-ASCAT-METOP-12.5km_0.2.parquet \
     --swot  <data-dir>/S1D_coaligned_catalogue_WV_..._PODAAC-SWOT-KARIN-L2-WINDWAVE-D0_0.1.parquet \
     --test-dir test_datasets

.. toctree::
   :maxdepth: 2

   usage
   api/soba_reference_repo
