"""Compile the manuscript with project settings and verify its PDF output."""

from pathlib import Path
import shutil
import subprocess


def compile_pdf(project, *, min_pdf_bytes=100):
    """Compile the current manuscript using project configuration and verify its PDF."""
    project = Path(project).resolve()
    source = project / "paper/main.tex"
    if not source.is_file() or source.stat().st_size == 0:
        raise RuntimeError("required artifact is missing or empty: paper/main.tex")
    if shutil.which("latexmk") is None:
        raise RuntimeError(
            "latexmk is not installed; install a LaTeX distribution before paper_compile"
        )
    cwd = project if (project / ".latexmkrc").is_file() else source.parent
    command = ["latexmk", "-pdf", "-recorder", "-interaction=nonstopmode", "-halt-on-error"]
    if cwd == project:
        command.extend(["-outdir=paper", "paper/main.tex"])
    else:
        command.append("main.tex")
    completed = subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )
    if completed.returncode:
        raise RuntimeError(
            f"latexmk failed: {(completed.stdout + completed.stderr)[-3000:]}"
        )
    pdf = source.with_suffix(".pdf")
    if not pdf.is_file() or pdf.stat().st_size < min_pdf_bytes:
        raise RuntimeError("latexmk completed but paper/main.pdf is missing or invalid")
    with pdf.open("rb") as stream:
        if stream.read(5) != b"%PDF-":
            raise RuntimeError("latexmk completed but paper/main.pdf is not a PDF")
    return pdf
