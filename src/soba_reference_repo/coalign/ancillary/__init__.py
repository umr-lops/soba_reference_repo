"""Ancillary readers: one module per data source, one shared interface.

A reader is a ``Source``: it names the catalogue columns it adds and fills them for a batch of
rows (dicts with ``ref_time``, ``ref_lon``, ``ref_lat``, ``sar_safe_slc`` ...). A value that
cannot be read (no file, outside the grid, fill value) stays ``None`` and is counted, so one
missing day never stops a run; a file that is present but wrong (unknown label, unit, time)
raises. Readers import their heavy libraries only when used, so the base builder needs none.
"""

from __future__ import annotations

from importlib import import_module
from pathlib import Path

# recipe name -> (module, class). The recipe may only name a source listed here.
READERS = {
    "tengeop": ("tengeop", "TenGeoPSource"),
    "ecmwf": ("ecmwf", "Ecmwf"),
}


class Source:
    """Base of every ancillary reader."""

    name = ""                 # recipe key
    columns: tuple = ()       # catalogue columns added, in order
    description = ""          # entry of the global attribute ``source ancillary datasets``

    def fill(self, rows: list) -> None:
        """Set ``self.columns`` on every row (``None`` where there is no value)."""
        raise NotImplementedError

    def counts(self) -> dict:
        """Run counters for the summary (rows left null, files missing ...)."""
        return {}


def make(name: str, root) -> Source:
    """The reader named ``name`` (a key of ``READERS``) reading under ``root``."""
    module, cls = READERS[name]
    return getattr(import_module(f"{__name__}.{module}"), cls)(Path(root))
