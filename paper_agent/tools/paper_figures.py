"""One cached MinerU parse for full paper text and architecture figures."""
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

VERSION = "architecture-captions-v1"
READER_VERSION = "mineru-full-paper-v1"
ARCHITECTURE = re.compile(
    r"\b(?:architectur\w*|workflow|flowchart|schematic|pipeline)\b"
    r"|\b(?:overall|proposed|model|network|methodological)\s+framework\b"
    r"|\b(?:framework|methodology|model)\s+overview\b"
    r"|\boverview\s+of\s+(?:the\s+)?(?:proposed\s+)?(?:model|method|framework)\b"
    r"|架构|流程图|框架图|总体框架|方法概览|模型结构", re.I)


def architecture_reason(caption: str) -> str | None:
    # Inspect the introductory description, not performance claims in later panels.
    lead = re.sub(r"^(?:fig(?:ure)?\.?\s*)S?\d+\s*[|:.．–—-]?\s*", "", caption, flags=re.I)
    lead = re.split(r"\.\s|\s[ab]\s|。", lead, maxsplit=1)[0][:250]
    match = ARCHITECTURE.search(lead)
    return match.group() if match else None


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
    if data.get("is_full_document") is False:
        raise ValueError("MinerU returned a partial document")
    source_pages = data["pages"]
    page_count = data.get("metadata", {}).get("document", {}).get("page_count")
    if page_count is not None and page_count != len(source_pages):
        raise ValueError("MinerU page count does not match the full PDF")
    if not source_pages or [p["page_idx"] for p in source_pages] != list(range(len(source_pages))):
        raise ValueError("MinerU output has missing or unordered PDF pages")

    def text_parts(block):
        content = block.get("content", "")
        if not isinstance(content, str):
            raise ValueError("Unsupported MinerU block content; expected text")
        if content.strip():
            yield content.strip()
        for field in ("captions", "footnotes"):
            for child in block.get(field, []):
                yield from text_parts(child)

    pages = [{"page": p["page_idx"] + 1,
              "text": "\n\n".join(part for b in p["blocks"] for part in text_parts(b))}
             for p in source_pages]
    if not any(p["text"].strip() for p in pages):
        raise ValueError("MinerU returned no readable paper content")
    return pages


def read_mineru_paper(project: Path, pdf: Path) -> dict:
    """Read the entire PDF and grouped figures from one required MinerU parse."""
    python = runtime_python()
    if not python:
        raise RuntimeError("MinerU is required: set PAPER_AGENT_MINERU_PYTHON to its environment's Python.")
    env = os.environ.copy()
    env.setdefault("MINERU_MODEL_SMALL_BACKEND", "onnx")
    env.setdefault("MINERU_MODEL_VLM_ENGINE", "llama-cpp")
    env.setdefault("MINERU_MODEL_SOURCE", "local")
    timeout = float(env.get("PAPER_AGENT_MINERU_TIMEOUT", "1200"))
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
                subprocess.run(command, env=env, capture_output=True, text=True,
                               check=True, timeout=timeout)
                # Read only the structured content; never extract arbitrary archive paths.
                with zipfile.ZipFile(archive) as bundle:
                    names = [n for n in bundle.namelist() if Path(n).name == "structured_content.json"]
                    if len(names) != 1:
                        raise ValueError("MinerU output must contain one structured_content.json")
                    (work / "structured_content.json").write_bytes(bundle.read(names[0]))
                pages_from_structured(json.loads((work / "structured_content.json").read_text(encoding="utf-8")))
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
    report = json.loads(report_path.read_text(encoding="utf-8"))
    pages = pages_from_structured(json.loads(structured_path.read_text(encoding="utf-8")))
    selected = []
    for figure in report["figures"]:
        reason = architecture_reason(figure["caption"])
        if reason:
            selected.append({**figure, "selection_reason": reason})
    return {"status": "completed", "pages": pages, "reader_version": READER_VERSION,
            "pdf_sha256": sha, "mineru_version": version,
            "extraction_version": VERSION, "cache": str(cache / "grouped"),
            "total_figures": len(report["figures"]), "figures": selected,
            "page_diagnostics": report["page_diagnostics"]}


def save_architecture_figures(root: Path, slug: str, extraction: dict) -> list[str]:
    """Save independent image/caption pairs under the source paper's identifier."""
    if not re.fullmatch(r"[\w.-]+", slug, re.UNICODE) or slug in {".", ".."}:
        raise ValueError("invalid paper slug")
    directory = root / "figures" / slug
    directory.mkdir(parents=True, exist_ok=True)
    outputs = []
    for figure in extraction["figures"]:
        if not figure.get("image"):
            continue
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
