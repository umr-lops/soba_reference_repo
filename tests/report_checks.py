"""Shared checks for manually importing a generated report."""

import re
from pathlib import Path

import pyarrow.parquet as pq


def assert_report_layout(manifest):
    tex_path = Path(manifest["report_tex"])
    assets_dir = tex_path.parent / f"images_{tex_path.stem}"
    figures = [Path(path) for path in manifest["figures"].values()]
    tex = tex_path.read_text()
    headings = re.findall(r"\\(?:section\*?|subsection)\{([^}]+)\}", tex)
    distribution = ("SWOT Significant Wave-Height Distribution"
                    if manifest.get("reference") == "swot" else
                    "HSCAT Wind-Direction Distribution"
                    if manifest.get("reference_variable") == "winddirection" else
                    "HSCAT Wind-Speed Distribution")
    assert headings == [
        "Version History", "Dataset Content Description", "Global Coverage Map",
        "Monthly Distribution of Matchups", distribution,
        "Catalogue Columns", "Global Metadata", "Dataset Production Steps",
        "Selected Products", "Selection of Tiles / Filtered Co-locations",
    ]
    assert tex.count(r"\begin{titlepage}") == 1
    assert tex.count(r"\tableofcontents") == 1
    assert tex.count(r"\begin{thebibliography}") == 1
    assert "Checked by" in tex
    assert tex.count(r"\bibitem{") == 1
    assert r"\begin{longtable}" not in tex
    assert tex.index("Sequential quality-filter counts") < tex.index("Matches before and after")
    assert "Summary statistics for ref" not in tex
    assert "docs.google.com" not in tex
    assert "Libraries and Commands Used" not in tex
    assert "Configuration Files" not in tex
    assert "Copernicus/ESA Sentinel-1 Level-2 Ocean" in tex
    prose = re.sub(r"(?m)^%.*$", "", tex)
    before, rest = prose.split(r"\subsection{Global Metadata}")
    metadata, after = rest.split(r"\section{Dataset Production Steps}")
    assert "coaligned_catalogue" not in before + after
    assert "co-aligned" not in before + after
    assert "Matches before and after the additional selection" in tex
    assert "source catalogue" in metadata
    assert "report_provenance" in manifest
    assert "Sequential quality-filter counts by mission" in tex
    assert "soba.ifremer.fr/documents/dataset_description/" in tex
    assert "sobadataflow.png" in tex
    columns = tex.split(r"\label{tab:columns}", 1)[1].split(r"\end{tabular}", 1)[0]
    schema_rows = [line.split(" & ") for line in columns.splitlines()
                   if " & " in line and not line.lstrip().startswith(r"\textbf{")]
    schema = pq.read_schema(manifest["test_parquet"])
    assert [row[0].replace(r"\_", "_").strip() for row in schema_rows] == schema.names
    assert [row[1].replace(r"\_", "_").strip() for row in schema_rows] == [
        str(field.type) for field in schema
    ]
    assert len(figures) == 3
    assert assets_dir.is_dir()
    assert set(assets_dir.iterdir()) == {
        *figures, assets_dir / "soba.sty", assets_dir / "logo_soba.png",
        assets_dir / "sobadataflow.png"
    }
    assert r"\input{" + assets_dir.name + "/soba.sty}" in tex
    assert r"\graphicspath{{" + assets_dir.name + "/}}" in tex
    for figure in figures:
        assert figure.parent == assets_dir
        assert figure.is_file()
        assert rf"{assets_dir.name}/{figure.name}" in tex
    assert not list(tex_path.parent.glob("*.png"))
    assert not (tex_path.parent / "soba.sty").exists()
