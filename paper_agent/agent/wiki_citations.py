"""PDF-grounded bibliography records and deterministic paper citation matching."""

from __future__ import annotations

import json
import re
import tempfile
import unicodedata
from pathlib import Path
from typing import Any

from paper_agent.tools import research_wiki
from .wiki_relations import write_relations_batch
from .wiki_prompts import PROMPT_VERSION as ANALYSIS_VERSION


CITATION_ORIGIN = "wiki-agent-citations-v1"


def _key(value: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKC", value).casefold() if c.isalnum())


def _doi(value: str | None) -> str:
    if value in (None, "null", "None"):
        return ""
    return re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "",
                  (value or "").strip().casefold()).rstrip(".,;")


def _arxiv(value: str | None) -> str:
    if value in (None, "null", "None"):
        return ""
    value = re.sub(r"^(?:https?://arxiv\.org/(?:abs|pdf)/|arxiv:\s*)", "",
                   (value or "").strip().casefold())
    return re.sub(r"v\d+$", "", value.removesuffix(".pdf"))


def _surname(author: str) -> str:
    # Bibliographies often print "Smith, A." while metadata uses "Alice Smith".
    part = author.split(",", 1)[0] if "," in author else author
    words = [word for word in part.split() if len(_key(word)) > 1]
    return _key(words[-1]) if words else ""


def _printed_identifiers(quote: str) -> tuple[set[str], set[str]]:
    text = quote.casefold()
    dois = set()
    for token in re.findall(r"10\.\d{4,9}/[-._;()/:a-z0-9]+", text):
        token = token.rstrip(".,;:")
        while token.endswith(")") and token.count(")") > token.count("("):
            token = token[:-1]
        dois.add(token)
    arxiv_ids = {_arxiv(token) for token in re.findall(
        r"(?<![a-z0-9])(?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[a-z-]+)?/\d{7})(?:v\d+)?"
        r"(?![a-z0-9.])", text)}
    return dois, arxiv_ids


def validate_extraction(synthesis: dict[str, Any], pages: list[dict[str, Any]],
                        sections: dict[str, str], *,
                        check_prose: bool = True) -> tuple[list[dict], list[dict]]:
    """Reject ungrounded bibliography and declared coined names leaking into prose."""
    terms, references = synthesis.get("coined_terms"), synthesis.get("references")
    if not isinstance(terms, list) or not isinstance(references, list):
        raise ValueError("Synthesis must contain coined_terms and references arrays")
    for term in terms:
        if (not isinstance(term, dict) or any(not isinstance(term.get(k), str)
                                             or not term[k].strip()
                                             for k in ("name", "description"))):
            raise ValueError("Coined terms require a name and generic description")
    if check_prose:
        validate_knowledge_prose(synthesis, sections, terms)
    page_text = {p["page"]: p["text"] for p in pages}
    validated = []
    for reference in references:
        if not isinstance(reference, dict):
            raise ValueError("Reference must be an object")
        for field in ("label", "title", "quote"):
            if not isinstance(reference.get(field), str):
                raise ValueError(f"Reference requires string {field}")
        if not reference["label"].strip() or not reference["quote"].strip():
            raise ValueError("Reference label and quote must not be empty")
        authors, year = reference.get("authors"), reference.get("year")
        if (not isinstance(authors, list)
                or any(not isinstance(a, str) or not a.strip() for a in authors)
                or year is not None and (type(year) is not int or not 1800 <= year <= 2100)):
            raise ValueError("Invalid reference authors or year")
        for field in ("doi", "arxiv_id"):
            if reference.get(field) is not None and not isinstance(reference[field], str):
                raise ValueError(f"Invalid reference {field}")
        cited_pages = reference.get("pages")
        if (not isinstance(cited_pages, list) or not cited_pages
                or any(type(p) is not int or p not in page_text for p in cited_pages)
                or cited_pages != sorted(set(cited_pages))
                or cited_pages != list(range(cited_pages[0], cited_pages[-1] + 1))):
            raise ValueError("Reference requires valid contiguous PDF pages")
        quote = reference["quote"]
        source = "\n".join(page_text[p] for p in cited_pages)
        if " ".join(quote.split()) not in " ".join(source.split()):
            raise ValueError("Reference quote is not present on the cited PDF pages")
        grounded = _key(quote)
        for value in [reference["title"], *authors, str(year) if year else ""]:
            if value and _key(value) not in grounded:
                raise ValueError("Reference metadata is not supported by its quote")
        printed_dois, printed_arxiv = _printed_identifiers(quote)
        doi, arxiv = _doi(reference.get("doi")), _arxiv(reference.get("arxiv_id"))
        if (doi and doi not in printed_dois or arxiv and arxiv not in printed_arxiv):
            raise ValueError("Reference identifier is not supported by its quote")
        if research_wiki.scan_for_threats(json.dumps(reference, ensure_ascii=False), scope="strict"):
            raise ValueError("Reference failed the wiki content scan")
        validated.append(reference)
    return terms, validated


def validate_knowledge_prose(synthesis: dict[str, Any], sections: dict[str, str],
                             terms: list[dict]) -> None:
    """Check mutable prose separately from immutable source extraction."""
    metadata = synthesis.get("metadata", {})
    prose = [body for heading, body in sections.items() if heading != "Abstract"]
    for node in [metadata, synthesis.get("idea", {}), *synthesis.get("experiments", [])]:
        prose.extend(str(value) for key, value in node.items()
                     if node is not metadata or key in research_wiki.CLASSIFICATION_FIELDS)
    for term in terms:
        name = term["name"].strip()
        # Avoid treating an acronym embedded inside another word as a name occurrence.
        pattern = (rf"(?<![a-zA-Z0-9_]){re.escape(name)}(?![a-zA-Z0-9_])" if name.isascii()
                   else re.escape(name))
        if any(re.search(pattern, text, re.I) for text in
               [*prose, *(t.get("description", "") for t in terms if isinstance(t, dict))]):
            raise ValueError(f"Author-coined name remains in knowledge prose: {name}")


def _identity(path: Path) -> dict[str, Any]:
    meta = research_wiki._load_paper_frontmatter(path)
    try:
        authors = json.loads(meta.get("authors", "[]"))
    except ValueError:
        authors = []
    return {"id": f"paper:{path.stem}", "title": _key(meta.get("title", "")),
            "authors": {_surname(a) for a in authors if isinstance(a, str) and _surname(a)},
            "year": str(meta.get("year", "")), "doi": _doi(meta.get("doi", "")),
            "arxiv_id": _arxiv(meta.get("arxiv", ""))}


def _match(reference: dict, papers: list[dict]) -> str | None:
    doi, arxiv = _doi(reference.get("doi")), _arxiv(reference.get("arxiv_id"))
    candidates = []
    for paper in papers:
        if any(a and b and a != b for a, b in ((doi, paper["doi"]), (arxiv, paper["arxiv_id"]))):
            continue
        id_match = bool(doi and doi == paper["doi"] or arxiv and arxiv == paper["arxiv_id"])
        authors = {_surname(a) for a in reference["authors"] if _surname(a)}
        title_match = bool(_key(reference["title"]) and _key(reference["title"]) == paper["title"]
                           and reference.get("year") and str(reference["year"]) == paper["year"]
                           and authors & paper["authors"])
        if id_match or title_match:
            candidates.append((paper["id"], id_match))
    exact = [node for node, id_match in candidates if id_match]
    matches = exact or [node for node, _ in candidates]
    return matches[0] if len(matches) == 1 else None


def save_references(paper_path: Path, terms: list[dict], references: list[dict]) -> Path:
    """Store source-only aliases and references without adding unanalysed paper nodes."""
    target = paper_path.with_suffix(".references.json")
    record = {"version": ANALYSIS_VERSION, "paper_id": f"paper:{paper_path.stem}",
              "coined_terms": terms, "references": references}
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=target.parent,
                                     prefix=".references-", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
            stream.flush()
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
    return target


def refresh_citations(root: Path) -> dict[str, int]:
    """Resolve saved bibliographies against all current papers, including new arrivals."""
    papers = [_identity(p) for p in sorted((root / "papers").glob("*.md"))]
    ids = {p["id"] for p in papers}
    counts = {}
    relations_by_owner = {}
    for path in sorted((root / "papers").glob("*.references.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        owner = record["paper_id"]
        if owner not in ids:
            continue
        relations = []
        for reference in record["references"]:
            target = _match(reference, papers)
            if target and target != owner:
                relations.append({"from": owner, "to": target, "type": "cites",
                                  "evidence": f"{path.relative_to(root)}; reference "
                                  f"{reference['label']}; PDF pages {reference['pages']}: "
                                  f"{reference['quote']}"})
        relations_by_owner[owner] = relations
        counts[owner] = len({e["to"] for e in relations})
    write_relations_batch(root, relations_by_owner, origin=CITATION_ORIGIN)
    return counts
