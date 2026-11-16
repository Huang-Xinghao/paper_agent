"""One cached MinerU parse for full paper text and all grouped figures."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import zipfile

from paper_agent.tools.paper_preprocessing import document_from_structured

VERSION = "architecture-captions-v1"
READER_VERSION = "mineru-full-paper-v2"


def runtime_python() -> str | None:
    configured = os.environ.get("PAPER_AGENT_MINERU_PYTHON")
    if configured:
        if not Path(configured).is_file():
            raise ValueError(f"MinerU Python does not exist: {configured}")
        return configured
    executable = shutil.which("mineru-kit")
    if executable:
        sibling = Path(executable).parent / "python"
        if sibling.is_file():
            return str(sibling)
    return None


def pages_from_structured(data: dict) -> list[dict]:
    """Preserve MinerU reading order, physical pages, formulas, tables and captions."""
    return document_from_structured(data)["pages"]


def read_mineru_paper(project: Path, pdf: Path, *, on_progress=None, role='idea_generator') -> dict:
    from paper_agent.integrations.progress import OperationProgress
    with OperationProgress(project, 'pdf_parse', pdf, role=role, on_progress=on_progress).running() as progress:
        return _read_mineru_paper(project, pdf, progress)


def _read_mineru_paper(project: Path, pdf: Path, progress) -> dict:
    """Read the entire PDF and grouped figures from one required MinerU parse."""
    python = runtime_python()
    if not python:
        raise RuntimeError("MinerU is required: set PAPER_AGENT_MINERU_PYTHON to its environment's Python.")
    env = os.environ.copy()
    env.setdefault("MINERU_MODEL_SMALL_BACKEND", "onnx")
    env.setdefault("MINERU_MODEL_VLM_ENGINE", "llama-cpp")
    env.setdefault("MINERU_MODEL_SOURCE", "local")
    timeout = float(env.get("PAPER_AGENT_MINERU_TIMEOUT", "1200"))
    progress.update('checking_parser')
    version = subprocess.run(
        [python, "-c", "from importlib.metadata import version; print(version('mineru'))"],
        env=env, capture_output=True, text=True, check=True, timeout=30).stdout.strip()
    sha = hashlib.sha256(pdf.read_bytes()).hexdigest()
    settings = [VERSION, version, env.get("MINERU_HOME"),
                env.get("MINERU_MODEL_SMALL_BACKEND"), env.get("MINERU_MODEL_VLM_ENGINE")]
    key = hashlib.sha256(json.dumps(settings).encode()).hexdigest()[:16]
    cache = project / ".paper-agent" / "pdf-figures" / sha / key
    report_path = cache / "grouped" / "groups.json"
    structured_path = cache / "structured_content.json"
    if not report_path.exists() or not structured_path.exists():
        cache.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=cache.parent) as tmp:
            work = Path(tmp)
            archive = work / "parse.zip"
            command = [str(Path(python).parent / "mineru-kit"), "parse", str(pdf),
                       "-o", str(archive), "--format", "zip", "--tier", "standard"]
            try:
                progress.update('parsing_pdf')
                subprocess.run(command, env=env, capture_output=True, text=True,
                               check=True, timeout=timeout)
                # Read only the structured content; never extract arbitrary archive paths.
                progress.update('reading_structure')
                with zipfile.ZipFile(archive) as bundle:
                    names = [n for n in bundle.namelist() if Path(n).name == "structured_content.json"]
                    if len(names) != 1:
                        raise ValueError("MinerU output must contain one structured_content.json")
                    (work / "structured_content.json").write_bytes(bundle.read(names[0]))
                pages_from_structured(json.loads((work / "structured_content.json").read_text(encoding="utf-8")))
                progress.update('grouping_figures')
                subprocess.run([python, str(Path(__file__).with_name("figure_grouping.py")), "--pdf", str(pdf),
                                "--input", str(work / "structured_content.json"),
                                "--output", str(work / "grouped")], env=env,
                               capture_output=True, text=True, check=True, timeout=timeout)
            except subprocess.CalledProcessError as exc:
                raise RuntimeError(f"MinerU paper parsing failed: {exc.stderr[-2000:]}") from exc
            archive.unlink()
            # Publish complete results only; an interrupted run is never a cache hit.
            cache.mkdir(exist_ok=True)
            shutil.copytree(work, cache, dirs_exist_ok=True)
    else:
        progress.update('using_cached_parse')
    progress.update("preparing_sources")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    document = document_from_structured(
        json.loads(structured_path.read_text(encoding="utf-8")), pdf_sha256=sha,
        parser_identity={"name": "MinerU", "version": version,
                         "reader_version": READER_VERSION, "settings_key": key})
    return {"status": "completed", "pages": document["pages"], "document": document,
            "reader_version": READER_VERSION,
            "pdf_sha256": sha, "mineru_version": version,
            "extraction_version": VERSION, "cache": str(cache / "grouped"),
            "total_figures": len(report["figures"]), "figures": report["figures"],
            "page_diagnostics": report["page_diagnostics"]}


def save_architecture_figures(root: Path, slug: str, extraction: dict) -> list[str]:
    """Save independent image/caption pairs under the source paper's identifier."""
    if not re.fullmatch(r"[\w.-]+", slug, re.UNICODE) or slug in {".", ".."}:
        raise ValueError("invalid paper slug")
    directory = root / "figures" / slug
    directory.mkdir(parents=True, exist_ok=True)
    outputs = []
    for figure in extraction["figures"]:
        source = Path(extraction["cache"]) / figure["image"]
        name = Path(figure["image"]).name
        image = directory / name
        shutil.copyfile(source, image)
        caption = directory / (image.stem + ".md")
        lines = [f"# Figure {figure['number']}", "",
                 f"Source: paper:{slug}", f"PDF page: {figure['page']}", "",
                 figure["caption"], ""]
        if figure["review_required"]:
            lines += ["Review required: " + ", ".join(figure["review_reasons"]), ""]
        caption.write_text("\n".join(lines), encoding="utf-8")
        outputs.extend([str(image), str(caption)])
    return outputs
