"""Write the shields.io endpoint files of the coverage and test-count badges.

usage: python write_badges.py coverage.json junit.xml OUT_DIR
coverage.json comes from ``pytest --cov-report=json``, junit.xml from ``pytest --junitxml``.
"""

import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def colour(percent: float) -> str:
    for limit, name in ((90, "brightgreen"), (80, "green"), (70, "yellowgreen"), (60, "yellow")):
        if percent >= limit:
            return name
    return "red"


def main(coverage_json, junit_xml, out_dir) -> None:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    percent = json.loads(Path(coverage_json).read_text())["totals"]["percent_covered"]
    suites = ET.parse(junit_xml).getroot()
    suites = [suites] if suites.tag == "testsuite" else list(suites)
    tests = sum(int(s.get("tests", 0)) for s in suites)
    bad = sum(int(s.get("failures", 0)) + int(s.get("errors", 0)) for s in suites)
    skipped = sum(int(s.get("skipped", 0)) for s in suites)
    badges = {
        "coverage.json": {"label": "coverage", "message": f"{percent:.0f}%",
                          "color": colour(percent)},
        "tests.json": {"label": "tests", "message": f"{tests - skipped - bad} passed"
                       + (f", {bad} failed" if bad else ""),
                       "color": "red" if bad else "brightgreen"},
    }
    for name, badge in badges.items():
        (out / name).write_text(json.dumps({"schemaVersion": 1, **badge}) + "\n")
        print(name, badge["message"])


if __name__ == "__main__":
    main(*sys.argv[1:4])
