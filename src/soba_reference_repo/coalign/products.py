"""Reference products: which STAC point fields become reference parameters and ``ref_flag``.

The spec calls the parameter columns ``ref_param[_1,2,...N]`` and says their names differ per
dataset (for example ``windspeed_scat``, ``winddirection_scat``), so each is named after its
dataset. Only what a benchmark needs is kept: wind speed and direction for scatterometers,
significant wave height for altimeters and SWOT.

One entry per ``reference_product`` written by ``soba_matchups`` in a day manifest. Adding a
product is one entry here, nothing else changes.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Reference:
    """How one STAC reference product maps onto the catalogue."""

    name: str  # <refproductname> of the file name (spec section 1.1); "{ver}" = SWOT version
    params: tuple  # ((catalogue column, STAC point field), ...) in column order
    flag: str | None  # STAC point field copied to ref_flag; None = the product has none (null)


_SCAT = (("windspeed_scat", "wind_speed"), ("winddirection_scat", "wind_direction_to"))
_ASCAT_FLAG = "quality_flag"

REFERENCES = {
    "KNMI_HSCAT_HY2B_25KM": Reference("KNMI-HSCAT-HY2-25km", _SCAT, _ASCAT_FLAG),
    "KNMI_HSCAT_HY2C_25KM": Reference("KNMI-HSCAT-HY2-25km", _SCAT, _ASCAT_FLAG),
    "KNMI_ASCAT_METOPB_12P5KM": Reference("KNMI-ASCAT-METOP-12.5km", _SCAT, _ASCAT_FLAG),
    "KNMI_ASCAT_METOPC_12P5KM": Reference("KNMI-ASCAT-METOP-12.5km", _SCAT, _ASCAT_FLAG),
    # Not in the spec's name list: named after the same pattern.
    "KNMI_ASCAT_METOPB_25KM": Reference("KNMI-ASCAT-METOP-25km", _SCAT, _ASCAT_FLAG),
    "KNMI_ASCAT_METOPB_5P7KM": Reference("KNMI-ASCAT-METOP-5.7km", _SCAT, _ASCAT_FLAG),
    "CMEMS_WAV_L3_ALTI_VAVH": Reference(
        "CMEMS-WAV-L3-ALTI-TAC", (("swh_alti", "VAVH"),), None
    ),
    "ESACCI_SEASTATE_L2P_V5": Reference(
        "CCI-SEASTATE_ALTI_L2P-V5", (("swh_alti", "swh"),), "swh_quality_level",
    ),
    "PODAAC_SWOT_KARIN_L2_WINDWAVE_D0": Reference(
        "SWOT-KARIN-L2-WINDWAVE-{ver}", (("swh_swot", "swh_karin"),), "swh_karin_qual",
    ),
}


def reference_for(product: str) -> Reference:
    """The mapping of a STAC ``reference_product``; unknown products fail loudly."""
    if product not in REFERENCES:
        raise ValueError(f"no catalogue mapping for reference product {product!r}; "
                         f"known: {sorted(REFERENCES)}")
    return REFERENCES[product]


def product_name(reference: Reference, reference_version: str | None) -> str:
    """``<refproductname>`` of the file name; SWOT is named after PGD0 / PID0."""
    if "{ver}" not in reference.name:
        return reference.name
    version = (reference_version or "")[:4]
    if version not in ("PGD0", "PID0"):
        raise ValueError(f"cannot name a SWOT file from reference_version {reference_version!r}")
    return reference.name.format(ver=version)
