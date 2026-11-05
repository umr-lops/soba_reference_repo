"""Load the ESA Sentinel-1 quality-disclaimer (QD) impacted-product database.

The QD database is collected at Ifremer from the ESA SAR-MPC quality disclaimers. It lists the
Sentinel-1 SAFE products that are impacted by a documented degradation. The curated curation uses
it to remove any row whose SLC input *or* associated OCN product is listed. Only explicit product
names are used (glob patterns and period-only disclaimers are ignored), mirroring the reference
report methodology.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

#: Parquet files that carry an explicit list of impacted product names.
_DATABASE_FILES = ("impacted_products.parquet", "product_mentions.parquet")


def _safe_basename(value) -> str | None:
    """Reduce a SAFE path/id (``.../S1A_....SAFE:WV_003``) to its bare ``.SAFE`` name."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip()
    if not text or text in {"nan", "None", "<NA>"}:
        return None
    basename = text.rsplit("/", 1)[-1]
    basename = basename.split(":", 1)[0].strip()
    return basename or None


def load_flagged_safe_basenames(path) -> frozenset[str]:
    """Return the set of flagged Sentinel-1 SAFE basenames from a QD database.

    Args:
        path: a directory containing ``impacted_products.parquet`` and/or
            ``product_mentions.parquet``, or a single such Parquet file.

    Returns:
        The set of ``.SAFE`` product names to exclude (empty if none found).

    Raises:
        FileNotFoundError: if ``path`` is not a directory or contains none of the
            expected Parquet files.
        ValueError: if the Parquet files lack a ``product_name`` column.
    """
    path = Path(path)
    if path.is_file():
        files = [path]
    elif path.is_dir():
        files = [path / name for name in _DATABASE_FILES if (path / name).is_file()]
    else:
        raise FileNotFoundError(f"quality-disclaimer database not found: {path}")
    if not files:
        raise FileNotFoundError(f"no QD Parquet found under: {path}")

    basenames: set[str] = set()
    for file in files:
        frame = pd.read_parquet(file, columns=["product_name"])
        for value in frame["product_name"].dropna().unique():
            basename = _safe_basename(value)
            if basename:
                basenames.add(basename)
    return frozenset(basenames)


def flagged_safe_mask(frame, columns, flagged):
    """Boolean mask of rows whose SLC or OCN SAFE product is in ``flagged``.

    Args:
        frame: the source frame.
        columns: the two SAFE identifier column names (slc, ocn).
        flagged: set of flagged ``.SAFE`` basenames (see :func:`load_flagged_safe_basenames`).

    Returns:
        A boolean Series that is ``True`` for rows to drop. Missing SAFE names are kept.
    """
    mask = pd.Series(False, index=frame.index)
    for column in columns:
        if column not in frame:
            continue
        safe_names = (
            frame[column].astype("string")
            .str.rsplit("/", n=1)
            .str[-1]
            .str.split(":", n=1)
            .str[0]
        )
        mask |= safe_names.isin(flagged).fillna(False)
    return mask


def apply_flagged_mask(frame, columns, flagged):
    """Return (kept_frame, diagnostics) after dropping QD-flagged rows."""
    before = len(frame)
    drop_mask = flagged_safe_mask(frame, columns, flagged)
    kept = frame.loc[~drop_mask].copy()
    return kept, {
        "flagged_rows": int(drop_mask.sum()),
        "input_rows": before,
        "remaining_rows": len(kept),
    }
