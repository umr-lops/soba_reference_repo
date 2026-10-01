"""Standalone schema and key validation of exported TEST/TARGET parquet files.

This validates the current SWOT export. New reference types can register their
own schemas without applying the SWOT-specific filter or column rules to them.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from .swot_test import validate_swot_test_pair


def validate_pair(test: Path, target: Path, reference: str = "swot") -> bool:
    """Validate columns, metadata and aligned keys for a reference pair."""
    if reference == "swot":
        return validate_swot_test_pair(test, target)
    raise ValueError(f"unsupported reference: {reference}; only swot is available")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--test", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--reference", default="swot")
    args = parser.parse_args(argv)
    try:
        validate_pair(args.test, args.target, args.reference)
    except (ValueError, OSError) as error:
        parser.exit(1, f"validation failed: {error}\n")
    print("TEST/TARGET validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
