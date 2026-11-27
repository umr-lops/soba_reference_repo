"""Read soba_matchups STAC output: day folders, manifests and Items.

Read-only, with no catalogue logic.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def find_days(product_dir, start: str, end: str, run=("ocn", "wv"), legacy=True) -> list:
    """The run folder (it holds ``manifest.json`` and ``items/``) of each day, in day order.

    Two soba_matchups layouts are read:

    - the common one, ``<product_dir>/<YYYY-MM-DD>/<product_type>/<mode>/`` with ``run`` =
      ``(product_type, mode)`` (e.g. ``swot/d2/2026-11-20/ocn/wv``);
    - with ``legacy``, the older one where the day folder is the run: ``<day>/`` or
      ``ocn/wv/<day>/``.

    When a day has both, the common layout wins. ``<day>.replaced-*`` folders are not days;
    ``start`` and ``end`` are inclusive.
    """
    root = Path(product_dir)
    common, old = {}, {}
    for manifest in [*root.glob("*/manifest.json"), *root.glob("*/*/*/manifest.json")]:
        folder = manifest.parent
        if DAY.match(folder.name):
            if legacy:
                old.setdefault(folder.name, folder)
        elif (DAY.match(folder.parent.parent.name) and folder.parent.parent.parent == root
              and (folder.parent.name, folder.name) == tuple(run)):
            common[folder.parent.parent.name] = folder
    found = {**old, **common}
    return [found[day] for day in sorted(found) if start <= day <= end]


def read_manifest(day_dir) -> dict:
    return json.loads((Path(day_dir) / "manifest.json").read_text())


def iter_items(day_dir):
    """Every STAC Item of a day, in file-name order."""
    for path in sorted((Path(day_dir) / "items").glob("*.json")):
        yield json.loads(path.read_text())
