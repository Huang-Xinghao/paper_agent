"""Wiki bibliography, citation matching and evidence-grounded graph relationships."""

from __future__ import annotations

import json
import logging
import re
import tempfile
import unicodedata
from itertools import zip_longest
from pathlib import Path
from typing import Any, Callable
from paper_agent.tools import research_wiki
from paper_agent.tools.wiki_retrieval import Page, Section, load_page, rank_pages
from paper_agent.tools.wiki_prompts import RELATION_PROMPT
from paper_agent.tools.wiki_metadata import metadata_path, metadata_records
from paper_agent.tools.wiki_prompts import PROMPT_VERSION as ANALYSIS_VERSION

PAPER_RELATIONS = {
    "extends",
    "contradicts",
    "supersedes",
    "shares_method",
    "addresses_same_problem",
}
IDEA_RELATIONS = {"inspired_by"}
ORIGIN = "wiki-agent-relations-v1"
MAX_CANDIDATES = 8
logger = logging.getLogger(__name__)


def _summary(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    # Existing wiki pages use JSON-compatible single-line classification arrays.
    header = text.split("\n---\n", 1)[0]
    summary: dict[str, Any] = {"id": f"paper:{path.stem}"}
    for key in ("title", *research_wiki.CLASSIFICATION_FIELDS):
        match = re.search(rf"^{key}: (.+)$", header, re.M)
        if match:
            try:
                summary[key] = json.loads(match[1])
            except ValueError:
                summary[key] = match[1]
    thesis = re.search(r"(?m)^## One-line thesis\n(.*?)(?=^## |\Z)", text, re.S)
    summary["thesis"] = thesis[1].strip()[:700] if thesis else ""
    return summary


def classification_vocabulary(root: Path) -> dict[str, list[str]]:
    """Offer existing labels as vocabulary hints, never as evidence about a new PDF."""
    labels: dict[str, set[str]] = {
        key: set() for key in research_wiki.CLASSIFICATION_FIELDS
    }
    for directory in ("papers", "ideas", "experiments"):
        for path in sorted((root / directory).glob("*.md")):
            summary = _summary(path)
            for key in labels:
                values = summary.get(key, [])
                if isinstance(values, list):
                    labels[key].update(v for v in values if isinstance(v, str))
    return {key: sorted(values) for key, values in labels.items()}


def _select_candidates(
    papers: dict[str, Path], paper: Page, idea: Page
) -> dict[str, Path]:
    """Interleave problem and mechanism BM25 hits without filtering by research field."""

    def body(page: Page, headings: set[str]) -> str:
        sections = []
        for section in page.sections:
            if section.heading not in headings:
                continue
            lines = [
                line
                for line in page.lines[section.start + 1 : section.end]
                if line.strip()
                and not line.strip().startswith(("_TODO", "_Not yet", "_To be", "<!--"))
            ]
            content = "\n".join(lines).strip()
            if content:
                sections.append(content)
        return "\n".join(sections)

    problem = body(paper, {"One-line thesis", "Problem / Gap"})
    if not problem:
        problem = body(paper, {"Abstract"})
    mechanism = "\n".join((body(paper, {"Method"}), body(idea, {"strcture"}))).strip()
    queries = [f"{paper.title}\n{problem}"]
    if mechanism:
        queries.append(mechanism)
    pages = []
    for path in papers.values():
        candidate = load_page(path)
        content = body(
            candidate, {s.heading for s in candidate.sections} - {"Connections"}
        )
        lines = content.splitlines()
        pages.append(
            Page(
                path,
                content,
                lines,
                candidate.title,
                [Section("Candidate", 0, len(lines))],
            )
        )
    rankings = [rank_pages(pages, query) for query in queries]
    by_path = {path: node for node, path in papers.items()}
    selected = {}
    # Give method-only matches a place even when same-field problem hits dominate.
    for row in zip_longest(*rankings):
        for hit in row:
            if hit is None:
                continue
            page, _score = hit
            selected[by_path[page.path]] = page.path
            if len(selected) == MAX_CANDIDATES:
                return selected
    return selected


def discover_relations(
    root: Path,
    paper_path: Path,
    idea_path: Path,
    analyze: Callable[[str, str], dict[str, Any]],
) -> list[dict[str, str]]:
    """Read candidate notes, then validate endpoint types and verbatim evidence."""
    papers = {
        f"paper:{p.stem}": p
        for p in sorted((root / "papers").glob("*.md"))
        if p != paper_path
    }
    if not papers:
        return []
    current_id, idea_id = f"paper:{paper_path.stem}", f"idea:{idea_path.stem}"
    paper, idea = load_page(paper_path), load_page(idea_path)
    current = {current_id: paper.text, idea_id: idea.text}
    if len(papers) > MAX_CANDIDATES:
        papers = _select_candidates(papers, paper, idea)
    if not papers:
        return []
    existing_ideas = {}
    graph = root / "graph" / "edges.jsonl"
    linked_ideas = set()
    for line in graph.read_text(encoding="utf-8").splitlines():
        if line.strip():
            edge = json.loads(line)
            if edge.get("type") == "inspired_by" and edge.get("to") in papers:
                linked_ideas.add(edge.get("from"))
    for candidate in sorted((root / "ideas").glob("*.md")):
        node = f"idea:{candidate.stem}"
        if node != idea_id and node in linked_ideas:
            existing_ideas[node] = candidate
    documents = {
        **current,
        **{
            node: path.read_text(encoding="utf-8")
            for node, path in {**papers, **existing_ideas}.items()
        },
    }
    response = analyze(
        RELATION_PROMPT,
        json.dumps(
            {
                "current_paper": current_id,
                "current_idea": idea_id,
                "documents": documents,
            },
            ensure_ascii=False,
        ),
    )
    relations = response.get("relationships")
    if not isinstance(relations, list):
        raise ValueError("Relationship analysis must contain a relationships array")
    validated, seen = [], set()
    paths = {current_id: paper_path, idea_id: idea_path, **papers, **existing_ideas}
    for edge in relations:
        keys = ("from", "to", "type", "source_quote", "target_quote", "reason")
        if not isinstance(edge, dict) or any(
            not isinstance(edge.get(k), str) or not edge[k].strip() for k in keys
        ):
            raise ValueError(
                "Relationship missing endpoint, type, evidence quote or reason"
            )
        source, target, kind = (edge[k] for k in ("from", "to", "type"))
        paper_pair = (
            source in papers
            and target == current_id
            or source == current_id
            and target in papers
        )
        idea_pair = (
            source == idea_id
            and target in papers
            or source in existing_ideas
            and target == current_id
        )
        if not (
            paper_pair
            and kind in PAPER_RELATIONS
            or idea_pair
            and kind in IDEA_RELATIONS
        ):
            raise ValueError("Unsupported relationship type or endpoint pair")
        for node, key in ((source, "source_quote"), (target, "target_quote")):
            body = documents[node].split("\n---\n", 1)[-1]
            if edge[key] not in body:
                raise ValueError(
                    f"Relationship {key} is not a verbatim body excerpt of {node}"
                )
        identity = source, target, kind
        # Symmetric relationships get a canonical direction for stable deduplication.
        if kind in {"shares_method", "addresses_same_problem", "contradicts"}:
            identity = *sorted((source, target)), kind
        if identity in seen:
            continue
        seen.add(identity)
        evidence = (
            f"{paths[source].relative_to(root)}: {edge['source_quote']}\n"
            f"{paths[target].relative_to(root)}: {edge['target_quote']}\n"
            f"{edge['reason']}"
        )
        if research_wiki.scan_for_threats(evidence, scope="strict"):
            raise ValueError("Relationship evidence failed the wiki content scan")
        validated.append(
            {"from": identity[0], "to": identity[1], "type": kind, "evidence": evidence}
        )
    return validated


def write_relations(
    root: Path, owner: str, relations: list[dict[str, str]], *, origin: str = ORIGIN
) -> list[str]:
    """Atomically refresh this paper's inferred edges, preserving other/manual edges."""
    return write_relations_batch(root, {owner: relations}, origin=origin)


def write_relations_batch(
    root: Path,
    relations_by_owner: dict[str, list[dict[str, str]]],
    *,
    origin: str = ORIGIN,
) -> list[str]:
    """Commit edges atomically; return warnings for subsequent activity-log failures."""
    if not relations_by_owner:
        return []
    edge_path = root / "graph" / "edges.jsonl"
    existing = [
        json.loads(line)
        for line in edge_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    kept = [
        e
        for e in existing
        if not (e.get("origin") == origin and e.get("owner") in relations_by_owner)
    ]
    # Stage through the canonical writer to retain deduplication and evidence hygiene.
    with tempfile.TemporaryDirectory(
        prefix=".relations-", dir=edge_path.parent
    ) as directory:
        staged_root = Path(directory)
        staged = staged_root / "graph" / "edges.jsonl"
        staged.parent.mkdir()
        staged.write_text(
            "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in kept),
            encoding="utf-8",
        )
        research_wiki.add_edges(
            str(staged_root),
            [
                {**edge, "origin": origin, "owner": owner}
                for owner, relations in relations_by_owner.items()
                for edge in relations
            ],
        )
        quarantine_log = staged.parent / "quarantine.log"
        if quarantine_log.exists():
            with (edge_path.parent / "quarantine.log").open(
                "a", encoding="utf-8"
            ) as stream:
                stream.write(quarantine_log.read_text(encoding="utf-8"))
        staged.replace(edge_path)
    warnings = []
    for owner, relations in relations_by_owner.items():
        try:
            research_wiki.append_log(
                str(root), f"relationships: {owner} inferred={len(relations)}"
            )
        except OSError as exc:
            warning = f"Relationships committed; activity log failed for {owner}: {exc}"
            warnings.append(warning)
            logger.warning(warning)
    return warnings


CITATION_ORIGIN = "wiki-agent-citations-v1"


def _key(value: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKC", value).casefold() if c.isalnum()
    )


def _doi(value: str | None) -> str:
    if value in (None, "null", "None"):
        return ""
    return re.sub(
        r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)",
        "",
        (value or "").strip().casefold(),
    ).rstrip(".,;")


def _arxiv(value: str | None) -> str:
    if value in (None, "null", "None"):
        return ""
    value = re.sub(
        r"^(?:https?://arxiv\.org/(?:abs|pdf)/|arxiv:\s*)",
        "",
        (value or "").strip().casefold(),
    )
    return re.sub(r"v\d+$", "", value.removesuffix(".pdf"))


def _surname(author: str) -> str:
    # Bibliographies often print "Smith, A." while metadata uses "Alice Smith".
    part = author.split(",", 1)[0] if "," in author else author
    words = [word for word in part.split() if len(_key(word)) > 1]
    return _key(words[-1]) if words else ""


def _printed_identifiers(quote: str) -> tuple[set[str], set[str]]:
    text = quote.casefold()
    dois = set()
    arxiv_text = list(text)
    for match in re.finditer(r"10\.\d{4,9}/[-._;()/:a-z0-9]+", text):
        token = match.group()
        token = token.rstrip(".,;:")
        while token.endswith(")") and token.count(")") > token.count("("):
            token = token[:-1]
        dois.add(token)
        # Journal DOI suffixes such as 2026.12865 are not arXiv IDs.
        # Retain the explicit arXiv DOI namespace (10.48550/arXiv.<ID>).
        if not token.startswith("10.48550/arxiv."):
            arxiv_text[match.start():match.end()] = " " * (match.end() - match.start())
    arxiv_ids = {
        _arxiv(token)
        for token in re.findall(
            r"(?<![a-z0-9])(?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[a-z-]+)?/\d{7})(?:v\d+)?"
            r"(?![a-z0-9]|\.\d)",
            "".join(arxiv_text),
        )
    }
    return dois, arxiv_ids


def _printed_doi_matches(doi: str, visible: str) -> bool:
    """Allow parser whitespace after DOI punctuation, preserving every character."""
    if not re.fullmatch(r"10\.\d{4,9}/[-._;()/:a-z0-9]+", doi):
        return False
    pattern = "".join(
        re.escape(char) + (r"\s*" if char in "./-" else "") for char in doi
    )
    return (
        re.search(
            r"(?<![a-z0-9])" + pattern + r"(?![-_/:a-z0-9]|\.[a-z0-9])",
            visible.casefold(),
        )
        is not None
    )


def _source_link_dois(quote: str) -> dict[str, str]:
    """Reconcile a full DOI label's line-wrap hyphens with its source PDF link.

    Arbitrary link labels cannot provide identifier evidence. Both visible and
    destination DOIs must be complete and differ only in hyphens/whitespace.
    """
    result = {}
    pattern = r"\[([^\]\n]*)\]\((https?://(?:dx\.)?doi\.org/[^\s()]+)\)"
    for label, target in re.findall(pattern, quote, re.I):
        shown = re.sub(r"\s+", "", label)
        if not re.match(r"(?:https?://(?:dx\.)?doi\.org/|doi:\s*)?10\.\d{4,9}/", shown, re.I):
            continue
        shown, target = _doi(shown), _doi(target)
        if (shown != target and shown.replace('-', '') == target.replace('-', '')
                and re.fullmatch(r"10\.\d{4,9}/[-._;()/:a-z0-9]+", target)):
            result[shown] = target
    return result


def _bibliography_text(text: str) -> str:
    """Visible reference text, retaining scientific letters and numbers."""
    text = re.sub(r'\[([^\]\n]*)\]\((?:[^()\s]|\([^()\s]*\))*\)', r'\1', text)
    text = re.sub(r'</?(?:a|sup|sub|b|strong|i|em|span)(?:\s[^>]*)?>', '', text, flags=re.I)
    text = re.sub(r'\\(?:mathrm|mathbf|mathit|mathsf|textrm|text)\s*', '', text)
    # Download watermarks may split a bibliography entry across physical pages.
    text = re.sub(r'(?mi)^Downloaded from [^\n]*\buser on [^\n]*(?:\n|$)', '', text)
    return text


def _validate_extraction(
    synthesis: dict[str, Any],
    pages: list[dict[str, Any]],
    sections: dict[str, str],
    *,
    check_prose: bool = True,
) -> tuple[list[dict], list[dict]]:
    """Reject ungrounded bibliography and declared coined names leaking into prose."""
    terms, references = synthesis.get("coined_terms"), synthesis.get("references")
    if not isinstance(terms, list) or not isinstance(references, list):
        raise ValueError("Synthesis must contain coined_terms and references arrays")
    for term in terms:
        if not isinstance(term, dict) or any(
            not isinstance(term.get(k), str) or not term[k].strip()
            for k in ("name", "description")
        ):
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
        if (
            not isinstance(authors, list)
            or any(not isinstance(a, str) or not a.strip() for a in authors)
            or year is not None
            and (type(year) is not int or not 1800 <= year <= 2100)
        ):
            raise ValueError("Invalid reference authors or year")
        for field in ("doi", "arxiv_id"):
            if reference.get(field) is not None and not isinstance(
                reference[field], str
            ):
                raise ValueError(f"Invalid reference {field}")
        cited_pages = reference.get("pages")
        if (
            not isinstance(cited_pages, list)
            or not cited_pages
            or any(type(p) is not int or p not in page_text for p in cited_pages)
            or cited_pages != sorted(set(cited_pages))
            or cited_pages != list(range(cited_pages[0], cited_pages[-1] + 1))
        ):
            raise ValueError("Reference requires valid contiguous PDF pages")
        quote = reference["quote"]
        source = "\n".join(page_text[p] for p in cited_pages)
        if " ".join(quote.split()) not in " ".join(source.split()):
            raise ValueError("Reference quote is not present on the cited PDF pages")
        # MinerU may split an author across links and insert the reference label
        # between their initials and surname. Match metadata against visible text;
        # keep the original quote unchanged for the exact source check above.
        visible = _bibliography_text(quote)
        visible = visible.replace(f"[{reference['label']}]", "")
        grounded = _key(visible)
        for value in [reference["title"], *authors, str(year) if year else ""]:
            if value and _key(_bibliography_text(value)) not in grounded:
                raise ValueError(
                    f"Reference metadata is not supported by its quote: {reference['label']} ({value})"
                )
        printed_dois, printed_arxiv = _printed_identifiers(visible)
        printed_dois.update(_source_link_dois(quote).values())
        doi, arxiv = _doi(reference.get("doi")), _arxiv(reference.get("arxiv_id"))
        if (
            doi
            and doi not in printed_dois
            and not _printed_doi_matches(doi, visible)
            or arxiv
            and arxiv not in printed_arxiv
        ):
            raise ValueError(
                f"Reference identifier is not supported by its quote: doi={doi!r}, arxiv_id={arxiv!r}; printed DOI={sorted(printed_dois)!r}, arXiv={sorted(printed_arxiv)!r}"
            )
        if research_wiki.scan_for_threats(
            json.dumps(reference, ensure_ascii=False), scope="strict"
        ):
            raise ValueError("Reference failed the wiki content scan")
        validated.append(reference)
    return terms, validated


def validate_extraction(
    synthesis: dict[str, Any],
    pages: list[dict[str, Any]],
    sections: dict[str, str],
    *,
    check_prose: bool = True,
):
    """Locate invalid bibliography entries without weakening source validation."""
    references = synthesis.get("references")
    if not isinstance(references, list):
        return _validate_extraction(synthesis, pages, sections, check_prose=check_prose)
    terms, _ = _validate_extraction(
        {**synthesis, "references": []}, pages, sections, check_prose=check_prose
    )
    errors = []
    for index, reference in enumerate(references):
        try:
            _validate_extraction(
                {**synthesis, "references": [reference]}, pages, {}, check_prose=False
            )
        except ValueError as exc:
            label = reference.get("label", "?") if isinstance(reference, dict) else "?"
            errors.append(f"references[{index}] (label {label}): {exc}")
    if errors:
        raise ValueError("; ".join(errors))
    return terms, references


def validate_knowledge_prose(
    synthesis: dict[str, Any], sections: dict[str, str], terms: list[dict]
) -> None:
    """Check mutable prose separately from immutable source extraction."""
    metadata = synthesis.get("metadata", {})
    prose = [body for heading, body in sections.items() if heading != "Abstract"]
    for node in [
        metadata,
        synthesis.get("idea", {}),
        *synthesis.get("experiments", []),
    ]:
        prose.extend(
            str(value)
            for key, value in node.items()
            if node is not metadata or key in research_wiki.CLASSIFICATION_FIELDS
        )
    dataset_names = {
        part.strip().casefold()
        for dataset in synthesis.get("datasets", [])
        if isinstance(dataset, dict) and isinstance(dataset.get("name"), str)
        for part in re.split(r"\s*/\s*", dataset["name"])
        if part.strip()
    }
    for term in terms:
        name = term["name"].strip()
        # Dataset identity is explicitly exempt from generic mechanism naming.
        # A model can mistakenly list a newly introduced dataset as a coined method.
        if name.casefold() in dataset_names:
            continue
        # Avoid treating an acronym embedded inside another word as a name occurrence.
        pattern = (
            rf"(?<![a-zA-Z0-9_]){re.escape(name)}(?![a-zA-Z0-9_])"
            if name.isascii()
            else re.escape(name)
        )
        if any(
            re.search(pattern, text, re.I)
            for text in [
                *prose,
                *(t.get("description", "") for t in terms if isinstance(t, dict)),
            ]
        ):
            raise ValueError(f"Author-coined name remains in knowledge prose: {name}")


def _identity(path: Path) -> dict[str, Any]:
    meta = research_wiki._load_paper_frontmatter(path)
    try:
        authors = json.loads(meta.get("authors", "[]"))
    except ValueError:
        authors = []
    return {
        "id": f"paper:{path.stem}",
        "title": _key(meta.get("title", "")),
        "authors": {_surname(a) for a in authors if isinstance(a, str) and _surname(a)},
        "year": str(meta.get("year", "")),
        "doi": _doi(meta.get("doi", "")),
        "arxiv_id": _arxiv(meta.get("arxiv", "")),
    }


def _match(reference: dict, papers: list[dict]) -> str | None:
    doi, arxiv = _doi(reference.get("doi")), _arxiv(reference.get("arxiv_id"))
    authors = {_surname(a) for a in reference["authors"] if _surname(a)}
    title = _key(reference["title"])
    candidates = []
    for paper in papers:
        if any(
            a and b and a != b
            for a, b in ((doi, paper["doi"]), (arxiv, paper["arxiv_id"]))
        ):
            continue
        id_match = bool(
            doi and doi == paper["doi"] or arxiv and arxiv == paper["arxiv_id"]
        )
        title_match = bool(
            title
            and title == paper["title"]
            and reference.get("year")
            and str(reference["year"]) == paper["year"]
            and authors & paper["authors"]
        )
        if id_match or title_match:
            candidates.append((paper["id"], id_match))
    exact = [node for node, id_match in candidates if id_match]
    matches = exact or [node for node, _ in candidates]
    return matches[0] if len(matches) == 1 else None


def _citation_index(papers):
    """Index only exact matching keys; _match still enforces conflicts/ambiguity."""
    lookup = {}
    for paper in papers:
        for key in [('doi', paper['doi']), ('arxiv', paper['arxiv_id']),
                    ('title-year', (paper['title'], paper['year']))]:
            lookup.setdefault(key, []).append(paper)
    return lookup


def _citation_candidates(reference, lookup):
    candidates = {}
    for key in [('doi', _doi(reference.get('doi'))),
                ('arxiv', _arxiv(reference.get('arxiv_id'))),
                ('title-year', (_key(reference['title']), str(reference.get('year'))))]:
        if key[1]:
            for paper in lookup.get(key, []):
                candidates[paper['id']] = paper
    return list(candidates.values())


def save_references(
    paper_path: Path, terms: list[dict], references: list[dict]
) -> Path:
    """Store source-only aliases and references without adding unanalysed paper nodes."""
    target = metadata_path(paper_path, "references")
    record = {
        "version": ANALYSIS_VERSION,
        "paper_id": f"paper:{paper_path.stem}",
        "coined_terms": terms,
        "references": references,
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=target.parent,
        prefix=".references-",
        delete=False,
    ) as stream:
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
    lookup = _citation_index(papers)
    ids = {p["id"] for p in papers}
    counts = {}
    relations_by_owner = {}
    for _paper, path in metadata_records(root, "references"):
        record = json.loads(path.read_text(encoding="utf-8"))
        owner = record["paper_id"]
        if owner not in ids:
            continue
        relations = []
        for reference in record["references"]:
            target = _match(reference, _citation_candidates(reference, lookup))
            if target and target != owner:
                relations.append(
                    {
                        "from": owner,
                        "to": target,
                        "type": "cites",
                        "evidence": f"{path.relative_to(root)}; reference "
                        f"{reference['label']}; PDF pages {reference['pages']}: "
                        f"{reference['quote']}",
                    }
                )
        relations_by_owner[owner] = relations
        counts[owner] = len({e["to"] for e in relations})
    write_relations_batch(root, relations_by_owner, origin=CITATION_ORIGIN)
    return counts
