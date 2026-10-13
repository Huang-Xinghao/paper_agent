"""Deterministic literature tools used by the literature workflow phase."""

from __future__ import annotations

import json
import re
import time
import tempfile
import urllib.error
from difflib import SequenceMatcher
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from .core import ToolContext, ToolResult, tool


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def _words(text: str) -> set[str]:
    return {word.lower() for word in re.findall(r"[A-Za-z0-9][A-Za-z0-9_-]{2,}", text)}


@tool(read_only=True)
def discover_local_papers(ctx: ToolContext, query: str = "", library_path: str | None = None,
                          max_papers: int = 20) -> ToolResult:
    """Find local PDF papers in papers/, literature/, or a configured workspace path.

    Returns paths and lightweight filename relevance scores. Use read_local_paper for content.
    """
    roots: list[Path] = []
    if library_path:
        roots.append(ctx.resolve(library_path))
    roots.extend(ctx.resolve(name) for name in ("papers", "literature"))
    seen: set[Path] = set()
    candidates: list[dict[str, Any]] = []
    query_words = _words(query)
    for root in roots:
        if not root.is_dir():
            continue
        for path in root.rglob("*.pdf"):
            path = path.resolve()
            if path in seen:
                continue
            seen.add(path)
            name_words = _words(path.stem)
            score = len(query_words & name_words)
            candidates.append({"path": str(path.relative_to(ctx.workspace.resolve())),
                               "filename": path.name, "filename_score": score})
    candidates.sort(key=lambda item: (-item["filename_score"], item["filename"].lower()))
    limit = max(1, min(max_papers, 100))
    payload = {"query": query, "roots_checked": [str(root) for root in roots],
               "found": len(candidates), "papers": candidates[:limit]}
    if not candidates:
        payload["warning"] = ("No PDFs found in papers/, literature/, or the configured library path.")
    return ToolResult(_json(payload), metadata={"count": len(candidates)})


@tool(read_only=True)
def read_local_paper(ctx: ToolContext, path: str, max_pages: int = 3) -> ToolResult:
    """Extract the first pages of a local PDF for title, abstract, and introduction screening."""
    target = ctx.resolve(path)
    if not target.is_file() or target.suffix.lower() != ".pdf":
        return ToolResult(f"PDF not found: {path}", True, {"error_code": "PDF_NOT_FOUND"})
    try:
        from pypdf import PdfReader
    except ImportError:
        return ToolResult("Reading PDFs requires the optional 'pypdf' package", True,
                          {"error_code": "PDF_READER_UNAVAILABLE"})
    try:
        reader = PdfReader(str(target))
        pages = max(1, min(max_pages, 10))
        text = "\n\n".join((reader.pages[index].extract_text() or "")
                             for index in range(min(pages, len(reader.pages))))
    except Exception as exc:
        return ToolResult(f"Could not extract {path}: {exc}", True, {"error_code": "PDF_READ_FAILED"})
    return ToolResult(_json({"path": path, "pages_read": min(pages, len(reader.pages)),
                             "page_count": len(reader.pages), "text": text[:30000]}))


@tool(read_only=True)
def read_paper_pages(ctx: ToolContext, path: str, start_page: int = 1,
                     page_count: int = 5) -> ToolResult:
    """Read numbered PDF pages, allowing a paper to be reviewed beyond its abstract."""
    target = ctx.resolve(path)
    if not target.is_file() or target.suffix.lower() != ".pdf":
        return ToolResult(f"PDF not found: {path}", True, {"error_code": "PDF_NOT_FOUND"})
    if start_page < 1 or page_count < 1:
        return ToolResult("start_page and page_count must be positive", True)
    try:
        from pypdf import PdfReader
    except ImportError:
        return ToolResult("Reading PDFs requires pypdf", True, {"error_code": "PDF_READER_UNAVAILABLE"})
    try:
        reader = PdfReader(str(target))
        total = len(reader.pages)
        if start_page > total:
            return ToolResult(f"start_page {start_page} exceeds {total} pages", True)
        end = min(total, start_page + min(page_count, 10) - 1)
        pages = [{"page": index + 1, "text": (reader.pages[index].extract_text() or "")[:12000]}
                 for index in range(start_page - 1, end)]
    except Exception as exc:
        return ToolResult(f"Could not extract {path}: {exc}", True,
                          {"error_code": "PDF_READ_FAILED"})
    return ToolResult(_json({"path": path, "total_pages": total, "pages": pages,
                             "next_page": end + 1 if end < total else None}))


def _get_text(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "paper-agent/0.1"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            if exc.code not in {429, 502, 503, 504} or attempt == 2:
                raise
            try:
                delay = float(exc.headers.get("Retry-After", 2 ** attempt))
            except (ValueError, TypeError, AttributeError):
                delay = 2 ** attempt
            time.sleep(min(10, max(0, delay)))
    raise RuntimeError("request retries exhausted")


def _get_json(url: str) -> dict[str, Any]:
    return json.loads(_get_text(url))


@tool(read_only=True)
def search_papers(ctx: ToolContext, query: str, source: str = "openalex",
                  max_results: int = 10) -> ToolResult:
    """Search a public paper API and return structured metadata, without downloading PDFs.

    Supported sources are openalex, semantic_scholar, and arxiv. Network failures are returned
    as tool errors so the model can retry with another source.
    """
    limit = max(1, min(max_results, 50))
    encoded = urllib.parse.quote(query)
    if source == "openalex":
        url = f"https://api.openalex.org/works?search={encoded}&per-page={limit}"
        raw = _get_json(url).get("results", [])
        papers = [{"title": item.get("title"), "authors": [a.get("author", {}).get("display_name")
                    for a in item.get("authorships", [])], "year": item.get("publication_year"),
                   "doi": item.get("doi"), "url": item.get("primary_location", {}).get("landing_page_url"),
                   "abstract": None, "source": source} for item in raw]
    elif source == "semantic_scholar":
        fields = "title,authors,year,abstract,externalIds,url,venue"
        url = f"https://api.semanticscholar.org/graph/v1/paper/search?query={encoded}&limit={limit}&fields={fields}"
        raw = _get_json(url).get("data", [])
        papers = [{"title": item.get("title"), "authors": [a.get("name") for a in item.get("authors", [])],
                   "year": item.get("year"), "doi": (item.get("externalIds") or {}).get("DOI"),
                   "url": item.get("url"), "abstract": item.get("abstract"), "venue": item.get("venue"),
                   "source": source} for item in raw]
    elif source == "arxiv":
        url = f"https://export.arxiv.org/api/query?search_query=all:{encoded}&max_results={limit}&sortBy=relevance&sortOrder=descending"
        xml = _get_text(url)
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        try:
            entries = ET.fromstring(xml).findall("atom:entry", ns)
        except ET.ParseError as exc:
            return ToolResult(f"Invalid arXiv API response: {exc}", True)
        papers = []
        for entry in entries:
            get = lambda tag: (entry.findtext(f"atom:{tag}", default="", namespaces=ns)).strip()
            papers.append({"title": re.sub(r"\s+", " ", get("title")),
                           "authors": [author.text or "" for author in entry.findall("atom:author/atom:name", ns)],
                           "year": get("published")[:4], "doi": None, "url": get("id"),
                           "abstract": re.sub(r"\s+", " ", get("summary")), "source": source})
    else:
        return ToolResult(f"Unsupported source: {source}", True, {"error_code": "UNSUPPORTED_SOURCE"})
    return ToolResult(_json({"query": query, "source": source, "count": len(papers), "papers": papers}))


@tool()
def download_arxiv_paper(ctx: ToolContext, url: str) -> ToolResult:
    """Download and cache a verified arXiv PDF inside the project workspace."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"arxiv.org", "export.arxiv.org"}:
        return ToolResult("Not an arXiv paper URL", True)
    match = re.fullmatch(r"/(?:abs|pdf)/((?:\d{4}\.\d{4,5}|[A-Za-z-]+(?:\.[A-Za-z-]+)?/\d{7})(?:v\d+)?)(?:\.pdf)?", parsed.path)
    if not match:
        return ToolResult("Invalid arXiv paper identifier", True)
    paper_id = match.group(1)
    relative = f"papers/arxiv/{paper_id.replace('/', '_')}.pdf"
    target = ctx.resolve(relative, write=True)
    temporary = None
    try:
        if target.is_file():
            with target.open("rb") as stream:
                if stream.read(5) != b"%PDF-":
                    raise ValueError("Cached file is not a PDF")
            return ToolResult(_json({"path": relative, "arxiv_id": paper_id, "status": "cached"}))
        request = urllib.request.Request(f"https://arxiv.org/pdf/{paper_id}",
                                         headers={"User-Agent": "paper-agent/0.1"})
        target.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(request, timeout=30) as response:
            header = response.read(5)
            if header != b"%PDF-":
                raise ValueError("arXiv returned a non-PDF response")
            with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(header)
                while chunk := response.read(1024 * 1024):
                    stream.write(chunk)
        temporary.replace(target)
        return ToolResult(_json({"path": relative, "arxiv_id": paper_id, "status": "downloaded"}))
    except (OSError, ValueError) as exc:
        return ToolResult(f"arXiv download failed: {exc}", True)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


@tool(read_only=True)
def verify_papers(ctx: ToolContext, papers: list[dict], min_year: int | None = None) -> ToolResult:
    """Normalize/de-duplicate metadata and cross-check DOI/title using Crossref.

    accepted means usable metadata, not verified truth: inspect verification on each record.
    Missing DOI/network failures stay explicitly unverified. This does not read full text.
    """
    accepted: list[dict[str, Any]] = []
    seen: set[str] = set()
    rejected: list[dict[str, str]] = []
    for paper in papers:
        title = str(paper.get("title") or "").strip()
        key = re.sub(r"\W+", " ", title.lower()).strip()
        year = paper.get("year")
        if not title:
            rejected.append({"reason": "missing title"}); continue
        if key in seen:
            rejected.append({"title": title, "reason": "duplicate title"}); continue
        if min_year is not None and str(year).isdigit() and int(year) < min_year:
            rejected.append({"title": title, "reason": f"before {min_year}"}); continue
        seen.add(key)
        verification = {"status": "unverified", "reason": "No valid DOI", "full_text_read": False}
        doi = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", str(paper.get("doi") or ""), flags=re.I).strip()
        if re.fullmatch(r"10\.\d{4,9}/\S+", doi):
            try:
                record = _get_json("https://api.crossref.org/works/" + urllib.parse.quote(doi, safe=""))["message"]
                canonical_title = (record.get("title") or [""])[0]
                normalized = lambda t: re.sub(r"\W+", " ", t.casefold()).strip()
                matched = (str(record.get("DOI", "")).casefold() == doi.casefold()
                           and SequenceMatcher(None, normalized(title), normalized(canonical_title)).ratio() >= 0.85)
                verification = {"status": "verified_metadata" if matched else "mismatch",
                                "source": "crossref", "canonical_title": canonical_title,
                                "canonical_doi": record.get("DOI"), "full_text_read": False}
                if not matched:
                    rejected.append({"title": title, "reason": "DOI/title mismatch", "verification": verification})
                    continue
            except Exception as exc:
                verification["reason"] = f"DOI lookup unavailable: {exc}"
        accepted.append({"verification": verification, "title": title, "authors": paper.get("authors") or [], "year": year,
                         "doi": paper.get("doi"), "url": paper.get("url"), "abstract": paper.get("abstract"),
                         "source": paper.get("source")})
    return ToolResult(_json({"accepted": accepted, "rejected": rejected,
                             "accepted_count": len(accepted), "rejected_count": len(rejected)}))
