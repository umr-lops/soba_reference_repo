"""Synthetic soba_matchups STAC tree and SAR archive for the coalign tests."""

import json
from pathlib import Path

OCN = "S1C_WV_OCN__2SSV_20261119T003214_20261119T004119_010398_014C82_3086"
SLC = "S1C_WV_SLC__1SSV_20261119T003214_20261119T004119_010398_014C82_AAAA"


def make_archive(root: Path, ocn=OCN, slc=SLC, with_slc=True) -> Path:
    """An archive with one OCN SAFE (and its SLC twin); returns the OCN SAFE dir."""
    mission = ocn[:3]
    sat = f"sentinel-1{mission[2].lower()}"
    ocn_dir = root / f"{sat}/L2/WV/{mission}_WV_OCN__2S/2026/323/{ocn}.SAFE"
    ocn_dir.mkdir(parents=True)
    if with_slc:
        (root / f"sentinel-1{mission[2].lower()}/L1/WV/{mission}_WV_SLC__1S/2026/323/{slc}.SAFE"
         ).mkdir(parents=True)
    return ocn_dir


def point(lon, lat, **fields):
    base = {
        "ref_time_utc": "2026-11-19T00:28:01.000000000Z", "ref_lon": lon, "ref_lat": lat,
        "sar_distance_to_coast": 186.0, "sar_incidence_angle": 23.4, "sar_elevation_angle": 21.0,
        "sar_ground_heading": 337.0, "sar_ground_heading_uncertainty": 0.4,
        "delta_diff_time_minutes": -4.2,
    }
    base.update(fields)
    return base


def make_item(ocn_dir: Path, imagette=1, points=None, pol="VV", platform="sentinel-1c",
              mode="WV", product_type="OCN", version="4008", source="HY-2B HSCAT"):
    stem = "20261119t003214-20261119t003217-010398-014c82"
    name = f"s1c-wv1-ocn-{pol.lower()}-{stem}-{imagette:03d}.nc"
    return {
        "geometry": {"type": "Polygon", "coordinates": [[
            [-72.9, -66.1], [-72.6, -66.3], [-72.2, -66.2], [-72.5, -66.0], [-72.9, -66.1]]]},
        "assets": {"sar": {"href": f"file://{ocn_dir}/measurement/{name}"}},
        "properties": {
            "start_datetime": "2026-11-19T00:32:14Z", "sarmatchup:sar_platform": platform,
            "sarmatchup:sar_instrument_mode": mode, "sarmatchup:sar_product_type": product_type,
            "sarmatchup:imagette_index": imagette, "sarmatchup:polarization": pol,
            "sarmatchup:matchups": [{
                "reference_file": "hscat_20261119_001653_hy_2b__40560_o_250_4008_ovw_l2.nc",
                "reference_source": source, "reference_version": version,
                "points": points if points is not None else [
                    point(-72.33, -66.2, wind_speed=11.94, wind_direction_to=167.5,
                          quality_flag=0.0, ice_probability=0.1)],
            }],
        },
    }


def write_day(product_dir: Path, day: str, items, reference_product="KNMI_HSCAT_HY2B_25KM",
              layout=""):
    """Write <product_dir>/<layout>/<day>/{manifest.json,items/}; returns the day folder."""
    folder = product_dir / layout / day if layout else product_dir / day
    (folder / "items").mkdir(parents=True)
    (folder / "manifest.json").write_text(json.dumps({"reference_product": reference_product}))
    for number, item in enumerate(items):
        (folder / "items" / f"item_{number:03d}.json").write_text(json.dumps(item))
    return folder
