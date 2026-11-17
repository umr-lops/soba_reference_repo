"""Interactive HTML report of a curated / coaligned-catalogue run, for the dataset producers.

The PDF document is the dataset's business card; this page is the producers'
workbench to check that the curation choices are relevant and that the columns
hold what is expected. It is computed from a run directory only (no new Python
dependency) and rendered in the browser with Plotly.js (pinned CDN build):

* **sub-datasets**: SAR unit x reference database (x altimeter for WV-alti),
  with their row counts and time span;
* **distributions** of the reference variable (WV-alti: ``hs_alti_mean``, the
  mean Hs over the 30 km / 25 min window, then ``ref_hs_alti_closest``;
  WV-SWOT: the KaRIn Hs; WV-HSCAT: wind speed and direction): histogram,
  monthly counts and a 2-degree density map, per sub-dataset;
* for a curated run, the **filter cascade** is replayed on the source
  catalogues: rows removed / kept per rule and per catalogue, and the same
  figures for the rows removed by each rule, compared with the kept rows;
* **comparison with the previous version** (``--previous-run``): rules added,
  removed or changed, and the row counts N-1 vs N;
* **column conformity**: type, missing fraction, range or distinct values, and
  automatic warnings (all missing, constant, latitude / longitude out of range,
  ``UNKNOWN`` strings, mostly missing).

The aggregates (not the rows) are embedded as JSON, so a page stays a few MB.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import tomllib
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.compute as pc
import pyarrow.parquet as pq
import pyarrow.types as pat

PLOTLY_JS = "https://cdn.plot.ly/plotly-2.35.2.min.js"
MAP_STEP = 2.0
# (column, label, (start, stop, step)) candidates per product; the first present column of
# each label is used
VARIABLES = {
    "alti": [
        ("hs_alti_mean", "Hs, mean of the altimeter points within 30 km / 25 min (m)",
         (0, 15, 0.1)),
        ("ref_hs_alti_closest", "Hs of the closest altimeter point (m)", (0, 15, 0.1)),
    ],
    "swot": [("ref_mean_hs_karin", "SWOT KaRIn Hs (m)", (0, 15, 0.1))],
    "scat": [
        ("scat_windspeed", "HSCAT wind speed (m/s)", (0, 30, 0.5)),
        ("ref_param_2", "HSCAT wind speed (m/s)", (0, 30, 0.5)),
        ("scat_winddirection", "HSCAT wind direction (deg)", (0, 360, 10)),
        ("ref_param_1", "HSCAT wind direction (deg)", (0, 360, 10)),
    ],
}
_PRODUCT_RE = re.compile(r"_(?:SV|DV|SH|DH)_(.+?)_\d+\.\d+\.parquet$")
_CCI_ALT_RE = re.compile(r"L2P-SWH-(.+?)-\d{8}T")
_CMEMS_ALT_RE = re.compile(r"global_vavh_l3_rt_(.+?)_\d{8}T")
_CMEMS_ALT = {"al": "saral", "c2": "cryosat-2", "cfo": "cfosat", "h2b": "hy-2b", "h2c": "hy-2c",
              "j3": "jason-3", "s3a": "sentinel-3_a", "s3b": "sentinel-3_b",
              "s6a": "sentinel-6_a", "swot": "swot", "swon": "swot"}


# ---------------------------------------------------------------------------
# run loading
# ---------------------------------------------------------------------------
def _product(manifest, recipe) -> str:
    reference = str(manifest.get("reference") or recipe.get("reference") or "").lower()
    if reference.startswith("alti"):
        return "alti"
    if "scat" in reference:
        return "scat"
    return "swot"


def _database(path) -> str:
    match = _PRODUCT_RE.search(Path(path).name)
    token = match.group(1) if match else Path(path).stem
    return re.split(r"[-_]", token)[0]


def altimeter_of(fname) -> str:
    """Altimeter mission from a CCI L2P / CMEMS L3 file name ('' if not recognised)."""
    fname = str(fname)
    match = _CCI_ALT_RE.search(fname)
    if match:
        return match.group(1).lower()
    match = _CMEMS_ALT_RE.search(fname)
    if match:
        return _CMEMS_ALT.get(match.group(1), match.group(1))
    return ""


def _variables(product, columns) -> list[tuple[str, str, tuple]]:
    chosen, labels = [], set()
    for column, label, bins in VARIABLES[product]:
        if column in columns and label not in labels:
            chosen.append((column, label, bins))
            labels.add(label)
    return chosen


def _read(path, columns) -> pd.DataFrame:
    available = set(pq.read_schema(path).names)
    return pq.read_table(path, columns=[c for c in columns if c in available]).to_pandas()


# ---------------------------------------------------------------------------
# aggregates
# ---------------------------------------------------------------------------
def aggregate(frame, variables, with_map=True) -> dict:
    """Row count, histograms, monthly counts and (optionally) the 2-degree density map."""
    out: dict = {"n": int(len(frame)), "hist": {}, "hist_missing": {}, "monthly": {}}
    for column, _, (start, stop, step) in variables:
        if column in frame:
            values = pd.to_numeric(frame[column], errors="coerce").to_numpy(dtype=float)
        else:
            values = np.full(len(frame), np.nan)
        edges = np.arange(start, stop + step / 2, step)
        counts, _ = np.histogram(values[np.isfinite(values)], bins=edges)
        out["hist"][column] = counts.astype(int).tolist()
        out["hist_missing"][column] = int((~np.isfinite(values)).sum())
    if len(frame) and "sar_time" in frame:
        months = pd.to_datetime(frame["sar_time"], utc=True, errors="coerce").dt.strftime("%Y-%m")
        out["monthly"] = {str(k): int(v) for k, v in months.value_counts().items()}
    if with_map and len(frame) and {"sar_lat", "sar_lon"} <= set(frame.columns):
        lat = pd.to_numeric(frame["sar_lat"], errors="coerce").to_numpy(dtype=float)
        lon = pd.to_numeric(frame["sar_lon"], errors="coerce").to_numpy(dtype=float)
        ok = (np.abs(lat) <= 90) & (np.abs(lon) <= 360)
        lon = np.where(lon[ok] > 180, lon[ok] - 360, lon[ok])
        cells = pd.DataFrame({"x": np.floor(lon / MAP_STEP) * MAP_STEP + MAP_STEP / 2,
                              "y": np.floor(lat[ok] / MAP_STEP) * MAP_STEP + MAP_STEP / 2})
        out["map"] = [[float(x), float(y), int(n)] for (x, y), n in cells.value_counts().items()]
    return out


def _subset_frames(frame, catalogue_label, product):
    """(subset id, level, parent, frame) for a catalogue and, for WV-alti, its altimeters."""
    yield catalogue_label, 1, "ALL", frame
    if product == "alti" and "fname" in frame:
        missions = frame["fname"].map(altimeter_of)
        for mission in sorted(m for m in missions.unique() if m):
            yield f"{catalogue_label} / {mission}", 2, catalogue_label, frame[missions == mission]


# ---------------------------------------------------------------------------
# column conformity
# ---------------------------------------------------------------------------
def _is_numeric(kind) -> bool:
    return pat.is_integer(kind) or pat.is_floating(kind)


def column_conformity(paths) -> list[dict]:
    """Per column: type, missing %, range / distinct values and warnings, over the files."""
    stats: dict[str, dict] = {}
    for path in paths:
        table = pq.read_table(path)
        for name in table.column_names:
            column = table.column(name)
            item = stats.setdefault(name, {"column": name, "type": str(column.type), "rows": 0,
                                           "missing": 0, "min": None, "max": None, "sum": 0.0,
                                           "finite": 0, "distinct": set(), "unknown": 0,
                                           "files": 0})
            item["files"] += 1
            item["rows"] += len(column)
            if _is_numeric(column.type):
                values = column.to_numpy(zero_copy_only=False).astype(float)
                finite = values[np.isfinite(values)]
                item["missing"] += len(values) - len(finite)
                if len(finite):
                    lo, hi = float(finite.min()), float(finite.max())
                    item["min"] = lo if item["min"] is None else min(item["min"], lo)
                    item["max"] = hi if item["max"] is None else max(item["max"], hi)
                    item["sum"] += float(finite.sum())
                    item["finite"] += len(finite)
                continue
            item["missing"] += column.null_count
            if pat.is_string(column.type) or pat.is_large_string(column.type):
                if len(item["distinct"]) <= 50:
                    item["distinct"].update(str(v) for v in pc.unique(column).to_pylist()[:51])
                for flag in ("UNKNOWN", ""):
                    item["unknown"] += int(pc.sum(pc.equal(column, flag)).as_py() or 0)
    rows = []
    for item in stats.values():
        missing = item["missing"] / item["rows"] if item["rows"] else 0.0
        warnings = []
        if item["rows"] and item["missing"] >= item["rows"]:
            warnings.append("all missing")
        elif missing > 0.5:
            warnings.append(f"{missing:.0%} missing")
        if item["min"] is not None and item["min"] == item["max"] and item["finite"] > 1:
            warnings.append("constant")
        name = item["column"].lower()
        if item["min"] is not None and name.endswith("lat") and (
                item["min"] < -90 or item["max"] > 90):
            warnings.append("latitude out of [-90, 90]")
        if item["min"] is not None and name.endswith("lon") and (
                item["min"] < -180 or item["max"] > 360):
            warnings.append("longitude out of range")
        if item["unknown"]:
            warnings.append(f"{item['unknown']:,} UNKNOWN / empty")
        if item["min"] is not None:
            content = (f"{item['min']:.4g} … {item['max']:.4g}, mean "
                       f"{item['sum'] / max(item['finite'], 1):.4g}")
        elif item["distinct"]:
            count = len(item["distinct"])
            sample = ", ".join(sorted(item["distinct"])[:3])
            content = (f"{'> 50' if count > 50 else count} distinct: {sample}"
                       + ("…" if count > 3 else ""))
        else:
            content = ""
        rows.append({"column": item["column"], "type": item["type"],
                     "missing_pct": round(100 * missing, 2), "content": content,
                     "files": item["files"], "warnings": warnings})
    return sorted(rows, key=lambda row: (not row["warnings"], row["column"]))


# ---------------------------------------------------------------------------
# comparison with the previous version
# ---------------------------------------------------------------------------
def _catalogue_key(item) -> str:
    return f"{item['mission']} {_database(item.get('curated_path') or item['path'])}"


def _load_run(directory):
    directory = Path(directory)
    manifest = json.loads((directory / "manifest.json").read_text())
    recipe = {}
    if (directory / "recipe.toml").is_file():
        with (directory / "recipe.toml").open("rb") as stream:
            recipe = tomllib.load(stream)
    return manifest, recipe


def compare_with_previous(run_dir, previous_dir) -> dict:
    """Rules added / removed / changed and row counts, previous run (N-1) vs this run (N)."""
    new_manifest, new_recipe = _load_run(run_dir)
    old_manifest, old_recipe = _load_run(previous_dir)

    def rules(recipe):
        out = {}
        for item in recipe.get("catalogues", []):
            for rule in item.get("rules", []):
                if rule.get("enabled", True):
                    out.setdefault(rule["id"], rule)
        return out

    def counts(manifest):
        return {_catalogue_key(item): item for item in manifest.get("sources", [])}

    new_rules, old_rules = rules(new_recipe), rules(old_recipe)
    rule_rows = []
    for rule_id in dict.fromkeys([*old_rules, *new_rules]):
        old, new = old_rules.get(rule_id), new_rules.get(rule_id)
        if old is None:
            status = "added"
        elif new is None:
            status = "removed"
        elif old.get("clauses") != new.get("clauses"):
            status = "changed"
        else:
            status = "unchanged"
        rule_rows.append({"id": rule_id, "status": status,
                          "old": json.dumps(old.get("clauses")) if old else "",
                          "new": json.dumps(new.get("clauses")) if new else ""})
    new_counts, old_counts = counts(new_manifest), counts(old_manifest)
    count_rows = []
    for key in sorted(set(new_counts) | set(old_counts)):
        old, new = old_counts.get(key), new_counts.get(key)
        count_rows.append({"catalogue": key,
                           "old_input": old["input_rows"] if old else None,
                           "old_kept": old["curated_rows"] if old else None,
                           "new_input": new["input_rows"] if new else None,
                           "new_kept": new["curated_rows"] if new else None})
    return {"previous_version": str(old_recipe.get("version", "?")),
            "previous_dir": str(previous_dir), "rules": rule_rows, "counts": count_rows}


# ---------------------------------------------------------------------------
# payload
# ---------------------------------------------------------------------------
def _qd_context(run_dir, recipe, rules_by_key):
    uses_qd = any(clause[1] == "qd_flagged" for rules in rules_by_key.values()
                  for rule in rules for clause in rule.get("clauses", []))
    qd = recipe.get("quality_disclaimers")
    if not uses_qd or not qd:
        return None
    from .quality_disclaimers import load_flagged_safe_basenames

    if isinstance(qd, str) and not Path(qd).is_absolute():
        qd = str((Path(run_dir) / qd).resolve())
    return {"qd_flagged": load_flagged_safe_basenames(qd)}


def _rules_key(mission, path) -> tuple[str, str]:
    """Catalogue identity robust to relative / absolute recipe paths."""
    return mission, Path(path).name


def build_payload(run_dir, previous_run=None) -> dict:
    """All the aggregates of the page (JSON-serialisable)."""
    from .alti_report import _replay_removed, _rule_columns
    from .curation import resolve_rules

    run_dir = Path(run_dir)
    manifest, recipe = _load_run(run_dir)
    product = _product(manifest, recipe)
    sources = sorted(manifest["sources"], key=lambda s: (s["mission"], s.get("path", "")))
    curated = any(source.get("steps") for source in sources)
    output_paths = [Path(s.get("curated_path") or s["path"]) for s in sources]
    columns = set().union(*(set(pq.read_schema(p).names) for p in output_paths))
    variables = _variables(product, columns)
    base_columns = ["sar_time", "sar_lat", "sar_lon", "fname"] + [v[0] for v in variables]

    groups = [{"id": "kept", "label": "Curated rows (kept)" if curated else "All rows"}]
    data: dict[str, dict] = {"kept": {}}
    frames: dict[str, list] = {"kept": []}
    subsets = {"ALL": {"id": "ALL", "level": 0, "parent": None}}
    cascade = []
    rules_by_key = {_rules_key(item["mission"], item["path"]): item.get("rules", [])
                    for item in recipe.get("catalogues", [])}
    context = _qd_context(run_dir, recipe, rules_by_key) if curated else None
    if curated:
        groups.append({"id": "input", "label": "Source catalogue rows (before curation)"})
        data["input"], frames["input"] = {}, []

    def add(group_id, frame, label):
        for sid, level, parent, sub in _subset_frames(frame, label, product):
            subsets.setdefault(sid, {"id": sid, "level": level, "parent": parent})
            data[group_id][sid] = aggregate(sub, variables, with_map=level <= 1)

    for source in sources:
        label = _catalogue_key(source)
        if not curated:
            kept = _read(source["path"], base_columns)
        else:
            recipe_rules = rules_by_key.get(_rules_key(source["mission"], source["path"]), [])
            rules = [rule for rule in resolve_rules(recipe_rules, include_defaults=False)
                     if rule.get("enabled", True)]
            needed = list(dict.fromkeys(base_columns + sorted(_rule_columns(rules))))
            frame = _read(source["path"], needed).reset_index(drop=True)
            removed = _replay_removed(frame, rules, context)
            dropped = [index for part in removed.values() for index in part]
            kept = frame.drop(index=dropped)
            row = {"catalogue": label, "input": int(len(frame)), "kept": int(len(kept)),
                   "manifest_kept": source["curated_rows"], "rules": []}
            for rule in rules:
                group_id = f"rule:{rule['id']}"
                if group_id not in data:
                    groups.append({"id": group_id, "label": f"Removed by {rule['id']}"})
                    data[group_id], frames[group_id] = {}, []
                part = frame.loc[removed.get(rule["id"], frame.index[:0])]
                frames[group_id].append(part)
                row["rules"].append({"id": rule["id"], "name": rule.get("name", ""),
                                     "clauses": "; ".join(json.dumps(c) for c in rule["clauses"]),
                                     "removed": int(len(part))})
                add(group_id, part, label)
            cascade.append(row)
            frames["input"].append(frame)
            add("input", frame, label)
        frames["kept"].append(kept)
        add("kept", kept, label)
    for group in groups:
        parts = frames[group["id"]]
        whole = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
        data[group["id"]]["ALL"] = aggregate(whole, variables)
    months = sorted({m for g in data.values() for s in g.values() for m in s["monthly"]})
    return {
        "title": "", "product": product, "curated": curated,
        "version": str(recipe.get("version", "?")),
        "production_date": str(recipe.get("production_date", "")),
        "variables": [{"id": c, "label": label, "bins": list(bins)}
                      for c, label, bins in variables],
        "subsets": sorted(subsets.values(), key=lambda s: (s["level"] > 0, s["id"])),
        "groups": groups, "months": months, "data": data, "cascade": cascade,
        "files": [{"catalogue": _catalogue_key(s),
                   "file": Path(s.get("curated_path") or s["path"]).name,
                   "input": s["input_rows"], "rows": s["curated_rows"]} for s in sources],
        "columns": column_conformity(output_paths),
        "previous": compare_with_previous(run_dir, previous_run) if previous_run else None,
    }


# ---------------------------------------------------------------------------
# page
# ---------------------------------------------------------------------------
_TEMPLATE = Path(__file__).with_name("html_report_template.html")


def build_html_report(run_dir, output=None, title=None, previous_run=None) -> Path:
    """Write the interactive report of a curated / catalogue run directory; return its path."""
    run_dir = Path(run_dir)
    payload = build_payload(run_dir, previous_run)
    report_dir = run_dir / "report"
    texs = sorted(report_dir.glob("*.tex")) if report_dir.is_dir() else []
    stem = texs[0].stem if texs else ("S1_curated" if payload["curated"] else "S1_catalogue")
    payload["title"] = title or f"{stem.replace('_', ' ')} {payload['version']} — producer report"
    out = Path(output) if output else report_dir / f"{stem}_{payload['version']}.html"
    data = json.dumps(payload, separators=(",", ":")).replace("</", "<\\/")
    page = (_TEMPLATE.read_text(encoding="utf-8")
            .replace("__TITLE__", html.escape(payload["title"]))
            .replace("__PLOTLY__", PLOTLY_JS)
            .replace("__PAYLOAD__", data))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--run-dir", type=Path, required=True,
                        help="curated or catalogue run directory (manifest.json, recipe.toml)")
    parser.add_argument("--previous-run", type=Path, default=None,
                        help="run directory of the previous version (N-1), for the comparison")
    parser.add_argument("--output", type=Path, default=None,
                        help="HTML file (default: <run-dir>/report/<stem>_<version>.html)")
    parser.add_argument("--title", default=None)
    args = parser.parse_args(argv)
    out = build_html_report(args.run_dir, args.output, args.title, args.previous_run)
    print(f"HTML:   {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
