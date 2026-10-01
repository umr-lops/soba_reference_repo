"""Shared report assets and PDF compilation for recipe-driven datasets."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from importlib.metadata import PackageNotFoundError, version

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
ASSET_DIR = Path(__file__).resolve().parent / "assets"
if not ASSET_DIR.is_dir():
    ASSET_DIR = PACKAGE_ROOT / "assets"


def library_version() -> str:
    """Record a checkout revision or an installed distribution version."""
    repository = Path(__file__).resolve().parents[2]
    try:
        described = subprocess.run(
            ["git", "-C", str(repository), "describe", "--tags", "--always", "--dirty"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        if described.returncode == 0 and described.stdout.strip():
            return f"commit {described.stdout.strip()}"
    except OSError:
        pass
    try:
        return f"version {version('soba_reference_repo')}"
    except PackageNotFoundError:
        return "unknown"


def stage_latex_assets(latex_dir: Path, output_dir: Path) -> list[Path]:
    """Copy companion files next to a report's TeX file."""
    latex_dir, output_dir = Path(latex_dir), Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    staged = []
    for name in ("soba.sty", "logo_soba.png"):
        source = latex_dir / name
        if not source.is_file():
            raise FileNotFoundError(f"missing LaTeX asset: {source}")
        target = output_dir / name
        target.write_bytes(source.read_bytes())
        staged.append(target)
    return staged


def _path(value: str) -> str:
    """Wrap a file name so long TeX names can break."""
    return "\\path|" + value + "|"


def find_pdflatex(miktex_bin: str | Path | None = None) -> str:
    """Locate pdflatex on PATH or in a supplied directory."""
    if miktex_bin:
        directory = Path(miktex_bin)
        for name in ("pdflatex", "pdflatex.exe"):
            candidate = directory / name
            if candidate.is_file():
                return str(candidate)
        raise FileNotFoundError(f"no pdflatex in {directory}")
    binary = shutil.which("pdflatex") or shutil.which("pdflatex.exe")
    if binary is None:
        raise FileNotFoundError(
            "pdflatex is not on PATH; install TeX Live or MiKTeX, or set $MIKTEX_BIN"
        )
    return binary


def compile_pdf(output_dir: Path, stem: str, miktex_bin: str | Path | None = None) -> Path:
    """Run pdflatex twice from the build directory; require a PDF."""
    output_dir = Path(output_dir)
    pdflatex = find_pdflatex(miktex_bin)
    for _ in range(2):
        completed = subprocess.run(
            [pdflatex, "-interaction=nonstopmode", "-halt-on-error", f"{stem}.tex"],
            cwd=output_dir, capture_output=True, text=True, check=False,
        )
        if completed.returncode != 0:
            log = output_dir / f"{stem}.log"
            detail = (
                log.read_text(errors="replace")[-4000:]
                if log.is_file() else completed.stdout[-4000:]
            )
            raise RuntimeError(f"pdflatex failed for {stem}.tex:\n{detail}")
    pdf = output_dir / f"{stem}.pdf"
    if not pdf.is_file():
        raise RuntimeError(f"pdflatex reported success but {pdf} is missing")
    return pdf


def purge_latex_byproducts(output_dir: Path, stem: str) -> list[Path]:
    """Remove only compiler scratch files; retain TeX, figures and assets."""
    output_dir = Path(output_dir)
    removed = []
    for suffix in (".aux", ".log", ".out", ".toc"):
        candidate = output_dir / f"{stem}{suffix}"
        if candidate.is_file():
            candidate.unlink()
            removed.append(candidate)
    return removed
