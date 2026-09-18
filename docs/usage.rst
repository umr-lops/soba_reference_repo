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
