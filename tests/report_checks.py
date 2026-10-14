"""Shared checks for manually importing a generated report."""

from pathlib import Path


def assert_report_layout(manifest):
    tex_path = Path(manifest["report_tex"])
    assets_dir = tex_path.parent / f"images_{tex_path.stem}"
    figures = [Path(path) for path in manifest["figures"].values()]
    tex = tex_path.read_text()
    assert len(figures) == 3
    assert assets_dir.is_dir()
    assert set(assets_dir.iterdir()) == {
        *figures, assets_dir / "soba.sty", assets_dir / "logo_soba.png"
    }
    assert rf"\usepackage{{{assets_dir.name}/soba}}" in tex
    assert rf"\graphicspath{{{{{assets_dir.name}/}}}}" in tex
    for figure in figures:
        assert figure.parent == assets_dir
        assert figure.is_file()
        assert figure.relative_to(tex_path.parent).as_posix() in tex
    assert not list(tex_path.parent.glob("*.png"))
    assert not (tex_path.parent / "soba.sty").exists()
