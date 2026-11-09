"""Fixed workflow for reading a local paper into the research wiki."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from paper_agent.tools import ToolContext, research_wiki
from paper_agent.tools.literature import discover_local_papers
from paper_agent.tools.paper_figures import (
    VERSION as FIGURES_VERSION, READER_VERSION, read_mineru_paper, save_architecture_figures,
)

from .model_client import ModelClient, load_env_file
from paper_agent.tools.paper_preprocessing import source_context, PREPROCESSING_VERSION
from .wiki_quality import QUALITY_VERSION, prepare_analysis, write_json
from .wiki_prompts import GENERATION_PROMPT, DEFAULT_TASK
from .wiki_relations import classification_vocabulary, discover_relations, write_relations
from .wiki_citations import (
    ANALYSIS_VERSION, validate_extraction, validate_knowledge_prose, save_references, refresh_citations,
)


@dataclass
class WikiState:
    paper_path: str = ""
    page_count: int = 0
    slug: str = ""
    wiki_path: str = ""
    idea_path: str = ""
    experiment_paths: list[str] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)
    final: str = ""
    relationship_count: int = 0
    citation_count: int = 0
    figure_paths: list[str] = field(default_factory=list)
    evidence_path: str = ""
    quality_path: str = ""


class WikiAgent:
    """Run a fixed PDF-to-wiki workflow; the model only analyzes paper content."""

    def __init__(self, project: str | Path, *, model: str | None = None,
                 model_client: ModelClient | None = None, max_pages: int | None = None,
                 api_key: str | None = None,
                 base_url: str | None = None, timeout: float = 300,
                 on_progress: Callable[[str, dict[str, Any]], None] | None = None):
        self.project = Path(project).resolve()
        repo_root = Path(__file__).resolve().parents[2]
        self.dataset_catalog = self.project / "电池资源库" / "电芯" / "数据集"
        if not self.dataset_catalog.is_dir():
            self.dataset_catalog = repo_root / "电池资源库" / "电芯" / "数据集"
        load_env_file(repo_root / ".env")
        load_env_file(repo_root / ".env.example")
        selected_model = model or os.environ.get("PAPER_AGENT_MODEL", "gpt-4o")
        if (max_pages is not None and max_pages < 1) or timeout <= 0:
            raise ValueError("max_pages, when set, and timeout must be positive")
        self.model = model_client or ModelClient(selected_model, api_key=api_key,
                                                base_url=base_url, timeout=timeout)
        self.max_pages = max_pages
        self.on_progress = on_progress

    def _progress(self, action: str, **details: Any) -> None:
        """Report a durable workflow action to callers such as the CLI."""
        if self.on_progress is not None:
            self.on_progress(action, details)

    def _analyze(self, system: str, user: str) -> dict[str, Any]:
        response = self.model.complete([{"role": "system", "content": system},
                                        {"role": "user", "content": user}], [])
        if response.tool_calls:
            raise ValueError("Wiki analysis model must return JSON, not tool calls")
        content = response.content.strip()
        if content.startswith("```json") and content.endswith("```"):
            content = content[7:-3].strip()
        try:
            result = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ValueError("Wiki analysis model returned invalid JSON") from exc
        if not isinstance(result, dict):
            raise ValueError("Wiki analysis model must return a JSON object")
        return result

    def _select_paper(self, ctx: ToolContext, prompt: str, paper_path: str | Path | None) -> str:
        if paper_path is None:
            discovery = discover_local_papers(ctx, query=prompt, max_papers=100)
            papers = json.loads(discovery.output)["papers"]
            if len(papers) != 1:
                raise ValueError(f"Found {len(papers)} local PDFs; specify paper_path explicitly")
            paper_path = papers[0]["path"]
        target = ctx.resolve(paper_path)
        if not target.is_file() or target.suffix.lower() != ".pdf":
            raise ValueError(f"PDF not found in workspace: {paper_path}")
        return str(target.relative_to(self.project))

    def _read_full_paper(self, ctx: ToolContext, paper_path: str) -> dict[str, Any]:
        paper = read_mineru_paper(self.project, ctx.resolve(paper_path))
        if self.max_pages is not None and len(paper["pages"]) > self.max_pages:
            raise ValueError(f"Paper has {len(paper['pages'])} pages; max_pages={self.max_pages}")
        return paper

    def _catalog_context(self) -> tuple[dict[str, Any], set[str]]:
        index = self.dataset_catalog.parent / "共享指南" / "数据集目录.md"
        if not index.is_file():
            index = self.dataset_catalog / "数据集目录.md"
        readmes = sorted(self.dataset_catalog.glob("*_README.md"))
        catalog_ids = {path.name.removesuffix("_README.md") for path in readmes}
        source_notes = {}
        for path in readmes:
            content = path.read_text(encoding="utf-8")
            if "## BatteryLife 数据集检索\n" in content:
                source_notes[path.name.removesuffix("_README.md")] = content.split(
                    "## BatteryLife 数据集检索\n", 1)[1].strip()
        return ({"path": str(self.dataset_catalog),
                 "index": index.read_text(encoding="utf-8") if index.is_file() else "",
                 "catalog_ids": sorted(catalog_ids),
                 "source_notes": source_notes}, catalog_ids)

    @staticmethod
    def _validate_datasets(synthesis: dict[str, Any], catalog_ids: set[str]) -> list[str]:
        datasets = synthesis.get("datasets")
        if not isinstance(datasets, list):
            raise ValueError("Synthesis must contain a datasets array")
        names = []
        seen = set()
        for number, dataset in enumerate(datasets, 1):
            if not isinstance(dataset, dict):
                raise ValueError(f"Dataset {number} must be an object")
            name, evidence, catalog_id = (dataset.get("name"), dataset.get("evidence"),
                                          dataset.get("catalog_id"))
            if not isinstance(name, str) or not name.strip():
                raise ValueError(f"Dataset {number} missing name")
            if not isinstance(evidence, str) or not evidence.strip():
                raise ValueError(f"Dataset {number} missing evidence")
            if catalog_id is not None and (not isinstance(catalog_id, str)
                                           or catalog_id not in catalog_ids):
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

    @staticmethod
    def _experiment_slug(paper_slug: str, title: str) -> str:
        digest = hashlib.sha256(title.strip().casefold().encode("utf-8")).hexdigest()[:12]
        return f"{paper_slug}-{digest}"

    @staticmethod
    def _existing_paper_by_doi(root: Path, doi: str) -> Path | None:
        normalized = doi.strip().lower().removeprefix("https://doi.org/")
        if not normalized:
            return None
        for path in sorted((root / "papers").glob("*.md")):
            frontmatter = path.read_text(encoding="utf-8").split("\n---", 1)[0]
            match = re.search(r'^\s+doi:\s*["\']?([^"\'\s]+)', frontmatter, re.M)
            if match and match.group(1).lower().removeprefix("https://doi.org/") == normalized:
                return path
        return None

    def go(self, prompt: str, *, paper_path: str | Path | None = None,
           source_metadata: dict[str, Any] | None = None,
           annotations: list[dict[str, Any]] | None = None,
           provenance: dict[str, Any] | None = None,
           existing_paper_path: str | None = None) -> WikiState:
        if not prompt.strip():
            raise ValueError("prompt must not be empty")
        ctx = ToolContext(self.project)
        state = WikiState()
        self._progress("start", project=str(self.project))
        self._progress("select_paper", requested_path=str(paper_path or "auto"))
        state.paper_path = self._select_paper(ctx, prompt, paper_path)
        state.steps.append("select_paper")
        self._progress("paper_selected", paper_path=state.paper_path)

        self._progress("read_pdf", paper_path=state.paper_path)
        figures = self._read_full_paper(ctx, state.paper_path)
        pages = figures["pages"]
        document = figures.get("document")
        pdf_sha256 = hashlib.sha256(ctx.resolve(state.paper_path).read_bytes()).hexdigest()
        if not document or document.get("pdf_sha256") != pdf_sha256:
            raise ValueError("Structured source document is missing or does not match PDF")
        draft_directory = self.project / ".paper-agent/wiki-preprocessing" / document["document_id"]
        write_json(draft_directory / "source.json", document)
        state.page_count = len(pages)
        state.steps.append("read_full_paper")
        self._progress("pdf_read", page_count=state.page_count)
        self._progress("extract_architecture_figures")
        self._progress("architecture_figures_extracted", status=figures["status"],
                       count=len(figures["figures"]))
        state.steps.append("extract_architecture_figures")
        self._progress("load_dataset_catalog")
        catalog_context, catalog_ids = self._catalog_context()
        vocabulary = classification_vocabulary(ctx.resolve("research-wiki"))
        self._progress("analyze_paper", page_count=state.page_count)
        print(f"Analyzing all {state.page_count} PDF pages", file=sys.stderr, flush=True)
        synthesis = self._analyze(
            GENERATION_PROMPT,
            json.dumps({"task": prompt, "paper_path": state.paper_path,
                        "source_document": source_context(document),
                        "dataset_catalog": catalog_context,
                        "existing_classification_labels": vocabulary,
                        "source_metadata": source_metadata or {},
                        "user_annotations": annotations or []},
                       ensure_ascii=False),
        )
        metadata = synthesis.get("metadata")
        sections = synthesis.get("sections")
        if isinstance(metadata, dict) and source_metadata:
            metadata.update({key: value for key, value in source_metadata.items()
                             if key in {"title", "authors", "year", "venue", "doi", "arxiv_id"}
                             and value})
        if not isinstance(metadata, dict) or not isinstance(sections, dict):
            raise ValueError("Synthesis must contain metadata and sections objects")
        title, authors, year = metadata.get("title"), metadata.get("authors"), metadata.get("year")
        if (not isinstance(title, str) or not title.strip()
                or not isinstance(authors, list) or not authors
                or not all(isinstance(author, str) and author.strip() for author in authors)
                or type(year) is not int or year < 1800 or year > 2100):
            raise ValueError("Paper title, authors, or year is missing or invalid")
        # Immutable extraction/identity cannot be changed by the revision stage.
        for key in ("venue", "doi", "arxiv_id"):
            if metadata.get(key) is not None and not isinstance(metadata[key], str):
                raise ValueError(f"Invalid metadata field: {key}")
        paper_classification = research_wiki.validate_classification(metadata)
        terms, references = validate_extraction(synthesis, pages, {}, check_prose=False)
        validate_knowledge_prose({"metadata": metadata}, {}, terms)
        validated: dict[str, Any] = {}

        def validate_draft(draft: dict[str, Any]) -> None:
            dataset_names = self._validate_datasets(draft, catalog_ids)
            classifications = [paper_classification, *(
                research_wiki.validate_classification(node)
                for node in [draft["idea"], *draft["experiments"]]
            )]
            validate_knowledge_prose(draft, draft["sections"], terms)
            validated.update(dataset_names=dataset_names, classifications=classifications)

        synthesis, quality_report = prepare_analysis(
            synthesis, document, self._analyze, draft_directory,
            lambda action, **details: self._progress(action, **details),
            model_name=getattr(self.model, "model", "injected"), source_saved=True,
            validate=validate_draft,
            revision_context={"dataset_catalog": catalog_context,
                              "existing_classification_labels": vocabulary})
        sections = synthesis["sections"]
        state.steps.append("verify_evidence")
        idea, experiments = synthesis["idea"], synthesis["experiments"]
        dataset_names, classifications = validated["dataset_names"], validated["classifications"]
        sections["Dataset and Features"] = (
            "Datasets used (deduplicated):\n" +
            ("\n".join(f"- {name}" for name in dataset_names) if dataset_names
             else "- 本文未识别出具体数据集。") + "\n\n" + sections["Dataset and Features"]
        )
        state.steps.append("validate_analysis")
        self._progress("analysis_validated", datasets=len(dataset_names),
                       experiments=len(experiments))

        root = ctx.resolve("research-wiki")
        if not (root / "papers").is_dir():
            self._progress("initialize_wiki", path=str(root))
            research_wiki.init_wiki(str(root))
        path = ctx.resolve(existing_paper_path) if existing_paper_path else None
        if path is not None and (not path.is_relative_to(root / "papers") or not path.is_file()):
            raise ValueError("Existing paper must be a wiki paper page")
        if path is None:
            path = self._existing_paper_by_doi(root, metadata.get("doi") or "")
        if path is None:
            path = research_wiki.ingest_paper(
                str(root), title=title, authors=authors, year=year,
                venue=metadata.get("venue") or "", doi=metadata.get("doi") or "",
                arxiv_id=metadata.get("arxiv_id") or "",
                prefetched_meta=metadata, refresh_indexes=False,
            )
        state.slug = path.stem
        state.steps.append("ingest_paper")
        self._progress("write_paper", path=str(path.relative_to(self.project)))
        evidence_file = root / "sources" / f"{document['document_id']}.json"
        quality_file = path.with_suffix(".quality.json")
        write_json(evidence_file, document)
        state.evidence_path = str(evidence_file.relative_to(self.project))
        state.quality_path = str(quality_file.relative_to(self.project))
        figure_outputs = save_architecture_figures(root, state.slug, figures)
        state.figure_paths = [str(Path(p).relative_to(self.project)) for p in figure_outputs]
        research_wiki.update_paper_sections(str(root), state.slug, sections, refresh_indexes=False)
        idea_slug = f"{state.slug}-paper-idea"
        self._progress("write_idea", slug=idea_slug)
        idea_path = root / "ideas" / f"{idea_slug}.md"
        if idea_path.exists():
            research_wiki.update_idea_sections(str(root), idea_slug, {
                "Thesis": idea["thesis"], "strcture": idea["structure"],
            }, refresh_indexes=False)
        else:
            research_wiki.upsert_idea(
                str(root), idea_slug, idea["title"],
                description=("Research idea reconstructed from the cited paper; not an "
                             "agent-generated proposal."),
                stage="proposed", outcome="unknown", thesis=idea["thesis"],
                structure=idea["structure"],
                tags=["paper-extracted"], based_on=[f"paper:{state.slug}"],
                refresh_indexes=False,
            )
        state.idea_path = str(idea_path.relative_to(self.project))
        state.steps.append("upsert_idea")
        self._progress("write_experiments", count=len(experiments))
        for experiment in experiments:
            exp_slug = self._experiment_slug(state.slug, experiment["title"])
            exp_path = research_wiki.add_experiment(
                str(root), exp_slug, title=f"Paper-reported: {experiment['title']}",
                idea=f"idea:{idea_slug}",
                question=experiment["question"], hypothesis=experiment["hypothesis"],
                protocol=experiment["protocol"], results=experiment["results"],
                tags=["paper-reported"], update_on_exist=True,
                preserve_notes=True, refresh_indexes=False,
            )
            state.experiment_paths.append(str(exp_path.relative_to(self.project)))
        state.steps.append("add_reported_experiments")
        node_paths = [path, idea_path, *(self.project / p for p in state.experiment_paths)]
        for node_path, classification in zip(node_paths, classifications):
            research_wiki.update_node_classification(node_path, classification)
        self._progress("write_citations", references=len(references))
        save_references(path, terms, references)
        citation_counts = refresh_citations(root)
        state.citation_count = citation_counts.get(f"paper:{state.slug}", 0)
        self._progress("analyze_relationships")
        relations = discover_relations(root, path, idea_path, self._analyze)
        write_relations(root, f"paper:{state.slug}", relations)
        state.relationship_count = len(relations)
        self._progress("relationships_written", count=state.relationship_count)
        self._progress("rebuild_wiki_indexes")
        research_wiki.rebuild_index(str(root))
        self._progress("verify_outputs")
        for node_path, classification in zip(node_paths, classifications):
            frontmatter = node_path.read_text(encoding="utf-8").split("\n---\n", 1)[0]
            for key, values in classification.items():
                match = re.search(rf"^{key}: (.*)$", frontmatter, re.M)
                if not match or json.loads(match.group(1)) != values:
                    raise RuntimeError(f"Wiki classification write could not be verified: {node_path}")
        record = path.read_text(encoding="utf-8")
        if not all(body.strip() in record for body in sections.values()):
            raise RuntimeError("Wiki write could not be verified")
        idea_record = idea_path.read_text(encoding="utf-8")
        if ("## strcture\n" not in idea_record
                or not all(idea[key].strip() in idea_record
                           for key in ("thesis", "structure"))):
            raise RuntimeError("Wiki idea write could not be verified")
        for relative, experiment in zip(state.experiment_paths, experiments):
            experiment_record = (self.project / relative).read_text(encoding="utf-8")
            if not all(experiment[key].strip() in experiment_record
                       for key in ("question", "hypothesis", "protocol", "results")):
                raise RuntimeError(f"Wiki experiment write could not be verified: {relative}")
        edges = [json.loads(line) for line in (root / "graph" / "edges.jsonl").read_text(
            encoding="utf-8").splitlines() if line.strip()]
        expected = {(f"idea:{idea_slug}", f"paper:{state.slug}", "inspired_by")}
        expected.update((f"idea:{idea_slug}", f"exp:{Path(p).stem}", "tested_by")
                        for p in state.experiment_paths)
        expected.update((edge["from"], edge["to"], edge["type"]) for edge in relations)
        actual = {(edge["from"], edge["to"], edge["type"]) for edge in edges}
        if not idea_path.is_file() or not all((self.project / p).is_file()
                                              for p in state.experiment_paths) or not expected <= actual:
            raise RuntimeError("Wiki entity write could not be verified")
        source_record = {"source": provenance or {}, "annotations": annotations or [],
                         "pdf_path": state.paper_path, "pdf_sha256": pdf_sha256,
                         "document_id": document["document_id"], "parser": document["parser"],
                         "reader_version": READER_VERSION, "preprocessing_version": PREPROCESSING_VERSION,
                         "evidence_path": state.evidence_path, "quality_path": state.quality_path,
                         "content_origin": "paper_reported"}
        write_json(path.with_suffix(".source.json"), source_record)
        quality_report["wiki_content_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        write_json(quality_file, quality_report)
        state.wiki_path = str(path.relative_to(self.project))
        state.steps.append("verify_write")
        state.final = (f"Wrote {state.wiki_path}, 1 idea and "
                       f"{len(state.experiment_paths)} reported experiments "
                       f"from {state.page_count} PDF pages; "
                       f"{state.relationship_count} semantic cross-paper relationships, "
                       f"{state.citation_count} outgoing citations")
        self._progress("complete", wiki_path=state.wiki_path,
                       idea_path=state.idea_path,
                       experiment_count=len(state.experiment_paths),
                       relationship_count=state.relationship_count,
                       citation_count=state.citation_count,
                       page_count=state.page_count)
        return state

    def ingest_zotero(self, client: Any, *, item_key: str | None = None,
                      collection_key: str | None = None,
                      prompt: str = DEFAULT_TASK,
                      force: bool = False) -> dict[str, Any]:
        """Import one item or a collection; failures are recorded per item."""
        if bool(item_key) == bool(collection_key):
            raise ValueError("Specify exactly one Zotero item key or collection key")
        keys = ([client.key(item_key)] if item_key else
                [record["key"] for record in client.collection_items(collection_key)])
        root = ToolContext(self.project).resolve("research-wiki")
        manifest_path = root / "zotero-sync.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
        results = []
        for key in dict.fromkeys(keys):
            identity = f"{client.library_type}/{client.library_id}/{key}"
            previous = manifest.get(identity, {})
            try:
                self._progress("fetch_zotero", item_key=key)
                source = client.prepare(self.project, key)
                # Include task/model so changed analysis settings don't silently reuse old output.
                signature = hashlib.sha256(json.dumps(
                    [source.fingerprint, prompt, getattr(self.model, "model", "injected"),
                     ANALYSIS_VERSION, QUALITY_VERSION, PREPROCESSING_VERSION,
                     READER_VERSION, FIGURES_VERSION, "independent-figure-library-v1",
                     os.environ.get("PAPER_AGENT_MINERU_PYTHON"), os.environ.get("MINERU_HOME")]
                ).encode()).hexdigest()
                outputs = previous.get("outputs", [])
                if (not force and previous.get("status") == "completed"
                        and previous.get("signature") == signature and outputs
                        and all(ToolContext(self.project).resolve(p).is_file() for p in outputs)):
                    results.append({"item_key": key, "status": "skipped", "wiki_path": outputs[0]})
                    self._progress("zotero_skipped", item_key=key)
                    continue
                state = self.go(prompt, paper_path=source.pdf_path,
                                source_metadata=source.metadata, annotations=source.annotations,
                                provenance=source.provenance,
                                existing_paper_path=(outputs[0] if outputs and
                                    ToolContext(self.project).resolve(outputs[0]).is_file() else None))
                # Archive superseded experiments from this import, preserving their history.
                removed = []
                for old in previous.get("outputs", []):
                    old_path = ToolContext(self.project).resolve(old)
                    if (old_path.parent == root / "experiments" and old_path.is_file()
                            and old not in state.experiment_paths):
                        archive = root / "archive" / "zotero" / key
                        archive.mkdir(parents=True, exist_ok=True)
                        old_path.replace(archive / old_path.name)
                        removed.append(f"exp:{old_path.stem}")
                if removed:
                    edge_path = root / "graph" / "edges.jsonl"
                    edges = [json.loads(line) for line in edge_path.read_text(encoding="utf-8").splitlines() if line.strip()]
                    edge_path.write_text("".join(json.dumps(edge, ensure_ascii=False) + "\n"
                        for edge in edges if edge["from"] not in removed and edge["to"] not in removed), encoding="utf-8")
                    research_wiki.rebuild_index(str(root))
                outputs = [state.wiki_path, state.idea_path, *state.experiment_paths,
                           f"research-wiki/papers/{state.slug}.references.json",
                           f"research-wiki/papers/{state.slug}.source.json", state.evidence_path,
                           state.quality_path, *state.figure_paths]
                manifest[identity] = {"status": "completed", "signature": signature,
                                      "outputs": outputs, "source": source.provenance}
                results.append({"item_key": key, "status": "completed", "wiki_path": state.wiki_path})
            except Exception as exc:
                manifest[identity] = {**previous, "status": "failed", "error": str(exc)}
                results.append({"item_key": key, "status": "failed", "error": str(exc)})
                self._progress("zotero_failed", item_key=key, error=str(exc))
            root.mkdir(parents=True, exist_ok=True)
            temporary = manifest_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            temporary.replace(manifest_path)
        return {"items": results, "completed": sum(r["status"] == "completed" for r in results),
                "skipped": sum(r["status"] == "skipped" for r in results),
                "failed": sum(r["status"] == "failed" for r in results)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Read a local PDF or Zotero papers into the research wiki")
    parser.add_argument("prompt", nargs="*", help="Paper-reading task")
    zotero = parser.add_mutually_exclusive_group()
    zotero.add_argument("--zotero-item", help="Parent bibliographic item key")
    zotero.add_argument("--zotero-collection", help="Collection key (direct members)")
    parser.add_argument("--zotero-library-id", default=None)
    parser.add_argument("--zotero-library-type", choices=["users", "groups"], default=None)
    parser.add_argument("--zotero-data-dir", type=Path, default=None,
                        help="Local Zotero data directory; otherwise use Web API")
    parser.add_argument("--force", action="store_true", help="Reanalyze unchanged Zotero items")
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("--paper", type=Path, default=None, help="PDF path inside the project")
    parser.add_argument("--model", default=None)
    parser.add_argument("--timeout", type=float, default=300,
                        help="Timeout in seconds for each model request")
    parser.add_argument("--max-pages", type=int, default=None)
    args = parser.parse_args()
    def progress(action: str, details: dict[str, Any]) -> None:
        suffix = " ".join(f"{key}={value}" for key, value in details.items())
        print(f"[wiki] {action}" + (f" | {suffix}" if suffix else ""),
              flush=True)

    agent = WikiAgent(args.project, model=args.model, max_pages=args.max_pages,
                      timeout=args.timeout, on_progress=progress)
    prompt = " ".join(args.prompt) or DEFAULT_TASK
    if args.zotero_item or args.zotero_collection:
        if args.paper:
            parser.error("--paper cannot be combined with Zotero import")
        from paper_agent.tools.zotero import LocalZoteroClient, ZoteroClient
        library_id = args.zotero_library_id or os.environ.get("ZOTERO_LIBRARY_ID")
        if not library_id:
            parser.error("Set ZOTERO_LIBRARY_ID or --zotero-library-id")
        library_type = args.zotero_library_type or os.environ.get("ZOTERO_LIBRARY_TYPE", "users")
        data_dir = args.zotero_data_dir or os.environ.get("ZOTERO_DATA_DIR")
        if data_dir:
            if library_type != "users":
                parser.error("Local Zotero import supports the personal library (users) only")
            client = LocalZoteroClient(library_id, data_dir)
        else:
            client = ZoteroClient(library_id, api_key=os.environ.get("ZOTERO_API_KEY", ""),
                                  library_type=library_type)
        try:
            result = agent.ingest_zotero(client, item_key=args.zotero_item,
                                         collection_key=args.zotero_collection, prompt=prompt,
                                         force=args.force)
        finally:
            client.close()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if result["failed"]:
            raise SystemExit(1)
    else:
        print(agent.go(prompt, paper_path=args.paper).final)


if __name__ == "__main__":
    main()
