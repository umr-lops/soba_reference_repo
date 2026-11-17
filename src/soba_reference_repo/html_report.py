"""Self-contained HTML companion of the curated / catalogue PDF documents.

Built from what a curated (``soba_curate_dataset``) or catalogue
(``soba_catalogue_report``) run directory already holds, without new
dependencies:

* ``manifest.json``: sources, input / curated rows and, for a curated run, the
  sequential filter steps (rule, clauses, before / removed / remaining);
* ``recipe.toml``: version and production date;
* the generated ``report/<stem>.tex``: section titles and the figures with
  their captions, in document order (the PNG files are embedded as base64,
  so the page is a single file);
* the Parquet schemas of the output files: column names and types.

It is not a mirror of the PDF text: it gives the numbers and figures that are
convenient to browse and compare between versions.
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import re
import tomllib
from pathlib import Path

import pyarrow.parquet as pq

_CSS = """
:root { --fg:#1d2433; --muted:#5b6475; --line:#d9dee7; --head:#eef2f8; --accent:#0d47a1;
        --bg:#fff; }
* { box-sizing:border-box; }
body { margin:0; padding:24px clamp(16px,4vw,48px); font:15px/1.5 system-ui,-apple-system,
       "Segoe UI",Roboto,sans-serif; color:var(--fg); background:var(--bg); max-width:1200px; }
h1 { font-size:1.6em; margin:0 0 4px; } h2 { margin-top:2em; border-bottom:2px solid var(--line);
     padding-bottom:4px; } h3 { margin-top:1.5em; color:var(--accent); }
.meta { color:var(--muted); margin-bottom:1em; }
.scroll { overflow-x:auto; margin:8px 0 16px; }
table { border-collapse:collapse; font-size:.9em; }
th, td { border:1px solid var(--line); padding:4px 8px; text-align:left; vertical-align:top; }
th { background:var(--head); } td.num { text-align:right; font-variant-numeric:tabular-nums; }
figure { margin:16px 0 24px; }
figure img { max-width:100%; height:auto; border:1px solid var(--line); }
figcaption { color:var(--muted); font-size:.9em; margin-top:4px; }
code { font-size:.9em; background:var(--head); padding:0 3px; border-radius:3px; }
nav ul { columns:2; padding-left:18px; } @media (max-width:700px) { nav ul { columns:1; } }
"""

_SECTION_RE = re.compile(r"\\(section|subsection)\*?\{")
_GRAPHICS_RE = re.compile(r"\\includegraphics(?:\[[^\]]*\])?\{([^}]+)\}")
_CAPTION_RE = re.compile(r"\\caption(?:\[[^\]]*\])?\{")


def _braced(text: str, start: int) -> tuple[str, int]:
    """Content of the brace group opening just before ``start``, and the end index."""
    depth, i = 1, start
    while i < len(text) and depth:
        depth += {"{": 1, "}": -1}.get(text[i], 0)
        i += 1
    return text[start:i - 1], i


def latex_to_text(value: str) -> str:
    """Plain text from the small LaTeX subset used in the SOBA captions / titles."""
    value = re.sub(r"\\label\{[^}]*\}", "", value)
    value = re.sub(r"\\(?:textbf|textit|emph|texttt|mathrm|text)\{([^{}]*)\}", r"\1", value)
    value = value.replace("\\\\", " ").replace("~", " ").replace("$", "")
    value = re.sub(r"\\([_%&#{}$])", r"\1", value)
    value = re.sub(r"\\(?:ge|geq)\b", "≥", value)
    value = re.sub(r"\\(?:le|leq)\b", "≤", value)
    value = re.sub(r"\\[a-zA-Z]+\*?", "", value)
    return re.sub(r"\s+", " ", value.replace("{", "").replace("}", "")).strip()


def parse_tex(tex: str) -> list[dict]:
    """Ordered ``section`` / ``figure`` items of a generated SOBA document."""
    items = []
    events = []
    for match in _SECTION_RE.finditer(tex):
        title, end = _braced(tex, match.end())
        events.append((match.start(), {"kind": match.group(1), "title": latex_to_text(title)}))
    for match in _GRAPHICS_RE.finditer(tex):
        path = match.group(1)
        if "logo" in Path(path).name:
            continue
        caption = ""
        # the caption must belong to the same figure: before its \end{figure} and the next image
        end = tex.find("\\end{figure", match.end())
        end = len(tex) if end < 0 else end
        cap = _CAPTION_RE.search(tex, match.end(), end)
        nxt = _GRAPHICS_RE.search(tex, match.end(), end)
        if cap and (nxt is None or cap.start() < nxt.start()):
            caption = latex_to_text(_braced(tex, cap.end())[0])
        events.append((match.start(), {"kind": "figure", "path": path, "caption": caption}))
    for _, item in sorted(events, key=lambda event: event[0]):
        items.append(item)
    # drop the headings without any figure below them (tables of the PDF are not reproduced)
    kept: list[dict] = []
    for index, item in enumerate(items):
        if item["kind"] == "figure":
            kept.append(item)
            continue
        level = 0 if item["kind"] == "section" else 1
        for following in items[index + 1:]:
            if following["kind"] == "figure":
                kept.append(item)
                break
            if (0 if following["kind"] == "section" else 1) <= level:
                break
    return kept


def _image_uri(report_dir: Path, path: str) -> str | None:
    candidates = [report_dir / path, *report_dir.glob(f"images_*/{Path(path).name}")]
    for candidate in candidates:
        if candidate.is_file():
            data = base64.b64encode(candidate.read_bytes()).decode()
            return f"data:image/png;base64,{data}"
    return None


def _database(source) -> str:
    name = Path(source.get("curated_path") or source["path"]).name
    return "CCI" if "CCI" in name else ("CMEMS" if "CMEMS" in name else "")


def _clause_text(clause) -> str:
    column, operation = clause[0], clause[1]
    operand = clause[2] if len(clause) > 2 else ""
    return f"{column} {operation} {json.dumps(operand)}".strip()


def _table(header, rows, numeric=()) -> str:
    head = "".join(f"<th>{html.escape(str(cell))}</th>" for cell in header)
    body = []
    for row in rows:
        cells = []
        for index, cell in enumerate(row):
            cls = ' class="num"' if index in numeric else ""
            text = f"{cell:,}" if isinstance(cell, int) else html.escape(str(cell))
            cells.append(f"<td{cls}>{text}</td>")
        body.append("<tr>" + "".join(cells) + "</tr>")
    return (f'<div class="scroll"><table><thead><tr>{head}</tr></thead>'
            f'<tbody>{"".join(body)}</tbody></table></div>')


def _columns(sources) -> list[tuple[str, str, str]]:
    seen: dict[str, tuple[str, set]] = {}
    for source in sources:
        path = Path(source.get("curated_path") or source["path"])
        if not path.is_file():
            continue
        for field in pq.read_schema(path):
            kind, owners = seen.setdefault(field.name, (str(field.type), set()))
            owners.add(source["mission"])
    return [(name, kind, ", ".join(sorted(owners))) for name, (kind, owners) in seen.items()]


def build_html_report(run_dir, output=None, title=None) -> Path:
    """Write the HTML companion of a curated / catalogue run directory; return its path."""
    run_dir = Path(run_dir)
    manifest = json.loads((run_dir / "manifest.json").read_text())
    recipe = {}
    if (run_dir / "recipe.toml").is_file():
        with (run_dir / "recipe.toml").open("rb") as stream:
            recipe = tomllib.load(stream)
    report_dir = run_dir / "report"
    texs = sorted(report_dir.glob("*.tex")) if report_dir.is_dir() else []
    stem = texs[0].stem if texs else "report"
    sources = sorted(manifest.get("sources", []), key=lambda s: (s["mission"], s.get("path", "")))
    curated = any(source.get("steps") for source in sources)
    version = recipe.get("version", "?")
    date = str(recipe.get("production_date", ""))
    title = title or f"{stem.replace('_', ' ')} {version}"
    # named like the versioned PDF (``<stem>_<version>.pdf``)
    out = Path(output) if output else report_dir / f"{stem}_{version}.html"

    parts = [f"<title>{html.escape(title)}</title><style>{_CSS}</style>",
             f"<h1>{html.escape(title)}</h1>",
             f'<div class="meta">version {html.escape(str(version))} · production date '
             f"{html.escape(date)} · reference {html.escape(str(manifest.get('reference', '')))} · "
             f"{manifest.get('curated_rows', 0):,} rows"
             + (f" (of {manifest.get('input_rows', 0):,} before curation)" if curated else "")
             + f" · companion of <code>{html.escape(stem)}.pdf</code></div>"]
    items = parse_tex(texs[0].read_text(encoding="utf-8")) if texs else []
    toc = ['<li><a href="#files">Files</a></li>']
    if curated:
        toc.append('<li><a href="#filters">Filter cascade</a></li>')
    toc += [f'<li><a href="#s{i}">{html.escape(item["title"])}</a></li>'
            for i, item in enumerate(items) if item["kind"] == "section"]
    toc.append('<li><a href="#columns">Columns</a></li>')
    parts.append("<nav><ul>" + "".join(toc) + "</ul></nav>")

    parts.append('<h2 id="files">Files</h2>')
    parts.append(_table(
        ["Mission", "File", "Input rows", "Rows"] if curated else ["Mission", "File", "Rows"],
        [([s["mission"], Path(s.get("curated_path") or s["path"]).name, s["input_rows"],
           s["curated_rows"]] if curated else
          [s["mission"], Path(s["path"]).name, s["curated_rows"]]) for s in sources],
        numeric=(2, 3) if curated else (2,)))

    if curated:
        parts.append('<h2 id="filters">Filter cascade</h2>')
        parts.append("<p>Rules applied in order to each catalogue (a row is kept when all the "
                     "clauses of the rule are true).</p>")
        rule_ids = list(dict.fromkeys(step["id"] for s in sources for step in s.get("steps", [])))
        summary = []
        for rule_id in rule_ids:
            row = [rule_id]
            for source in sources:
                step = next((x for x in source.get("steps", []) if x["id"] == rule_id), None)
                row.append(step["removed"] if step else "–")
            summary.append(row)
        labels = [f"{s['mission']} {_database(s)}" for s in sources]
        parts.append("<h3>Rows removed per rule</h3>")
        parts.append(_table(["Rule", *labels], summary, numeric=tuple(range(1, len(labels) + 1))))
        for source, label in zip(sources, labels):
            parts.append(f"<h3>{html.escape(label)}</h3>")
            parts.append(_table(
                ["Rule", "Name", "Clauses", "Before", "Removed", "Remaining"],
                [[x["id"], x.get("name", ""),
                  "; ".join(_clause_text(c) for c in x.get("clauses", [])),
                  x.get("before", ""), x.get("removed", ""), x.get("remaining", "")]
                 for x in source.get("steps", [])], numeric=(3, 4, 5)))

    for index, item in enumerate(items):
        if item["kind"] == "section":
            parts.append(f'<h2 id="s{index}">{html.escape(item["title"])}</h2>')
        elif item["kind"] == "subsection":
            parts.append(f"<h3>{html.escape(item['title'])}</h3>")
        else:
            uri = _image_uri(report_dir, item["path"])
            if uri:
                parts.append(f'<figure><img src="{uri}" alt="{html.escape(item["caption"])}">'
                             f"<figcaption>{html.escape(item['caption'])}</figcaption></figure>")

    parts.append('<h2 id="columns">Columns</h2>')
    parts.append(_table(["Column", "Type", "Present in"], _columns(sources)))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("<!doctype html><meta charset='utf-8'>"
                   "<meta name='viewport' content='width=device-width, initial-scale=1'>"
                   + "\n".join(parts) + "\n", encoding="utf-8")
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--run-dir", type=Path, required=True,
                        help="curated or catalogue run directory "
                             "(manifest.json, recipe.toml, report/)")
    parser.add_argument("--output", type=Path, default=None,
                        help="HTML file (default: <run-dir>/report/<stem>_<version>.html)")
    parser.add_argument("--title", default=None)
    args = parser.parse_args(argv)
    print(f"HTML:   {build_html_report(args.run_dir, args.output, args.title)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
