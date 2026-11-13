"""Fixed workflow for reading a local paper into the research wiki."""

from __future__ import annotations

from paper_agent.tools.wiki_metadata import metadata_path, metadata_records

import argparse
import hashlib
import json
import os
import re
import sys
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Callable

from paper_agent.tools import ToolContext, research_wiki
from paper_agent.tools.literature import discover_local_papers
from paper_agent.tools.paper_figures import (
    VERSION as FIGURES_VERSION, READER_VERSION, read_mineru_paper,
)

from paper_agent.agent.model_client import ModelClient, load_env_file
from paper_agent.tools.paper_preprocessing import source_context, PREPROCESSING_VERSION
from paper_agent.tools.wiki_review import QUALITY_VERSION, prepare_analysis, request_json_analysis
from paper_agent.tools.wiki_prompts import GENERATION_PROMPT, DEFAULT_TASK, PROJECT_SOURCE_PROMPT, EXTRACTION_REPAIR_PROMPT, PROMPT_VERSION
from paper_agent.tools.wiki_relations import classification_vocabulary
from paper_agent.tools.wiki_assets import figure_candidates, validate_figure_selection, limit_to_one_figure
from paper_agent.tools.wiki_relations import ANALYSIS_VERSION, validate_knowledge_prose

from paper_agent.tools.wiki_ingest import (
    ExtractedPaper, ValidatedDraft, validate_extracted, validate_datasets,
    dataset_catalog_context, WikiPublisher, WikiPublication as WikiState,
)
from paper_agent.storage import write_json


class WikiAgent:
    """Run a fixed PDF-to-wiki workflow; the model only analyzes paper content."""

    def __init__(self, project: str | Path, *, model: str | None = None,
                 model_client: ModelClient | None = None, max_pages: int | None = None,
                 api_key: str | None = None,
                 base_url: str | None = None, timeout: float = 300,
                 on_progress: Callable[[str, dict[str, Any]], None] | None = None,
                 wiki_root: str | Path | None = None):
        self.project = Path(project).resolve()
        self.wiki_root = Path(wiki_root).expanduser().resolve() if wiki_root else self.project / "research-wiki"
        repo_root = Path(__file__).resolve().parents[3]
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

    def _display(self, path: Path) -> str:
        return str(path.relative_to(self.project)) if path.is_relative_to(self.project) else str(path)

    def _published_path(self, path: str | Path) -> Path:
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self.project / candidate
        candidate = candidate.resolve()
        if not candidate.is_relative_to(self.wiki_root):
            raise ValueError("Published page must be inside the selected Wiki")
        return candidate

    def _progress(self, action: str, **details: Any) -> None:
        """Report a durable workflow action to callers such as the CLI."""
        if self.on_progress is not None:
            self.on_progress(action, details)

    def _analyze(self, system: str, user: str | list[dict]) -> dict[str, Any]:
        return request_json_analysis(self.model, system, user)

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

    def _catalog_context(self):
        return dataset_catalog_context(self.project)

    _validate_datasets = staticmethod(validate_datasets)


    def go(self, prompt: str, *, paper_path: str | Path | None = None,
           source_metadata: dict[str, Any] | None = None,
           annotations: list[dict[str, Any]] | None = None,
           provenance: dict[str, Any] | None = None,
           existing_paper_path: str | None = None,
           complete_relations: bool = True,
           project_sources: list[dict[str, str]] | None = None,
           research_id: str | None = None,
           reuse_analysis: bool = False,
           recovery_report: str | Path | None = None,
           publication_lock: Any = None) -> WikiState:
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
        if project_sources:
            # Keep PDF pages untouched: project evidence uses file/block locators.
            document = json.loads(json.dumps(document))
            for index, source in enumerate(project_sources):
                document["blocks"].append({
                    "id": f"project-{index}", "page": None, "type": "text",
                    "text": source["content"], "source_path": source["path"],
                    "source_kind": "project_artifact", "sha256": source["sha256"],
                    "bbox": None, "parent_id": None, "section_id": None,
                    "excluded_reason": None,
                })
            document["document_id"] = hashlib.sha256(json.dumps(
                [document["document_id"], project_sources], sort_keys=True,
                ensure_ascii=False).encode()).hexdigest()
        def analyze(system, user):
            instructions = system + (PROJECT_SOURCE_PROMPT if research_id else "")
            return self._analyze(instructions, user)
        draft_directory = self.project / ".paper-agent/wiki-preprocessing" / document["document_id"]
        write_json(draft_directory / "source.json", document)
        state.page_count = len(pages)
        state.steps.append("read_full_paper")
        self._progress("pdf_read", page_count=state.page_count)
        candidates = figure_candidates(figures)
        self._progress("figure_candidates_ready", status=figures["status"], count=len(candidates))
        state.steps.append("extract_figure_candidates")
        self._progress("load_dataset_catalog")
        catalog_context, catalog_ids = self._catalog_context()
        with publication_lock if publication_lock is not None else nullcontext():
            vocabulary = classification_vocabulary(self.wiki_root)
        self._progress("analyze_paper", page_count=state.page_count)
        print(f"Analyzing all {state.page_count} PDF pages", file=sys.stderr, flush=True)
        generation_input = {"task": prompt, "paper_path": state.paper_path,
                            "source_document": source_context(document),
                            "figure_candidates": candidates, "dataset_catalog": catalog_context,
                            "existing_classification_labels": vocabulary,
                            "source_metadata": source_metadata or {}, "user_annotations": annotations or []}
        signature = hashlib.sha256(json.dumps({
            "document_id": document["document_id"], "prompt_version": PROMPT_VERSION,
            "model": getattr(self.model, "model", "injected"), "task": prompt,
            "metadata": source_metadata, "annotations": annotations, "research_id": research_id,
        }, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        checkpoint = draft_directory / "generation.json"
        cached = json.loads(checkpoint.read_text()) if reuse_analysis and checkpoint.is_file() else {}
        if cached.get("signature") == signature:
            synthesis = cached["synthesis"]
            self._progress("resume_generation", checkpoint=str(checkpoint))
        elif recovery_report is not None:
            recovery_path = Path(recovery_report).resolve()
            if not recovery_path.is_relative_to(self.project / ".paper-agent/wiki-preprocessing"):
                raise ValueError("Recovery report must be inside this project's Wiki preprocessing directory")
            recovered = json.loads(recovery_path.read_text())
            if (recovered.get("document_id") != document["document_id"]
                    or recovered.get("pdf_sha256") != pdf_sha256
                    or recovered.get("model") != getattr(self.model, "model", "injected")
                    or not recovered.get("attempts")):
                raise ValueError("Recovery report does not match the current full PDF and analysis model")
            synthesis = recovered["attempts"][-1]["draft"]
            write_json(draft_directory / "recovered-quality.json", recovered)
            write_json(checkpoint, {"signature": signature, "synthesis": synthesis,
                                    "origin": "recovered_draft", "recovery_report": str(recovery_path)})
            self._progress("recover_draft", checkpoint=str(recovery_path))
        else:
            synthesis = analyze(GENERATION_PROMPT, json.dumps(generation_input, ensure_ascii=False))
            write_json(checkpoint, {"signature": signature, "synthesis": synthesis})


        self._progress("validate_extraction")
        extraction_attempts = []
        for extraction_attempt in range(2):
            try:
                (metadata, sections, title, authors, year,
                 paper_classification, terms, references) = self.validate_extracted(
                    synthesis, pages, source_metadata=source_metadata, research_id=research_id)
                write_json(checkpoint, {"signature": signature, "synthesis": synthesis})
                write_json(draft_directory / "extraction.json", {
                    "status": "passed", "attempts": extraction_attempts,
                    "signature": signature, "synthesis": synthesis})
                break
            except ValueError as exc:
                extraction_attempts.append({"error": str(exc), "synthesis": json.loads(json.dumps(synthesis))})
                write_json(draft_directory / "extraction.json", {
                    "status": "failed" if extraction_attempt else "needs_revision",
                    "attempts": extraction_attempts, "signature": signature})
                if extraction_attempt:
                    raise ValueError(f"Wiki extraction validation failed: {exc}; diagnostic: {draft_directory / 'extraction.json'}") from exc
                self._progress("repair_extraction", errors=str(exc))
                before_repair = json.loads(json.dumps(synthesis))
                repaired = analyze(EXTRACTION_REPAIR_PROMPT, json.dumps({
                    "extraction": {key: synthesis.get(key) for key in ("metadata", "coined_terms", "references")},
                    "validation_errors": [str(exc)], "source_document": source_context(document),
                    "source_metadata": source_metadata or {},
                }, ensure_ascii=False))
                if not isinstance(repaired, dict):
                    raise ValueError("Extraction repair must return a JSON object")
                old_refs = before_repair.get("references")
                new_refs = repaired.get("references", old_refs)
                if isinstance(old_refs, list) and isinstance(new_refs, list):
                    if len(old_refs) != len(new_refs):
                        raise ValueError("Extraction repair must preserve bibliography entries and order")
                    affected = {int(index) for index in re.findall(r"references\[(\d+)\]", str(exc))}
                    for index, (old_ref, new_ref) in enumerate(zip(old_refs, new_refs)):
                        if index not in affected and old_ref != new_ref:
                            raise ValueError(f"Extraction repair changed a previously valid reference: {index}")
                # A bibliography-only request cannot alter already validated identity
                # or terms. Ignore unsolicited fields instead of failing a valid fix.
                repair_keys = (("references",) if "references[" in str(exc)
                               else ("metadata", "coined_terms", "references"))
                for key in repair_keys:
                    if key in repaired:
                        synthesis[key] = repaired[key]
        validated: dict[str, Any] = {}

        def validate_draft(draft: dict[str, Any]) -> None:
            validate_figure_selection(draft.get("figure_selection"), candidates)
            draft['figure_selection'] = limit_to_one_figure({
                'candidates': candidates, 'selection': draft['figure_selection'],
            })['selection']
            dataset_names = self._validate_datasets(draft, catalog_ids)
            classifications = [research_wiki.validate_classification(draft['metadata']), *(
                research_wiki.validate_classification(node)
                for node in [draft["idea"], *draft["experiments"]]
            )]
            validate_knowledge_prose(draft, draft["sections"], terms)
            validated.update(dataset_names=dataset_names, classifications=classifications)

        resume_marker = draft_directory / "draft-signature.json"
        previous_draft = draft_directory / "draft.json"
        if reuse_analysis and resume_marker.is_file() and previous_draft.is_file():
            marker = json.loads(resume_marker.read_text())
            if marker.get("signature") == signature:
                resumed = json.loads(previous_draft.read_text())
                for key in ("sections", "section_status", "critical_claims", "idea", "datasets",
                            "experiments", "figure_selection"):
                    if key in resumed:
                        synthesis[key] = resumed[key]
                if isinstance(resumed.get('metadata'), dict):
                    synthesis['metadata'].update({key: resumed['metadata'][key]
                        for key in research_wiki.CLASSIFICATION_FIELDS if key in resumed['metadata']})
                self._progress("resume_draft", checkpoint=str(previous_draft))
        write_json(resume_marker, {"signature": signature})
        synthesis, quality_report = prepare_analysis(
            synthesis, document, analyze, draft_directory,
            lambda action, **details: self._progress(action, **details),
            model_name=getattr(self.model, "model", "injected"), source_saved=True,
            figure_candidates=candidates,
            validate=validate_draft,
            revision_context={"dataset_catalog": catalog_context,
                              "existing_classification_labels": vocabulary})
        return self.publish_analysis(
            state, document, figures, synthesis, quality_report, validated,
            metadata, terms, references, draft_directory, provenance=provenance,
            research_id=research_id, existing_paper_path=existing_paper_path,
            complete_relations=complete_relations, publication_lock=publication_lock,
            project_sources=project_sources, annotations=annotations)

    validate_extracted = staticmethod(validate_extracted)

    def publish_analysis(self, state, document, figures, synthesis, quality_report,
                         validated, metadata, terms, references, draft_directory, **options):
        """Legacy workflow adapter; the role uses WikiPublisher directly."""
        extracted = ExtractedPaper(metadata, synthesis['sections'], metadata['title'],
                                   metadata['authors'], metadata['year'],
                                   research_wiki.validate_classification(metadata), terms, references)
        result = self._publisher().publish(
            state.paper_path, document, figures, synthesis, quality_report,
            ValidatedDraft(extracted, **validated), draft_directory, **options)
        result.steps[:0] = state.steps
        return result

    def _publisher(self):
        return WikiPublisher(self.project, self.wiki_root, analyze=self._analyze,
                             on_progress=self.on_progress)

    def refresh_relationships(self, paper_path):
        return self._publisher().refresh_relationships(paper_path)

    def ingest_zotero(self, client: Any, *, item_key: str | None = None,
                      collection_key: str | None = None,
                      prompt: str = DEFAULT_TASK,
                      force: bool = False, complete_relations: bool = True) -> dict[str, Any]:
        """Import one item or a collection; failures are recorded per item."""
        if bool(item_key) == bool(collection_key):
            raise ValueError("Specify exactly one Zotero item key or collection key")
        keys = ([client.key(item_key)] if item_key else
                [record["key"] for record in client.collection_items(collection_key)])
        root = self.wiki_root
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
                        and all(self._published_path(p).is_file() for p in outputs)):
                    results.append({"item_key": key, "status": "skipped", "wiki_path": outputs[0]})
                    self._progress("zotero_skipped", item_key=key)
                    continue
                state = self.go(prompt, paper_path=source.pdf_path,
                                source_metadata=source.metadata, annotations=source.annotations,
                                provenance=source.provenance,
                                complete_relations=complete_relations,
                                existing_paper_path=(outputs[0] if outputs and
                                    self._published_path(outputs[0]).is_file() else None))
                # Archive superseded experiments from this import, preserving their history.
                removed = []
                for old in previous.get("outputs", []):
                    old_path = self._published_path(old)
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
                           self._display(metadata_path(root / "papers" / f"{state.slug}.md", "references")),
                           self._display(metadata_path(root / "papers" / f"{state.slug}.md", "source")), state.evidence_path,
                           state.quality_path, state.figure_selection_path, *state.figure_paths]
                manifest[identity] = {"status": "completed", "signature": signature,
                                      "outputs": outputs, "source": source.provenance}
                results.append({"item_key": key, "status": "completed", "wiki_path": state.wiki_path,
                                "relationship_status": state.relationship_status,
                                "relationship_error": state.relationship_error,
                                "figure_selection_path": state.figure_selection_path,
                                "figure_warnings": state.figure_warnings,
                                "relationship_warnings": state.relationship_warnings})
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
    parser.add_argument("--force", action="store_true", help="Reanalyze unchanged Zotero items or project research")
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("--wiki-root", type=Path, default=None, help="Destination Wiki directory")
    parser.add_argument("--archive-project", action="store_true",
                        help="Archive this project's final manuscript and evidence into the shared Wiki")
    parser.add_argument("--paper", type=Path, default=None, help="PDF path inside the project")
    relations = parser.add_mutually_exclusive_group()
    relations.add_argument("--skip-relations", action="store_true",
                           help="Publish the paper and leave semantic relationships pending")
    relations.add_argument("--relations-only", type=Path,
                           help="Complete or retry relationships for a published wiki paper Markdown path")
    parser.add_argument("--model", default=None)
    parser.add_argument("--timeout", type=float, default=300,
                        help="Timeout in seconds for each model request")
    parser.add_argument("--max-pages", type=int, default=None)
    args = parser.parse_args()
    if args.relations_only and (args.paper or args.zotero_item or args.zotero_collection or args.prompt or args.archive_project):
        parser.error("--relations-only cannot be combined with a PDF, Zotero import or reading task")
    if args.archive_project and (args.zotero_item or args.zotero_collection):
        parser.error("--archive-project cannot be combined with Zotero import")
    def progress(action: str, details: dict[str, Any]) -> None:
        suffix = " ".join(f"{key}={value}" for key, value in details.items())
        print(f"[wiki] {action}" + (f" | {suffix}" if suffix else ""),
              flush=True)

    if args.archive_project:
        from paper_agent.agent.wiki.archive import archive_research
        load_env_file(Path(__file__).resolve().parents[3] / '.env')
        try:
            result = archive_research(args.project, paper_path=args.paper or 'paper/main.pdf',
                                      wiki_root=args.wiki_root, model=args.model,
                                      timeout=args.timeout, on_progress=progress, force=args.force,
                                      complete_relations=not args.skip_relations)
        except Exception as exc:
            print(f"Research archival failed: {exc}", file=sys.stderr)
            raise SystemExit(1)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    agent = WikiAgent(args.project, model=args.model, max_pages=args.max_pages,
                      timeout=args.timeout, on_progress=progress, wiki_root=args.wiki_root)
    prompt = " ".join(args.prompt) or DEFAULT_TASK
    if args.relations_only:
        try:
            result = agent.refresh_relationships(args.relations_only)
        except Exception as exc:
            print(f"Relationship completion failed: {exc}", file=sys.stderr)
            raise SystemExit(1)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.zotero_item or args.zotero_collection:
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
                                         force=args.force, complete_relations=not args.skip_relations)
        finally:
            client.close()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if result["failed"]:
            raise SystemExit(1)
    else:
        print(agent.go(prompt, paper_path=args.paper,
                       complete_relations=not args.skip_relations).final)


if __name__ == "__main__":
    main()
