"""TenGeoP SAR surface roughness classification (``class_1``, ``pos_1``) for WV imagettes.

Source: the CLS deep-learning classifier writes, for every WV SLC SAFE, a sibling
``<yyyy>/<mm>/<dd>/<mission>_WV_DL__2S<...>.SAFE/DL_Summary.xml`` with one ``<id value="N">``
per imagette (N = the WV imagette number). Each has ``process_status``, and when it is ``OK``
the three most likely classes (``class_1..3``) with their probabilities (``prob_1..3``).

The spec's ``class_1`` is the most likely class and ``pos_1`` its coefficient (probability).
Standard library only, so it runs wherever the catalogue builder does.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

# short_label of the classifier model: the ten TenGeoP classes.
CLASSES = frozenset("AF BS IB LWA MCC OF POS RC SI WS".split())
SUMMARY = "DL_Summary.xml"
# S1C_WV_SLC__1SSV_<yyyy><mm><dd>T<hhmmss>_...: the start date gives the folder.
SLC_NAME = re.compile(r"S1[A-D]_WV_SLC__1S[SD][VH]_(\d{4})(\d{2})(\d{2})T")


def dl_summary_path(root, slc_safe: str) -> Path:
    """Where the classifier output of one SLC SAFE lives (``slc_safe`` ends with ``.SAFE``)."""
    found = SLC_NAME.match(slc_safe)
    if found is None:
        raise ValueError(f"not a WV SLC SAFE name: {slc_safe!r}")
    year, month, day = found.groups()
    dl_name = slc_safe.replace("_SLC__1S", "_DL__2S")
    return Path(root) / year / month / day / dl_name / SUMMARY


class TenGeoP:
    """Class lookup by (SLC SAFE name, imagette number), reading each summary once."""

    def __init__(self, root):
        self.root = Path(root)
        self._cache: dict = {}
        self.missing_summaries = 0

    def _imagettes(self, slc_safe: str) -> dict:
        if slc_safe not in self._cache:
            path = dl_summary_path(self.root, slc_safe)
            if not path.is_file():
                self.missing_summaries += 1
                self._cache[slc_safe] = {}
            else:
                self._cache[slc_safe] = {
                    int(node.get("value")): node
                    for node in ET.parse(path).getroot().find("imagettes")
                }
        return self._cache[slc_safe]

    def lookup(self, slc_safe: str, imagette: int) -> tuple:
        """``(class_1, pos_1)``, or ``(None, None)`` when the imagette has no valid class."""
        node = self._imagettes(slc_safe).get(int(imagette))
        if node is None or node.findtext("process_status") != "OK":
            return None, None
        label, prob = node.findtext("class_1"), node.findtext("prob_1")
        if label is None or prob is None:
            return None, None
        if label not in CLASSES:
            where = dl_summary_path(self.root, slc_safe)
            raise ValueError(f"unknown TenGeoP class {label!r} in {where}")
        value = float(prob)
        if not 0.0 <= value <= 1.0:
            raise ValueError(f"TenGeoP probability {prob} outside [0, 1]: "
                             f"{slc_safe} #{imagette}")
        return label, value
