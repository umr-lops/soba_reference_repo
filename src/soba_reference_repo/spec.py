"""What the SOBA parquet format specification requires, kept in one place.

Source: https://soba.ifremer.fr/parquet_format_specification.html (``parquet_format_spec.py`` of
soba-dataflux-overview). Update this module when the spec changes.
"""

from __future__ import annotations

import re

SPEC_VERSION = "1.6.0"

# <refproductname> of the file names (spec, naming convention). SWOT carries the release of the
# run (D0, D2, ...).
REF_PRODUCTS = (
    "KNMI-HSCAT-HY2-25km",
    "KNMI-ASCAT-METOP-12.5km",
    "CMEMS-WAV-L3-ALTI-TAC",
    "CCI-SEASTATE-ALTI-L2P-V4",
    "CCI-SEASTATE_ALTI_L2P-V5",
    "CMEMS-ALTI_L3_VAVH",
    "CMEMS-INSITU_013_030-BUOYS-HISTORY",
    "NOAA-NDBC-BUOYS-STDMET-TXT",
    "NOAA-NDBC-BUOYS-CWIND-TXT",
)
SWOT_PRODUCT_RE = re.compile(r"PODAAC-SWOT-KARIN-L2-WINDWAVE-[A-Za-z0-9]+")


def is_spec_product(name: str) -> bool:
    """Whether ``name`` is a ``<refproductname>`` of the spec list."""
    return name in REF_PRODUCTS or SWOT_PRODUCT_RE.fullmatch(name) is not None


# Mandatory columns of the co-aligned catalogue tables; curated datasets have the same status.
# ``ref_param[_1,2,...N]`` is named per dataset (e.g. windspeed_scat): not listed.
WV_CATALOGUE_COLUMNS = (
    "primary_key", "sar_time", "sar_incidence_angle", "sar_elevation_angle",
    "sar_ground_heading", "sar_distance_to_coast", "sar_path_ocn", "sar_path_slc",
    "sar_safe_slc", "sar_safe_ocn", "ref_lon", "ref_lat", "ref_time", "ref_flag", "ref_id",
    "legacy_usage", "ref_source", "class_1", "pos_1",
)
IW_CATALOGUE_COLUMNS = (
    "primary_key", "sar_time", "sar_incidence_angle", "sar_elevation_angle",
    "sar_ground_heading", "sar_distance_to_along_track_edge", "sar_distance_to_cross_track_edge",
    "sar_path_ocn", "sar_path_slc", "sar_path_grd", "sar_safe_grd", "sar_safe_slc",
    "sar_safe_ocn", "ref_lon", "ref_lat", "ref_time", "ref_id", "sea_ice_concentration",
    "bathymetry", "ref_distance_to_coast", "WW3_Hs", "ECMWF_Wind_Speed",
    "ECMWF_Wind_Direction", "imerg_rainrate",
)
# The spec has WV and IW tables; EW and SM products are SAFE-level like IW.
CATALOGUE_COLUMNS = {
    "WV": WV_CATALOGUE_COLUMNS, "IW": IW_CATALOGUE_COLUMNS,
    "EW": IW_CATALOGUE_COLUMNS, "SM": IW_CATALOGUE_COLUMNS,
}
# Optional in every dataset (spec 1.5.0 dropped them); checked when present.
OPTIONAL_POSITION_COLUMNS = ("sar_lat", "sar_lon")

# Global attributes mandatory in every SOBA parquet; plus ``source <ref>`` (scat, alti, swot...).
GLOBAL_ATTRIBUTES = (
    "source ancillary datasets", "library used to produce the parquet", "library version",
    "creation date",
)
SOURCE_ATTRIBUTE_RE = re.compile(r"source \S+")  # "source scat", not "source ancillary datasets"
CURATED_ATTRIBUTE = "source_catalogue_dataset"

MIN_KEY_DECIMALS = 2  # ref_lon / ref_lat in primary_key
