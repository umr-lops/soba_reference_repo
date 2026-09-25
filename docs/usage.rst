Install and run
===============

Install
-------

Into an environment that already has the SOBA stack (numpy, pandas, pyarrow, matplotlib,
geopandas)::

   python -m pip install -e . --no-deps

``--no-deps`` because that environment already satisfies every dependency. Python 3.11.

Run
---

.. code-block:: bash

   soba_reference_repo \
     --satellite S1D --scatterometer ASCAT \
     --scat  <data-dir>/S1D_coaligned_catalogue_WV_..._KNMI-ASCAT-METOP-12.5km_0.2.parquet \
     --swot  <data-dir>/S1D_coaligned_catalogue_WV_..._PODAAC-SWOT-KARIN-L2-WINDWAVE-D0_0.1.parquet \
     --test-dir test_datasets

Without the install, ``PYTHONPATH=src python -m soba_reference_repo.cli`` behaves the same. If
``pdflatex`` is not on ``PATH``, point the tool at it with ``--miktex-bin /path/to/miktex/bin/x64``
or by exporting ``MIKTEX_BIN``.

Merged SWOT-only TEST/TARGET export
-----------------------------------

Use the ``swot-test`` subcommand to merge one SWOT WV catalogue for each of S1A, S1C, and S1D.
The existing SCAT×SWOT workflow remains available through the command shown above. Set
``DATA_DIR`` to the catalogue directory; each wildcard below must resolve to exactly one file::

   DATA_DIR=/path/to/coaligned
   soba_reference_repo swot-test \\
     --swot-catalogue "$DATA_DIR"/S1A_coaligned_catalogue_WV_*_PODAAC-SWOT-KARIN-L2-WINDWAVE-D0_0.1.parquet \\
     --swot-catalogue "$DATA_DIR"/S1C_coaligned_catalogue_WV_*_PODAAC-SWOT-KARIN-L2-WINDWAVE-D0_0.1.parquet \\
     --swot-catalogue "$DATA_DIR"/S1D_coaligned_catalogue_WV_*_PODAAC-SWOT-KARIN-L2-WINDWAVE-D0_0.1.parquet \\
     --test-dir test_datasets --report-dir runs/swot_merged --version 0.1 --no-compile

The command defaults to the current UTC production date. Add ``--production-date YYYYMMDD`` to
reproduce a fixed name. It writes one dated directory containing exactly:

.. code-block:: text

   S1_WV_<date>_swh_0.1/
     S1_reference_test_dataset_WV_<date>_swh_0.1.parquet
     S1_target_dataset_WV_<date>_swh_0.1.parquet

TEST contains ``primary_key``, SAR time/position/angles/coast distance, ``sar_safe_slc``,
``sar_safe_ocn``, ``swot_lon``, ``swot_lat``, ``swot_waveheight``, ``swot_time``, and
``swot_source``. TARGET contains only the key, the two SAFE fields, SWOT coordinates, and SWOT time.
The primary key combines the SLC SAFE identifier with ``swot_lon`` and ``swot_lat`` rounded to
one decimal. Before these schema/key checks, the tool applies thirteen NECTAR filters in
order: land/coast, no dynamic ice, present time delta, positive KaRIn/OCN/nearest-altimeter/WW3
wave heights, present S1 classification, time delta under two hours, complete overlap, no
IMERG/SWOT rain, and allowed S1 classes (AF, BS, MCC, OF, POS, RC, WS). Complete overlap uses
``overlap_pct >= 100`` because the source never exceeds 100; ``> 100`` would retain nothing.
``ref_hs_alti_closest`` substitutes for unavailable SWOT nadir height, and positive SAR coast
distance stands in for the unavailable direct SWOT land flag. S1D has no classified source
rows, so the full filter set retains none of its rows. The report and manifest record each
cumulative filter count. The tool then excludes rows missing required values, keeps the
closest-time duplicate within each mission, and fails on cross-mission key collisions.
``swot_source`` comes from ``swot_path``; ``sar_ground_heading`` remains null.

Each Parquet has only the five global attributes ``source swot``, ``source ancillary datasets``,
``library used to produce the parquet``, ``library version``, and ``creation date``. A separate
``runs/swot_merged/<TEST-stem>/`` directory holds the editable LaTeX source, its three figures, and
a provenance manifest. The report uses the existing SOBA TEST-document layout. The coverage
map plots every retained row in one color. The monthly count and SWOT wave-height figures use
distinct, consistent colors and legends for the missions present (S1A and S1C in the current
strict-filter run). S1D is supplied but has no classified rows; S1B has no input catalogue.
The PDF is written there when LaTeX is installed and compilation is enabled.
This export validates its own schema, metadata, and paired keys; it does not use the separate
SCAT/SWOT validator because that validator checks a different schema.

Flags
-----

.. list-table::
   :header-rows: 1

   * - Flag
     - Default
     - Meaning
   * - ``--scat``
     - required
     - scatterometer catalogue parquet
   * - ``--swot``
     - required
     - SWOT KaRIn catalogue parquet
   * - ``--satellite``
     - required
     - Sentinel-1 platform, checked against the catalogue names and the SAFE identifiers
   * - ``--scatterometer``
     - required
     - ``ASCAT`` or ``HSCAT``
   * - ``--scene-key``
     - off
     - match the catalogues on the normalised imagette key instead of their own ``primary_key``
   * - ``--overlap-min-pct``
     - ``100``
     - required SAR/SWOT footprint overlap
   * - ``--rain-max-mm-h``
     - ``0.3``
     - maximum IMERG mean rain rate
   * - ``--time-max-min``
     - ``120``
     - maximum direct scatterometer--SWOT time difference
   * - ``--output-dir``
     - ``runs/<label>``
     - report build directory
   * - ``--test-dir``
     - ``test_datasets``
     - directory for the TEST parquet and the run manifest
   * - ``--test-name``
     - generated
     - override the TEST filename entirely
   * - ``--dataset-version``
     - ``0.1``
     - ``<version>`` field of the TEST filename
   * - ``--reference``
     - ``scat,swot``
     - reference families the validator requires
   * - ``--validate``
     - off
     - run the SOBA parquet validator on the TEST file; exit 1 if it fails
   * - ``--validator``
     - bundled copy
     - path to a validator module to use instead of that copy
   * - ``--compile`` / ``--no-compile``
     - ``--compile``
     - skip LaTeX to iterate on figures quickly
   * - ``--miktex-bin``
     - ``$MIKTEX_BIN``, else ``PATH``
     - directory holding the ``pdflatex`` executable

Outputs
-------

The report directory keeps everything the PDF was built from, so it can be hand-edited and
recompiled: the filled ``.tex``, the figures under ``images_<dataset file name>/``, and the
staged LaTeX assets. After a successful compile the LaTeX byproducts are removed;
``--keep-intermediates`` keeps them and ``--no-compile`` skips LaTeX entirely.

The deliverables directory receives the TEST parquet and a manifest named after it, so a dataset
and its provenance travel together. The PDF carries the same name as the parquet it documents.

Matching the two catalogues
---------------------------

The crossing pairs rows on the catalogues' own ``primary_key`` by default, the identifier both
sides ship and agree on for a shared imagette. ``--scene-key`` matches on the normalised imagette
key instead, which also bridges a SAFE-form or reference-point disagreement between the two. The
mode used is recorded in the manifest as ``match_on``.

Validating the output
---------------------

``--validate`` runs the SOBA validator against the parquet the run has just written and exits
non-zero when the file fails. The mandatory reference columns follow the reference's source
(``scat_lon``/``scat_lat``/``scat_time`` for a scatterometer, ``swot_*`` for SWOT); the retired
``ref_lon``/``ref_lat``/``ref_time`` names still satisfy one reference family when the ``<ref>_``
form is absent, so files written before the rename validate too.
