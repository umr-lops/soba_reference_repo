"""Read soba_matchups STAC output: day folders, manifests and Items.

Read-only, with no catalogue logic.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def find_days(product_dir, start: str, end: str) -> list:
    """Day folders under ``product_dir`` (any depth, so ``<day>`` and ``ocn/wv/<day>`` both work).

    A folder counts when its name is ``YYYY-MM-DD`` and it holds a ``manifest.json``.
    ``<day>.replaced-*`` folders are not days. ``start`` and ``end`` are inclusive.
    """
    found = {}
    for manifest in Path(product_dir).glob("**/manifest.json"):
        day = manifest.parent
        if DAY.match(day.name) and start <= day.name <= end:
            found.setdefault(day.name, day)
    return [found[name] for name in sorted(found)]


def read_manifest(day_dir) -> dict:
    return json.loads((Path(day_dir) / "manifest.json").read_text())


def iter_items(day_dir):
    """Every STAC Item of a day, in file-name order."""
    for path in sorted((Path(day_dir) / "items").glob("*.json")):
        yield json.loads(path.read_text())
