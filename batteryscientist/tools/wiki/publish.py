"""Validate publication drafts, select figures and publish reviewed Wiki content."""

from __future__ import annotations

from contextlib import nullcontext
from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import json
import logging
from pathlib import Path
import re
from typing import Any, Callable, NamedTuple
import unicodedata
from uuid import uuid4

from batteryscientist.storage import write_json

from . import storage
from ..core import ToolContext
from ..image_viewing import view_image
from ..paper_figures import READER_VERSION, save_architecture_figures
from ..paper_preprocessing import PREPROCESSING_VERSION
from .prompts import FIGURE_REVIEW_PROMPT
from .relations import (
    ANALYSIS_VERSION,
    discover_relations,
    refresh_citations,
    save_references,
    validate_extraction,
    validate_knowledge_prose,
    write_relations,
)
from .review import abstract_provenance
from .storage import (
    corpus_root,
    library_root,
    metadata_path,
    resolve_legacy_path,
    split_layout,
)


logger = logging.getLogger(__name__)


def assert_same_identity(path: Path, metadata: dict) -> None:
    """A shared DOI or filename alone must never authorize replacing another study."""
    from .storage import _last_name

    front = path.read_text(encoding="utf-8")[4:].split("\n---\n", 1)[0]

    def field(key):
        match = re.search(rf"(?m)^{key}: (.+)$", front)
        return json.loads(match[1]) if match else None

    def key(value):
        return "".join(
            c for c in unicodedata.normalize("NFKC", value).casefold() if c.isalnum()
        )

    old_authors, new_authors = field("authors") or [], metadata.get("authors") or []
    if (
        key(field("title") or "") != key(metadata["title"])
        or not old_authors
        or not new_authors
        or not (
            {key(_last_name(a)) for a in old_authors}
            & {key(_last_name(a)) for a in new_authors}
        )
    ):
        raise ValueError(
            f"Paper identity conflict: {path}; title/authors differ from incoming study"
        )


def markdown_errors(text: str) -> list[str]:
    """Check GFM table widths outside fences, including pipes inside inline code."""
    lines = text.splitlines()
    errors, fence, expected = [], None, None

    def cells(line):
        # A pipe is escaped iff it follows an odd number of backslashes.
        parts, start, separators = [], 0, []
        for i, char in enumerate(line):
            if char == "|" and (len(line[:i]) - len(line[:i].rstrip("\\"))) % 2 == 0:
                parts.append(line[start:i])
                start = i + 1
                separators.append(i)
        parts.append(line[start:])
        if separators and separators[0] == 0:
            parts.pop(0)
        if separators and separators[-1] == len(line) - 1:
            parts.pop()
        return parts

    previous = None
    for n, line in enumerate(lines, 1):
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if fence:
            if (
                marker
                and marker[1][0] == fence[0]
                and len(marker[1]) >= fence[1]
                and not marker[2].strip()
            ):
                fence = None
            continue
        if marker:
            fence = (marker[1][0], len(marker[1]))
            expected = None
            previous = None
            continue
        if re.search(r"(?<![A-Za-z])TODO(?![A-Za-z])", line, re.I):
            errors.append(f"line {n}: TODO placeholder")
        row = cells(line.strip())
        if "|" in line and row and all(re.fullmatch(r"\s*:?-+:?\s*", c) for c in row):
            expected = len(row)
            if previous and len(cells(previous)) != expected:
                errors.append(f"line {n-1}: table header width")
        elif expected is not None:
            if (
                not line.strip()
                or "|" not in line
                or re.match(r"^\s{0,3}(#|>|[-*+] )", line)
            ):
                expected = None
            elif len(row) != expected:
                errors.append(
                    f"line {n}: table has {len(row)} cells; expected {expected}"
                )
        previous = line.strip()
    return errors


def remove_template_placeholders(path: Path) -> None:
    """Omit optional sections whose entire body is the untouched template marker."""
    text = path.read_text(encoding="utf-8")
    # Exact marker/body matching avoids removing user notes or unfinished prose.
    text = re.sub(
        r"(?m)^## (?:Problem / Gap|Method|Limitations / Failure Modes)\n\s*_TODO\._\s*(?=^## |\Z)",
        "",
        text,
    )
    path.write_text(text, encoding="utf-8")


def retire_generated_experiments(
    root: Path, idea_slug: str, current: set[str], tag: str
) -> None:
    """Archive only this publisher's replaced experiments; preserve user experiments."""
    library = library_root(root)
    retired, records = set(), []
    aliases_path = library / "graph/path_aliases.json"
    aliases = json.loads(aliases_path.read_text()) if aliases_path.exists() else {}
    for path in sorted((root / "experiments").glob("*.md")):
        if path.name in current:
            continue
        content = path.read_text(encoding="utf-8")
        front = content[4:].split("\n---\n", 1)[0]
        if not re.search(
            rf'(?m)^idea_id: ["\']?idea:{re.escape(idea_slug)}["\']?$', front
        ):
            continue
        match = re.search(r"(?m)^tags: (.+)$", front)
        if not match or tag not in json.loads(match[1]):
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        target = root / "archive/superseded-experiments" / digest[:16] / path.name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.read_bytes())
        rel, archived = str(path.relative_to(library)), str(target.relative_to(library))
        aliases[rel] = archived
        retired.add("exp:" + path.stem)
        records.append(
            dict(
                original=rel,
                archived=archived,
                sha256=digest,
                reason="Replaced publisher experiment set",
            )
        )
        path.unlink()
    if not retired:
        return
    edge_path = library / "graph/edges.jsonl"
    edges = [
        json.loads(line) for line in edge_path.read_text().splitlines() if line.strip()
    ]
    removed = [e for e in edges if e["from"] in retired or e["to"] in retired]
    bundle = (
        root
        / "archive/superseded-experiments"
        / hashlib.sha256(json.dumps(records, sort_keys=True).encode()).hexdigest()[:16]
    )
    write_json(bundle / "manifest.json", dict(files=records, edges=removed))
    edge_path.write_text(
        "".join(
            json.dumps(e, ensure_ascii=False) + "\n" for e in edges if e not in removed
        ),
        encoding="utf-8",
    )
    # Flatten older aliases that previously targeted a now retired path.
    aliases = {key: aliases.get(value, value) for key, value in aliases.items()}
    write_json(aliases_path, aliases)


def limit_to_one_figure(report: dict) -> dict:
    """Keep the strongest overview among approved candidates, with stable ties."""
    candidates = {item['id']: item for item in report['candidates']}
    kept = [item for item in report['selection'] if item['decision'] == 'keep']
    if len(kept) <= 1:
        return report

    def score(record):
        caption = candidates[record['id']]['caption'].lower()
        overview = bool(re.search(r'\b(overview|overall|architecture|framework)\b', caption))
        detail = bool(re.search(r'\b(example|case study|case studies|step \d|step-by-step)\b', caption))
        return (overview, not detail)

    winner = max(kept, key=score)['id']
    selection = [
        {**item, 'decision': 'skip',
         'reason': f'One-figure policy: retained {winner} as the representative method diagram. ',
         'previous_decision': item}
        if item['decision'] == 'keep' and item['id'] != winner else item
        for item in report['selection']
    ]
    return {**report, 'selection': selection, 'selection_policy': 'one-representative-architecture-v1'}


def _image_path(extraction: dict, figure: dict) -> Path | None:
    if not figure.get("image"):
        return None
    cache = Path(extraction["cache"]).resolve()
    path = (cache / figure["image"]).resolve()
    if not path.is_relative_to(cache):
        raise ValueError("Figure image escapes the parser cache")
    return path if path.is_file() else None


def figure_candidates(extraction: dict) -> list[dict]:
    """Expose full captions and stable IDs; file paths never come from the model."""
    candidates = [
        {"id": f"p{figure['page']}-figure-{figure['number']}",
         "number": str(figure["number"]), "page": figure["page"],
         "caption": figure["caption"],
         "image_available": _image_path(extraction, figure) is not None,
         "review_required": figure.get("review_required", False),
         "review_reasons": figure.get("review_reasons", [])}
        for figure in extraction["figures"]
    ]
    counts = {}
    for candidate in candidates:
        base = candidate['id']
        counts[base] = counts.get(base, 0) + 1
        if counts[base] > 1:
            candidate['id'] = f"{base}-occurrence-{counts[base]}"
            candidate['review_required'] = True
            candidate['review_reasons'] = [*candidate['review_reasons'], 'duplicate_caption_anchor']
    return candidates


def validate_figure_selection(selection, candidates: list[dict], *,
                              decisions=frozenset({"keep", "skip", "inspect"})) -> None:
    if not isinstance(selection, list):
        raise ValueError("figure_selection must be an array covering every figure candidate")
    lookup = {candidate["id"]: candidate for candidate in candidates}
    seen = set()
    for record in selection:
        if not isinstance(record, dict) or not isinstance(record.get("id"), str):
            raise ValueError("figure_selection requires a candidate ID per decision")
        identity = record["id"]
        if identity not in lookup or identity in seen:
            raise ValueError(f"figure_selection has unknown or duplicate ID: {identity}")
        seen.add(identity)
        if not isinstance(record.get("decision"), str) or record["decision"] not in decisions:
            raise ValueError(f"figure_selection has invalid decision: {identity}")
        if not isinstance(record.get("reason"), str) or not record["reason"].strip():
            raise ValueError(f"figure_selection requires a concrete reason: {identity}")
        if record["decision"] in {"keep", "inspect"} and not lookup[identity]["image_available"]:
            raise ValueError(f"figure_selection cannot use an unavailable crop: {identity}")
    if seen != set(lookup):
        raise ValueError("figure_selection must cover every figure candidate exactly once")


def select_figures(ctx: ToolContext, extraction: dict, selection: list[dict],
                   method_context: dict, analyze, progress) -> tuple[dict, dict]:
    """Resolve only inspect decisions visually; failed visual decisions stay explicit."""
    candidates = figure_candidates(extraction)
    validate_figure_selection(selection, candidates)
    records = {record["id"]: {**record, "basis": "text"} for record in selection}
    pending = [candidate for candidate in candidates
               if records[candidate["id"]]["decision"] == "inspect"]
    visual_status, image_evidence = "not_needed", []
    if pending:
        progress("inspect_figures", count=len(pending))
        content = [{"type": "text", "text": json.dumps(
            {"method_context": method_context, "figure_candidates": pending}, ensure_ascii=False)}]
        figures = {candidate["id"]: figure
                   for candidate, figure in zip(candidates, extraction["figures"])}
        try:
            for candidate in pending:
                path = _image_path(extraction, figures[candidate["id"]])
                if path is None:
                    raise ValueError(f"Figure crop disappeared: {candidate['id']}")
                picture = view_image(ctx, str(path))
                content.append({"type": "text", "text": candidate["id"]})
                content.extend(picture.images)
                image_evidence.append({"id": candidate["id"], **picture.metadata})
            response = analyze(FIGURE_REVIEW_PROMPT, content)
            visual = response.get("figure_selection")
            validate_figure_selection(visual, pending, decisions={"keep", "skip", "unresolved"})
        except Exception as exc:
            # Figure enrichment must not discard an otherwise verified paper. Never
            # turn a failed request into an implicit "no architecture figures" result.
            visual_status = "failed"
            progress("figure_inspection_failed", error_type=type(exc).__name__)
            visual = [{"id": candidate["id"], "decision": "unresolved",
                       "reason": f"Visual inspection failed ({type(exc).__name__}); manual review required."}
                      for candidate in pending]
        else:
            visual_status = "completed"
        for record in visual:
            records[record["id"]] = {**record, "basis": "image" if visual_status == "completed" else "unresolved"}
    final = [records[candidate["id"]] for candidate in candidates]
    warnings = [f"{record['id']}: {record['reason']}"
                for record in final if record["decision"] == "unresolved"]
    report = {"candidates": candidates, "text_selection": selection, "selection": final,
              "visual_status": visual_status, "image_evidence": image_evidence,
              "warnings": warnings}
    report = limit_to_one_figure(report)
    selected = [{**figure, "selection_reason": record["reason"]}
                for figure, record in zip(extraction["figures"], report['selection'])
                if record["decision"] == "keep"]
    progress("architecture_figures_selected", count=len(selected), warnings=warnings)
    return {**extraction, "figures": selected}, report


def publish_figures(root: Path, slug: str, extraction: dict, report: dict) -> tuple[list[str], Path]:
    """Refresh only assets owned by the previous selection; preserve other files."""
    if len(extraction['figures']) > 1:
        raise ValueError('Only one representative architecture figure may be published per paper')
    paper = root / "papers" / f"{slug}.md"
    target = metadata_path(paper, "figures")
    previous_path = metadata_path(paper, "figures")
    directory = (root / "figures" / slug).resolve()
    previous = json.loads(previous_path.read_text(encoding="utf-8")) if previous_path.is_file() else {}
    old_paths = [(root / output).resolve() for output in previous.get("outputs", [])]
    if any(path.parent != directory for path in old_paths):
        raise ValueError("Saved figure selection contains an asset outside its figure directory")
    outputs = save_architecture_figures(root, slug, extraction)
    write_json(target, {**report, "outputs": [str(Path(path).relative_to(root)) for path in outputs]})
    retained = {Path(path).resolve() for path in outputs}
    for path in old_paths:
        if path not in retained:
            path.unlink(missing_ok=True)
    return outputs, target


# Draft validation


class ExtractedPaper(NamedTuple):
    metadata: dict
    sections: dict
    title: str
    authors: list[str]
    year: int
    classification: dict
    terms: list
    references: list


@dataclass(frozen=True)
class ValidatedDraft:
    extracted: ExtractedPaper
    dataset_names: list[str]
    classifications: list[dict]


def validate_extracted(
    draft: dict,
    pages: list[dict],
    *,
    source_metadata: dict | None = None,
    research_id: str | None = None,
    blocks: list[dict] | None = None,
) -> ExtractedPaper:
    metadata = draft.get("metadata")
    sections = draft.get("sections")
    if isinstance(metadata, dict) and source_metadata:
        metadata.update(
            {
                key: value
                for key, value in source_metadata.items()
                if key in {"title", "authors", "year", "venue", "doi", "arxiv_id"}
                and value
            }
        )
    if not isinstance(metadata, dict) or not isinstance(sections, dict):
        raise ValueError("Synthesis must contain metadata and sections objects")
    # Only citation identity and classification belong to immutable metadata.
    # Model-added narrative (e.g. source_conflicts) otherwise cannot be repaired
    # by the mutable-prose revision stage. Full-source review records issues.
    metadata = {
        key: value
        for key, value in metadata.items()
        if key
        in {
            "title",
            "authors",
            "year",
            "venue",
            "doi",
            "arxiv_id",
            *storage.CLASSIFICATION_FIELDS,
        }
    }
    draft["metadata"] = metadata
    if research_id and not metadata.get("venue"):
        metadata["venue"] = "unpublished"
    title, authors, year = (
        metadata.get("title"),
        metadata.get("authors"),
        metadata.get("year"),
    )
    if (
        not isinstance(title, str)
        or not title.strip()
        or not isinstance(authors, list)
        or not authors
        or not all(isinstance(author, str) and author.strip() for author in authors)
        or type(year) is not int
        or year < 1800
        or year > 2100
    ):
        raise ValueError("Paper title, authors, or year is missing or invalid")
    # Immutable extraction/identity cannot be changed by the revision stage.
    for key in ("venue", "doi", "arxiv_id"):
        if metadata.get(key) is not None and not isinstance(metadata[key], str):
            raise ValueError(f"Invalid metadata field: {key}")
    paper_classification = storage.validate_classification(metadata)
    terms, references = validate_extraction(draft, pages, {}, check_prose=False, blocks=blocks)
    validate_knowledge_prose({"metadata": metadata}, {}, terms)
    return ExtractedPaper(
        metadata,
        sections,
        title,
        authors,
        year,
        paper_classification,
        terms,
        references,
    )


def validate_datasets(synthesis: dict[str, Any], catalog_ids: set[str]) -> list[str]:
    datasets = synthesis.get("datasets")
    if not isinstance(datasets, list):
        raise ValueError("Synthesis must contain a datasets array")
    names = []
    seen = set()
    for number, dataset in enumerate(datasets, 1):
        if not isinstance(dataset, dict):
            raise ValueError(f"Dataset {number} must be an object")
        name, evidence, catalog_id = (
            dataset.get("name"),
            dataset.get("evidence"),
            dataset.get("catalog_id"),
        )
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"Dataset {number} missing name")
        if not isinstance(evidence, str) or not evidence.strip():
            raise ValueError(f"Dataset {number} missing evidence")
        if catalog_id is not None and (
            not isinstance(catalog_id, str) or catalog_id not in catalog_ids
        ):
            raise ValueError(f"Dataset {number} has unknown catalog_id")
        normalized_name = re.sub(r"\s+", " ", name.strip()).casefold()
        if normalized_name in seen or (catalog_id and catalog_id.casefold() in seen):
            continue
        seen.add(normalized_name)
        if catalog_id:
            seen.add(catalog_id.casefold())
        label = name.strip()
        if catalog_id:
            label += f" (local catalog: {catalog_id})"
        names.append(f"{label}: {evidence.strip()}")
    return names


def validate_draft(
    draft: dict,
    document: dict,
    candidates: list[dict],
    catalog_ids: set[str],
    *,
    research_id: str | None = None,
) -> ValidatedDraft:
    """Check and normalize a private draft snapshot before review/publication."""
    extracted = validate_extracted(draft, document["pages"], research_id=research_id,
                                   blocks=document.get('blocks'))
    validate_figure_selection(draft.get("figure_selection"), candidates)
    draft["figure_selection"] = limit_to_one_figure(
        {
            "candidates": candidates,
            "selection": draft["figure_selection"],
        }
    )["selection"]
    names = validate_datasets(draft, catalog_ids)
    classifications = [
        storage.validate_classification(node)
        for node in [draft["metadata"], draft["idea"], *draft["experiments"]]
    ]
    validate_knowledge_prose(draft, draft["sections"], extracted.terms)
    return ValidatedDraft(extracted, names, classifications)


# Publication


def _verify_sections(path: Path, expected: dict[str, str], label: str) -> None:
    """Verify exactly one body per expected heading, ignoring headings inside fences."""
    lines = path.read_text(encoding="utf-8").splitlines()
    start = 0
    if lines and lines[0] == "---":
        start = next((i + 1 for i in range(1, len(lines)) if lines[i] == "---"), 0)
    headings = []
    fence = None
    for index in range(start, len(lines)):
        line = lines[index]
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if fence is not None:
            if (
                marker
                and marker[1][0] == fence[0]
                and len(marker[1]) >= fence[1]
                and not marker[2].strip()
            ):
                fence = None
            continue
        if marker:
            fence = marker[1][0], len(marker[1])
            continue
        if line.startswith("## "):
            headings.append((index, line[3:].strip()))
    bodies: dict[str, list[str]] = {}
    for index, (line, heading) in enumerate(headings):
        end = headings[index + 1][0] if index + 1 < len(headings) else len(lines)
        bodies.setdefault(heading, []).append("\n".join(lines[line + 1 : end]).strip())
    for heading, body in expected.items():
        if bodies.get(heading) != [body.strip()]:
            raise RuntimeError(
                f"{label} write could not be verified: {path} (section: {heading})"
            )


@dataclass
class WikiPublication:
    paper_path: str = ""
    page_count: int = 0
    slug: str = ""
    wiki_path: str = ""
    idea_path: str = ""
    experiment_paths: list[str] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)
    final: str = ""
    relationship_count: int = 0
    relationship_status: str = "pending"
    relationship_error: str = ""
    relationship_warnings: list[str] = field(default_factory=list)
    citation_count: int = 0
    figure_paths: list[str] = field(default_factory=list)
    figure_selection_path: str = ""
    figure_warnings: list[str] = field(default_factory=list)
    evidence_path: str = ""
    quality_path: str = ""


class WikiPublisher:
    """File publication service; model requests are explicit injected capabilities."""

    def __init__(
        self,
        project: Path,
        wiki_root: Path,
        *,
        analyze: Callable,
        on_progress: Callable | None = None,
    ):
        self.project = Path(project).resolve()
        self.wiki_root = Path(wiki_root).resolve()
        self.analyze = analyze
        self.on_progress = on_progress

    def _display(self, path: Path) -> str:
        return (
            str(path.relative_to(self.project))
            if path.is_relative_to(self.project)
            else str(path)
        )

    def _published_path(self, path: str | Path) -> Path:
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self.project / candidate
        candidate = candidate.resolve()
        if not candidate.exists() and candidate.is_relative_to(self.wiki_root):
            candidate = resolve_legacy_path(self.wiki_root, candidate.relative_to(self.wiki_root))
        if not candidate.is_relative_to(self.wiki_root):
            raise ValueError("Published page must be inside the selected Wiki")
        return candidate

    def _progress(self, action: str, **details: Any) -> None:
        """Report a durable workflow action to callers such as the CLI."""
        if self.on_progress is not None:
            self.on_progress(action, details)

    @staticmethod
    def _experiment_slug(paper_slug: str, title: str) -> str:
        digest = hashlib.sha256(title.strip().casefold().encode("utf-8")).hexdigest()[
            :12
        ]
        return f"{paper_slug}-{digest}"

    @staticmethod
    def _existing_paper_by_doi(root: Path, doi: str) -> Path | None:
        normalized = doi.strip().lower().removeprefix("https://doi.org/")
        if not normalized:
            return None
        for path in sorted((root / "papers").glob("*.md")):
            frontmatter = path.read_text(encoding="utf-8").split("\n---", 1)[0]
            match = re.search(r'^\s+doi:\s*["\']?([^"\'\s]+)', frontmatter, re.M)
            if (
                match
                and match.group(1).lower().removeprefix("https://doi.org/")
                == normalized
            ):
                return path
        return None

    def publish(
        self,
        paper_path: str,
        document: dict,
        figures: dict,
        draft: dict,
        quality_report: dict,
        validated: ValidatedDraft,
        draft_directory: Path,
        *,
        provenance=None,
        research_id=None,
        existing_paper_path=None,
        complete_relations=True,
        publication_lock=None,
        project_sources=None,
        annotations=None,
    ) -> WikiPublication:
        """Render and verify reviewed content; the caller owns approval and publication locking."""
        for text in [*draft["sections"].values(), draft["idea"]["structure"],
                     draft["idea"]["thesis"],
                     *(e[k] for e in draft["experiments"]
                       for k in ("question", "hypothesis", "protocol", "results"))]:
            errors = markdown_errors(text)
            if errors:
                raise ValueError("Invalid publication Markdown: " + "; ".join(errors))
        state = WikiPublication(
            paper_path=paper_path, page_count=len(document["pages"])
        )
        synthesis = deepcopy(draft)
        quality_report = deepcopy(quality_report)
        metadata = validated.extracted.metadata
        terms, references = validated.extracted.terms, validated.extracted.references
        ctx = ToolContext(self.project)
        pdf_sha256 = document["pdf_sha256"]
        title, authors, year = metadata["title"], metadata["authors"], metadata["year"]
        project_sources = project_sources or []
        sections = synthesis["sections"]
        state.steps.append("verify_evidence")
        figures, figure_report = select_figures(
            ctx,
            figures,
            synthesis["figure_selection"],
            {
                "title": title,
                "method": sections.get("Method", ""),
                "idea": synthesis["idea"],
            },
            self.analyze,
            lambda action, **details: self._progress(action, **details),
        )
        quality_report["abstract_provenance"] = abstract_provenance(
            synthesis["sections"].get("Abstract", ""), document
        )
        figure_report.update(
            pdf_sha256=pdf_sha256,
            document_id=document["document_id"],
            version=ANALYSIS_VERSION,
        )
        write_json(draft_directory / "figures.json", figure_report)
        state.figure_warnings = figure_report["warnings"]
        idea, experiments = synthesis["idea"], synthesis["experiments"]
        dataset_names, classifications = (
            validated.dataset_names,
            validated.classifications,
        )
        sections["Dataset and Features"] = (
            "Datasets used (deduplicated):\n"
            + (
                "\n".join(f"- {name}" for name in dataset_names)
                if dataset_names
                else "- No specific datasets were identified in this paper."
            )
            + "\n\n"
            + sections["Dataset and Features"]
        )
        state.steps.append("validate_analysis")
        self._progress(
            "analysis_validated",
            datasets=len(dataset_names),
            experiments=len(experiments),
        )

        self._progress("wait_publication")
        with publication_lock if publication_lock is not None else nullcontext():
            if (not self.wiki_root.exists() or not any(self.wiki_root.iterdir())
                    or (split_layout(self.wiki_root) and not (self.wiki_root / "graph").is_dir())):
                storage.init_wiki(str(self.wiki_root), separate_origins=True)
            root = corpus_root(self.wiki_root, "autoresearch" if research_id else "literature")
            if not (root / "papers").is_dir():
                self._progress("initialize_wiki", path=str(root))
                storage.init_wiki(str(root))
            path = (
                self._published_path(existing_paper_path)
                if existing_paper_path
                else None
            )
            if research_id:
                if not re.fullmatch(r"[a-f0-9]{32}", research_id):
                    raise ValueError("Invalid research identity")
                path = root / "papers" / f"autoresearch-{research_id}.md"
            if (
                path is not None
                and not research_id
                and (not path.is_relative_to(root / "papers") or not path.is_file())
            ):
                raise ValueError("Existing paper must be a wiki paper page")
            if path is None:
                path = self._existing_paper_by_doi(root, metadata.get("doi") or "")
            if path is None and not research_id:
                # ingest_paper also reuses title slugs/arXiv identifiers.
                candidate = root / "papers" / (storage.slugify(
                    title, storage._last_name(authors[0]) if authors else "", year) + ".md")
                arxiv_match = storage._find_existing_page_by_arxiv(
                    root, metadata.get("arxiv_id") or "") if metadata.get("arxiv_id") else None
                for existing in (candidate, arxiv_match):
                    if existing is not None and existing.is_file():
                        assert_same_identity(existing, metadata)
                        path = existing
            if path is not None and path.is_file() and not research_id:
                assert_same_identity(path, metadata)
            if path is None or not path.is_file():
                path = storage.ingest_paper(
                    str(root),
                    title=title,
                    authors=authors,
                    year=year,
                    venue=metadata.get("venue") or "",
                    doi=metadata.get("doi") or "",
                    arxiv_id=metadata.get("arxiv_id") or "",
                    prefetched_meta=metadata,
                    refresh_indexes=False,
                    slug=f"autoresearch-{research_id}" if research_id else "",
                )
            else:
                # Preserve extra notes while refreshing the verified citation metadata.
                content = path.read_text(encoding="utf-8")
                front, body = content[4:].split("\n---\n", 1)
                for key, value in (
                    ("title", title),
                    ("authors", authors),
                    ("year", year),
                    ("venue", metadata.get("venue") or "unpublished"),
                ):
                    if not research_id and key == "venue" and not metadata.get("venue"):
                        continue
                    front = re.sub(
                        rf"(?m)^{key}:.*$",
                        lambda _: f"{key}: " + json.dumps(value, ensure_ascii=False),
                        front,
                    )
                for key, value in (
                    ("doi", metadata.get("doi")),
                    ("arxiv", metadata.get("arxiv_id")),
                ):
                    if not research_id and not value:
                        continue
                    front = re.sub(
                        rf"(?m)^  {key}:.*$",
                        lambda _: f"  {key}: " + json.dumps(value or None),
                        front,
                    )
                body = re.sub(r"\A\s*# [^\n]+", lambda _: f"\n# {title}", body, count=1)
                path.write_text("---\n" + front + "\n---\n" + body, encoding="utf-8")
            state.slug = path.stem
            state.steps.append("ingest_paper")
            self._progress("write_paper", path=self._display(path))
            evidence_file = root / "sources" / f"{document['document_id']}.json"
            quality_file = metadata_path(path, "quality")
            write_json(evidence_file, document)
            state.evidence_path = self._display(evidence_file)
            state.quality_path = self._display(quality_file)
            figure_outputs, selection_file = publish_figures(
                root, state.slug, figures, figure_report
            )
            state.figure_paths = [self._display(Path(p)) for p in figure_outputs]
            state.figure_selection_path = self._display(selection_file)
            storage.update_paper_sections(
                str(root), state.slug, sections, refresh_indexes=False
            )
            remove_template_placeholders(path)
            idea_slug = f"{state.slug}-paper-idea"
            self._progress("write_idea", slug=idea_slug)
            idea_path = root / "ideas" / f"{idea_slug}.md"
            if idea_path.exists():
                content = idea_path.read_text(encoding="utf-8")
                content = re.sub(r"(?m)^title:.*$", lambda _: "title: " + json.dumps(idea["title"], ensure_ascii=False), content, count=1)
                content = re.sub(r"(?m)^# [^\n]+", lambda _: "# " + idea["title"], content, count=1)
                idea_path.write_text(content, encoding="utf-8")
                storage.update_idea_sections(
                    str(root),
                    idea_slug,
                    {
                        "Thesis": idea["thesis"],
                        "strcture": idea["structure"],
                    },
                    refresh_indexes=False,
                )
            else:
                storage.upsert_idea(
                    str(root),
                    idea_slug,
                    idea["title"],
                    description=(
                        "Research idea from this autoresearch project."
                        if research_id
                        else "Research idea reconstructed from the cited paper; not an agent-generated proposal."
                    ),
                    stage="proposed",
                    outcome="unknown",
                    thesis=idea["thesis"],
                    structure=idea["structure"],
                    tags=["autoresearch" if research_id else "paper-extracted"],
                    based_on=[f"paper:{state.slug}"],
                    refresh_indexes=False,
                )
            state.idea_path = self._display(idea_path)
            state.steps.append("upsert_idea")
            self._progress("write_experiments", count=len(experiments))
            for experiment in experiments:
                exp_slug = self._experiment_slug(state.slug, experiment["title"])
                exp_path = storage.add_experiment(
                    str(root),
                    exp_slug,
                    title=f"{'Project-recorded' if research_id else 'Paper-reported'}: {experiment['title']}",
                    idea=f"idea:{idea_slug}",
                    question=experiment["question"],
                    hypothesis=experiment["hypothesis"],
                    protocol=experiment["protocol"],
                    results=experiment["results"],
                    tags=["autoresearch" if research_id else "paper-reported"],
                    update_on_exist=True,
                    preserve_notes=True,
                    refresh_indexes=False,
                )
                state.experiment_paths.append(self._display(exp_path))
            if not research_id:
                retire_generated_experiments(root, idea_slug,
                                             {Path(p).name for p in state.experiment_paths},
                                             "paper-reported")
            state.steps.append("add_reported_experiments")
            node_paths = [
                path,
                idea_path,
                *(self.project / p for p in state.experiment_paths),
            ]
            for node_path, classification in zip(node_paths, classifications):
                storage.update_node_classification(node_path, classification)
            self._progress("write_citations", references=len(references))
            save_references(path, terms, references)
            citation_counts = refresh_citations(root)
            state.citation_count = citation_counts.get(f"paper:{state.slug}", 0)
            self._progress("rebuild_wiki_indexes")
            storage.rebuild_index(str(root))
            self._progress("verify_outputs")
            for node_path, classification in zip(node_paths, classifications):
                frontmatter = node_path.read_text(encoding="utf-8").split("\n---\n", 1)[
                    0
                ]
                for key, values in classification.items():
                    match = re.search(rf"^{key}: (.*)$", frontmatter, re.M)
                    if not match or json.loads(match.group(1)) != values:
                        raise RuntimeError(
                            f"Wiki classification write could not be verified: {node_path}"
                        )
            for final_path in node_paths:
                errors = markdown_errors(final_path.read_text(encoding="utf-8"))
                if errors:
                    raise RuntimeError(f"Invalid final Wiki page {final_path}: {errors}")
            _verify_sections(path, sections, "Wiki")
            _verify_sections(
                idea_path,
                {"Thesis": idea["thesis"], "strcture": idea["structure"]},
                "Wiki idea",
            )
            for relative, experiment in zip(state.experiment_paths, experiments):
                _verify_sections(
                    self.project / relative,
                    {
                        heading: experiment[heading.lower()]
                        for heading in ("Question", "Hypothesis", "Protocol", "Results")
                    },
                    "Wiki experiment",
                )
            edges = [
                json.loads(line)
                for line in (library_root(root) / "graph" / "edges.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
                if line.strip()
            ]
            expected = {(f"idea:{idea_slug}", f"paper:{state.slug}", "inspired_by")}
            expected.update(
                (f"idea:{idea_slug}", f"exp:{Path(p).stem}", "tested_by")
                for p in state.experiment_paths
            )
            actual = {(edge["from"], edge["to"], edge["type"]) for edge in edges}
            if (
                not idea_path.is_file()
                or not all((self.project / p).is_file() for p in state.experiment_paths)
                or not expected <= actual
            ):
                raise RuntimeError("Wiki entity write could not be verified")
            from shutil import copy2
            archived_pdf = root / "raw-paper" / f"{state.slug}.pdf"
            archived_pdf.parent.mkdir(parents=True, exist_ok=True)
            input_pdf = self.project.joinpath(state.paper_path).resolve()
            if input_pdf.is_relative_to(root / "raw-paper"):
                archived_pdf = input_pdf
            elif input_pdf != archived_pdf.resolve():
                copy2(input_pdf, archived_pdf)
            source_record = {
                "source": provenance or {},
                "annotations": annotations or [],
                "pdf_path": str(archived_pdf),
                "pdf_sha256": pdf_sha256,
                "document_id": document["document_id"],
                "parser": document["parser"],
                "reader_version": READER_VERSION,
                "preprocessing_version": PREPROCESSING_VERSION,
                "evidence_path": state.evidence_path,
                "quality_path": state.quality_path,
                "figure_selection_path": state.figure_selection_path,
                "content_origin": "autoresearch" if research_id else "paper_reported",
                **(
                    {
                        "research_id": research_id,
                        "project_sources": [
                            {key: item[key] for key in ("path", "sha256")}
                            for item in project_sources or []
                        ],
                    }
                    if research_id
                    else {}
                ),
            }
            write_json(metadata_path(path, "source"), source_record)
            quality_report["wiki_content_sha256"] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
            quality_report["figure_selection_path"] = state.figure_selection_path
            quality_report["figure_warnings"] = state.figure_warnings
            if state.figure_warnings and quality_report["status"] == "passed":
                quality_report["status"] = "accepted_with_issues"
            write_json(quality_file, quality_report)
            state.wiki_path = self._display(path)
            state.steps.append("verify_write")
            self._progress("wiki_published", wiki_path=state.wiki_path)
            # Publication is complete. Semantic enrichment must not invalidate it.
            try:
                if complete_relations:
                    result = self.refresh_relationships(path)
                    state.relationship_status = result["status"]
                    state.relationship_count = result["count"]
                    state.relationship_warnings = result.get("warnings", [])
                else:
                    write_json(
                        metadata_path(path, "relations"),
                        {"status": "pending", "count": 0},
                    )
                    self._progress("relationships_pending", wiki_path=state.wiki_path)
            except Exception as exc:
                state.relationship_status = "failed"
                state.relationship_error = f"{type(exc).__name__}: {exc}"
                self._progress(
                    "relationships_failed",
                    wiki_path=state.wiki_path,
                    error=state.relationship_error,
                )
            relationship_summary = (
                f"{state.relationship_count} semantic cross-paper relationships"
                if state.relationship_status == "completed"
                else f"semantic relationships {state.relationship_status} (retry with --relations-only {state.wiki_path})"
            )
            if state.relationship_warnings:
                relationship_summary += (
                    f" ({len(state.relationship_warnings)} recording warnings)"
                )
            state.final = (
                f"Wrote {state.wiki_path}, 1 idea and "
                f"{len(state.experiment_paths)} reported experiments "
                f"from {state.page_count} PDF pages; "
                f"{relationship_summary}, "
                f"{state.citation_count} outgoing citations; "
                f"{len(state.figure_paths) // 2} architecture figures"
                + (
                    f" ({len(state.figure_warnings)} unresolved figure decisions; see {state.figure_selection_path})"
                    if state.figure_warnings
                    else ""
                )
            )
            self._progress(
                "complete",
                wiki_path=state.wiki_path,
                idea_path=state.idea_path,
                experiment_count=len(state.experiment_paths),
                relationship_count=state.relationship_count,
                relationship_status=state.relationship_status,
                relationship_warnings=state.relationship_warnings,
                figure_count=len(state.figure_paths) // 2,
                figure_warnings=state.figure_warnings,
                citation_count=state.citation_count,
                page_count=state.page_count,
            )
            return state

    def refresh_relationships(self, paper_path: str | Path) -> dict[str, Any]:
        """Enrich published notes without parsing, generating or reviewing them again."""
        path = self._published_path(paper_path)
        root = path.parent.parent
        if path.parent != root / "papers" or path.suffix != ".md" or not path.is_file():
            raise ValueError(
                "Relationship completion requires a published wiki paper page"
            )
        idea_path = root / "ideas" / f"{path.stem}-paper-idea.md"
        if not idea_path.is_file():
            raise ValueError("The paper's extracted Idea page is missing")
        status_path = metadata_path(path, "relations")
        write_json(status_path, {"status": "pending", "count": 0})
        self._progress("analyze_relationships", wiki_path=self._display(path))
        try:
            relations = discover_relations(root, path, idea_path, self.analyze)
            warnings = write_relations(root, f"paper:{path.stem}", relations)
        except Exception as exc:
            try:
                write_json(
                    status_path,
                    {
                        "status": "failed",
                        "count": 0,
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                )
            except OSError as recording_error:
                logger.warning(
                    "Relationship failure status could not be saved: %s",
                    recording_error,
                )
            raise
        result: dict[str, Any] = {"status": "completed", "count": len(relations)}
        if warnings:
            result["warnings"] = warnings
        # The graph is committed. Recording/notification failures cannot undo it.
        try:
            write_json(status_path, result)
        except OSError as exc:
            warning = (
                f"Relationships committed; completion status could not be saved: {exc}"
            )
            warnings.append(warning)
            logger.warning(warning)
        try:
            self._progress(
                "relationships_written", count=len(relations), warnings=warnings
            )
        except Exception as exc:
            warning = f"Relationships committed; completion notification failed: {exc}"
            warnings.append(warning)
            logger.warning(warning)
        if warnings:
            result["warnings"] = warnings
        return result


def retire_experiments(root: Path, research_id: str, current: set[Path]) -> None:
    """Keep superseded project experiments out of retrieval, retaining their old notes."""
    root = corpus_root(root, "autoresearch")
    removed = set()
    for path in (root / "experiments").glob(f"autoresearch-{research_id}-*.md"):
        if path.resolve() in current:
            continue
        archive = root / "archive" / "autoresearch" / research_id / str(uuid4().hex)
        archive.mkdir(parents=True, exist_ok=True)
        path.replace(archive / path.name)
        removed.add(f"exp:{path.stem}")
    if removed:
        graph = library_root(root) / "graph/edges.jsonl"
        edges = [
            json.loads(line) for line in graph.read_text().splitlines() if line.strip()
        ]
        graph.write_text(
            "".join(
                json.dumps(e, ensure_ascii=False) + "\n"
                for e in edges
                if e["from"] not in removed and e["to"] not in removed
            ),
            encoding="utf-8",
        )
        storage.rebuild_index(str(root))
