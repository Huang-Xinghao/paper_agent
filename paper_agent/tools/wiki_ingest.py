"""Wiki ingestion tools: staged sources, reviewed drafts and verified publication."""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import fcntl
from functools import partial
import hashlib
import json
import logging
from pathlib import Path
from .wiki_layout import corpus_root, library_root, resolve_legacy_path, split_layout
import re
from typing import Any, Callable, NamedTuple
from uuid import uuid4

from paper_agent.storage import write_json
from . import research_wiki
from .core import ToolContext, ToolResult, ToolSpec
from .paper_figures import READER_VERSION, read_mineru_paper
from .paper_preprocessing import PREPROCESSING_VERSION, source_context
from .wiki import resolve_wiki_root
from .wiki_assets import (
    figure_candidates,
    validate_figure_selection,
    limit_to_one_figure,
    select_figures,
    publish_figures,
)
from .wiki_metadata import metadata_path
from .wiki_publication import (assert_same_identity, markdown_errors,
                               remove_template_placeholders, retire_generated_experiments)
from .wiki_prompts import PROMPT_VERSION, PROJECT_SOURCE_PROMPT
from .wiki_review import (
    QUALITY_VERSION,
    prepare_analysis,
    request_json_analysis,
    abstract_provenance,
)
from .wiki_relations import (
    ANALYSIS_VERSION,
    save_references,
    refresh_citations,
    validate_extraction,
    validate_knowledge_prose,
    discover_relations,
    write_relations,
    classification_vocabulary,
)

logger = logging.getLogger(__name__)


# Public assignment tools

CURATION_VERSION = "wiki-role-v2"


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


class WikiIngestion:
    """One assignment's tool state; receives model/progress capabilities, never an Agent."""

    def __init__(self, project: Path, *, model, on_progress=None):
        self.project = Path(project).resolve()
        self.model = model
        self.on_progress = on_progress
        self.options = {}
        self.prepared = False
        self.receipt = None
        self.approval = None
        self.read_ranges = []
        self.review_count = 0

    def assign(self, **options):
        self.options = options
        if options.get('paper_path'):
            self._restore(ToolContext(self.project).resolve_read(options['paper_path']),
                          options.get('project_evidence', False), options.get('source_paths'),
                          options.get('force', False))

    def _state_pointer(self, pdf, project_evidence):
        root = str(Path(self.options.get('wiki_root') or resolve_wiki_root()).resolve())
        key = fingerprint([str(pdf), project_evidence, root])
        return self.project / '.paper-agent/wiki-curation/active' / f'{key}.json'

    def _save_state(self):
        if not self.prepared:
            return
        fields = ('inputs', 'signature', 'research_id', 'project_evidence', 'sources',
                  'document', 'figures', 'catalog', 'vocabulary', 'candidates', 'provenance',
                  'read_ranges', 'review_count', 'last_report', 'last_review_hash', 'last_review_result')
        value = {key: getattr(self, key) for key in fields if hasattr(self, key)}
        value.update(version=CURATION_VERSION, source_version=SOURCE_VERSION,
                     quality_version=QUALITY_VERSION, prompt_version=PROMPT_VERSION,
                     pdf=str(self.pdf), root=str(self.root), job=str(self.job),
                     receipt_path=str(self.receipt_path), draft_path=str(self.draft_path),
                     catalog_ids=list(self.catalog_ids), approval=(
                         {**self.approval, 'directory': str(self.approval['directory'])}
                         if self.approval else None))
        write_json(self.job / 'state.json', value)
        write_json(self._state_pointer(self.pdf, self.project_evidence),
                   {'state_path': self._display(self.job / 'state.json')})

    def _restore(self, pdf, project_evidence, source_paths=None, force=False):
        if force or self.prepared:
            return False
        pointer = self._state_pointer(pdf, project_evidence)
        if not pointer.is_file():
            return False
        state_path = ToolContext(self.project).resolve(json.loads(pointer.read_text())['state_path'])
        value = json.loads(state_path.read_text())
        if (value.get('version'), value.get('source_version'), value.get('quality_version'), value.get('prompt_version')) != (
                CURATION_VERSION, SOURCE_VERSION, QUALITY_VERSION, PROMPT_VERSION):
            return False
        saved_sources = [item['path'] for item in value['sources']]
        # Omitted source arguments mean resume the original evidence, not remove it.
        if source_paths and not set(source_paths).issubset(saved_sources):
            self.options['source_paths'] = list(dict.fromkeys([*saved_sources, *source_paths]))
            return False
        self.options['source_paths'] = saved_sources
        ctx = ToolContext(self.project)
        if any(not (path := ctx.resolve_read(name)).is_file() or
               hashlib.sha256(path.read_bytes()).hexdigest() != digest
               for name, digest in value['inputs'].items()):
            return False
        receipt_path = ctx.resolve(value['receipt_path'])
        receipt = json.loads(receipt_path.read_text()) if receipt_path.is_file() else None
        if receipt and receipt.get('status') in {'completed', 'skipped'}:
            return False
        for key, item in value.items():
            if key not in {'version', 'source_version', 'quality_version', 'prompt_version'}:
                setattr(self, key, item)
        for key in ('pdf', 'root', 'job', 'receipt_path', 'draft_path'):
            setattr(self, key, Path(getattr(self, key)))
        self.catalog_ids = set(self.catalog_ids)
        if self.approval:
            self.approval['directory'] = Path(self.approval['directory'])
        self.publisher = WikiPublisher(self.project, self.root,
            analyze=partial(request_json_analysis, self.model), on_progress=self.on_progress)
        self.source_text = json.dumps(source_context(self.document), ensure_ascii=False, indent=2)
        self.prepared, self.receipt = True, receipt
        self._restore_review_files()
        if self.approval and (not self.draft_path.is_file() or
                fingerprint(self._load_draft(self.draft_path)) != self.approval['draft_hash']):
            self.approval = None
        if self.on_progress:
            self.on_progress('wiki_resumed', {'draft_path': self._display(self.draft_path),
                'next_offset': self._next_offset(), 'review_count': self.review_count,
                'approval_restored': bool(self.approval)})
        return True

    def _legacy_job(self, pdf, project_evidence, source_paths, force):
        """Adopt pre-checkpoint drafts using their original persisted source list."""
        if force or self._state_pointer(pdf, project_evidence).exists():
            return None
        digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
        candidates = []
        for path in (self.project / '.paper-agent/wiki-curation').glob('*/source.json'):
            if not (path.parent / 'draft.json').is_file() or (path.parent / 'state.json').exists():
                continue
            document = json.loads(path.read_text())
            blocks = [b for b in document.get('blocks', []) if b.get('source_kind') == 'project_artifact']
            if document.get('pdf_sha256') != digest or bool(blocks) != project_evidence:
                continue
            paths = [b['source_path'] for b in blocks]
            if source_paths and not set(source_paths).issubset(paths):
                continue
            if any(not (file := ToolContext(self.project).resolve(b['source_path'])).is_file() or
                   hashlib.sha256(file.read_bytes()).hexdigest() != b['sha256'] for b in blocks):
                continue
            candidates.append((len(blocks), path.stat().st_mtime_ns, path.parent, document, paths))
        if not candidates:
            return None
        _, _, job, document, paths = max(candidates, key=lambda item: item[:2])
        return job, document, paths

    def _restore_review_files(self):
        """Recover completed on-disk review output, including a crash before state save."""
        for path in sorted(self.job.glob('review-*/quality.json'),
                           key=lambda p: int(p.parent.name.split('-')[-1])):
            report = json.loads(path.read_text())
            if (report.get('document_id') != self.document['document_id'] or
                    report.get('schema_version') != QUALITY_VERSION):
                continue
            self.review_count = max(self.review_count, int(path.parent.name.split('-')[-1]))
            self.last_report = report
            if not (path.parent / 'draft.json').is_file():
                continue
            draft = self._load_draft(path.parent / 'draft.json')
            draft_hash = fingerprint(draft)
            if not self.draft_path.is_file() or fingerprint(self._load_draft(self.draft_path)) != draft_hash:
                continue
            self.approval = None
            if report.get('status') == 'needs_revision':
                # This review could only run after the source-reading gate completed.
                self.read_ranges = [(0, len(self.source_text))]
                self.last_review_hash = draft_hash
                self.last_review_result = {'status': 'needs_revision', 'report': report,
                    'report_path': self._display(path)}
            approval_path = path.parent / 'approval.json'
            if report.get('status') in {'passed', 'accepted_with_issues'} and approval_path.is_file():
                approved = json.loads(approval_path.read_text())
                if approved == {'draft_hash': draft_hash, 'document_id': self.document['document_id']}:
                    self.read_ranges = [(0, len(self.source_text))]
                    self.approval = {'draft_hash': draft_hash, 'draft': draft, 'report': report,
                                     'directory': path.parent}
                    self.last_review_hash = draft_hash
                    self.last_review_result = {'status': report['status'], 'publishable': True,
                        'report_path': self._display(path), 'issues': report.get('issues', [])}

    def _next_offset(self):
        end = 0
        for start, stop in sorted(self.read_ranges):
            if start > end:
                break
            end = max(end, stop)
        return end if end < len(self.source_text) else None

    def _prepared_result(self):
        return self._result({'status': 'prepared', 'resumed': True,
            'draft_path': self._display(self.draft_path), 'document_id': self.document['document_id'],
            'source_chars': len(self.source_text), 'next_offset': self._next_offset(),
            'dataset_catalog': self.catalog, 'existing_classification_labels': self.vocabulary,
            'figure_candidates': self.candidates,
            'project_sources': [{k: item[k] for k in ('path', 'sha256')} for item in self.sources],
            'review_count': self.review_count, 'publishable': bool(self.approval) and
                self.draft_path.is_file() and fingerprint(self._load_draft(self.draft_path)) == self.approval['draft_hash'],
            'last_review': getattr(self, 'last_review_result', None)})

    def validate_draft_write(self, path: str) -> None:
        """Limit generic file edits to this assignment's staged JSON drafts."""
        self._require_prepared()
        target = ToolContext(self.project).resolve(path)
        if (
            target.parent != self.job
            or target.suffix != ".json"
            or not target.name.startswith("draft")
        ):
            raise ValueError(
                "Wiki role file writes are limited to draft*.json in this assignment staging directory; publish through wiki_publish"
            )

    def tools(self):
        return tuple(
            ToolSpec(
                name,
                getattr(self, name).__doc__,
                getattr(self, name),
                name == "wiki_read_source",
            )
            for name in (
                "wiki_prepare",
                "wiki_read_source",
                "wiki_review",
                "wiki_publish",
                "wiki_abort",
            )
        )

    @contextmanager
    def _lock(self):
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / ".autoresearch.lock").open("a") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield

    def _result(self, value, error=False):
        return ToolResult(json.dumps(value, ensure_ascii=False), error)

    def wiki_prepare(
        self,
        ctx,
        paper_path: str,
        project_evidence: bool = False,
        source_paths: list[str] | None = None,
        force: bool = False,
    ) -> ToolResult:
        """Prepare a full PDF and explicitly selected project evidence. Returns source-reading cursors, classification context and draft path. Call once after discovering relevant files."""
        if self.prepared:
            self._check_inputs()
            return self._prepared_result()
        if self.receipt:
            raise ValueError(
                "This assignment is already prepared; continue its source/review/publication tools"
            )
        if self.options:
            if ctx.resolve_read(paper_path) != ctx.resolve_read(
                self.options["paper_path"]
            ) or project_evidence != self.options.get("project_evidence", False):
                raise ValueError(
                    "PDF and evidence mode must match the current assignment"
                )
            force = self.options.get("force", False)
            source_paths = list(
                dict.fromkeys(
                    [*(self.options.get("source_paths") or []), *(source_paths or [])]
                )
            )
        if source_paths and not project_evidence:
            raise ValueError("Project sources require a project-evidence assignment")
        pdf = ctx.resolve_read(paper_path)
        if not pdf.is_file() or pdf.suffix.lower() != ".pdf":
            raise ValueError("A local manuscript PDF is required")
        if self._restore(pdf, project_evidence, source_paths, force):
            return self._prepared_result()
        source_paths = list(dict.fromkeys([*(self.options.get('source_paths') or []), *(source_paths or [])])) or None
        legacy = self._legacy_job(pdf, project_evidence, source_paths, force)
        if legacy:
            source_paths = legacy[2]
        self.root = Path(self.options.get("wiki_root") or resolve_wiki_root()).resolve()
        self.pdf = pdf
        self.project_evidence = project_evidence
        self.job = legacy[0] if legacy else self.project / ".paper-agent/wiki-curation" / uuid4().hex
        self.job.mkdir(parents=True, exist_ok=True)
        self.draft_path = self.job / "draft.json"
        key = fingerprint(self._display(pdf))
        self.receipt_path = (
            self.project / ".paper-agent/research-memory.json"
            if project_evidence
            else self.project / ".paper-agent/wiki-curation/receipts" / f"{key}.json"
        )
        self.sources = (
            collect_project_sources(self.project, source_paths)
            if project_evidence
            else []
        )
        self.inputs = {
            self._display(pdf): hashlib.sha256(pdf.read_bytes())
            .hexdigest(),
            **{s["path"]: s["sha256"] for s in self.sources},
        }
        self.signature = fingerprint(
            [
                CURATION_VERSION,
                SOURCE_VERSION,
                QUALITY_VERSION,
                PROMPT_VERSION,
                self.inputs,
            ]
        )
        previous = (
            json.loads(self.receipt_path.read_text())
            if self.receipt_path.is_file()
            else {}
        )
        self.research_id = (
            (previous.get("research_id") or uuid4().hex) if project_evidence else None
        )
        with self._lock():
            if (
                not force
                and previous.get("status") == "completed"
                and previous.get("signature") == self.signature
                and previous.get("wiki_root") == str(self.root)
                and previous.get("outputs")
                and all(Path(p).is_file() for p in previous["outputs"])
                and all(
                    hashlib.sha256(Path(p).read_bytes()).hexdigest() == digest
                    for p, digest in previous.get("output_hashes", {}).items()
                )
            ):
                self.receipt = {
                    **previous,
                    "status": "skipped",
                    "reason": "Wiki inputs and outputs are unchanged",
                }
                return self._result(self.receipt)
            self.vocabulary = classification_vocabulary(self.root)
        # Parsing can be slow; only shared Wiki reads/writes hold the publication lock.
        prepared = prepare_paper_sources(
            self.project,
            pdf,
            self.sources,
            expected_sha256=self.inputs[self._display(pdf)],
            on_progress=self.on_progress,
        )
        self.document, self.figures = prepared.document, prepared.figures
        self.catalog, self.catalog_ids = dataset_catalog_context(self.project)
        self.publisher = WikiPublisher(
            self.project,
            self.root,
            analyze=partial(request_json_analysis, self.model),
            on_progress=self.on_progress,
        )
        self.candidates = figure_candidates(self.figures)
        self.source_text = json.dumps(
            source_context(self.document), ensure_ascii=False, indent=2
        )
        if legacy and legacy[1]['document_id'] != self.document['document_id']:
            # Current parser/source versions changed; preserve the old evidence untouched.
            self.job = self.project / '.paper-agent/wiki-curation' / uuid4().hex
            self.job.mkdir(parents=True)
            self.draft_path = self.job / 'draft.json'
            legacy = None
        write_json(self.job / "source.json", self.document)
        self.provenance = {}
        if project_evidence:
            state = self.project / "workflow-state.json"
            workflow = json.loads(state.read_text()) if state.is_file() else {}
            self.provenance = {
                "kind": "autoresearch",
                "research_id": self.research_id,
                "project": str(self.project),
                "run_id": workflow.get("run_id"),
                "manuscript_status": "unpublished",
                "inputs": self.inputs,
            }
        self.prepared = True
        if legacy:
            self._restore_review_files()
        self._save_receipt("running")
        self._save_state()
        if legacy:
            return self._prepared_result()
        return self._result(
            {
                "status": "prepared",
                "draft_path": self._display(self.draft_path),
                "document_id": self.document["document_id"],
                "source_chars": len(self.source_text),
                "next_offset": 0,
                "dataset_catalog": self.catalog,
                "existing_classification_labels": self.vocabulary,
                "figure_candidates": self.candidates,
                "project_sources": [
                    {k: s[k] for k in ("path", "sha256")} for s in self.sources
                ],
            }
        )

    def wiki_read_source(self, ctx, offset: int = 0, limit: int = 20000) -> ToolResult:
        """Read the prepared source by character cursor, preserving block IDs and quotes. Continue with next_offset until null before submitting a draft for review."""
        self._require_prepared()
        if offset < 0 or offset >= len(self.source_text) or not 1 <= limit <= 40000:
            raise ValueError("Invalid source cursor or limit (1..40000)")
        end = min(offset + limit, len(self.source_text))
        self.read_ranges.append((offset, end))
        self._save_state()
        return self._result(
            {
                "offset": offset,
                "next_offset": end if end < len(self.source_text) else None,
                "total_chars": len(self.source_text),
                "content": self.source_text[offset:end],
            }
        )

    def _require_prepared(self):
        if getattr(self, "aborted", False):
            raise ValueError("This assignment was aborted; start a new Wiki assignment")
        if not self.prepared:
            raise ValueError(
                "Call wiki_prepare before reading, reviewing or publishing"
            )

    def _check_inputs(self):
        for name, digest in self.inputs.items():
            path = ToolContext(self.project).resolve_read(name)
            if (
                not path.is_file()
                or hashlib.sha256(path.read_bytes()).hexdigest() != digest
            ):
                self.approval = None
                raise ValueError(
                    f"Source changed after preparation: {name}; start a new Wiki assignment"
                )

    def _load_draft(self, path):
        target = ToolContext(self.project).resolve(path)
        draft = json.loads(target.read_text())
        if not isinstance(draft, dict):
            raise ValueError("Draft must be a JSON object")
        return draft

    def _validate(self, draft):
        return validate_draft(
            draft,
            self.document,
            self.candidates,
            self.catalog_ids,
            research_id=self.research_id,
        )

    def _review_request(self, system, user):
        from paper_agent.integrations.progress import OperationProgress
        with OperationProgress(self.project, 'review', self.job / 'evidence-review',
                               role='wiki_agent', on_progress=self.on_progress).running() as progress:
            progress.update('reviewing')
            return request_json_analysis(
                self.model,
                system + (PROJECT_SOURCE_PROMPT if self.project_evidence else ''), user)

    def wiki_review(self, ctx, draft_path: str) -> ToolResult:
        """Validate the role-authored JSON draft and request independent evidence review. Returns errors/issues for the role to revise; never generates or revises the draft itself. Approval is bound to this exact draft and source version."""
        self._require_prepared()
        if self.receipt and self.receipt.get("status") in {"completed", "skipped"}:
            raise ValueError("This assignment has already been published")
        self._check_inputs()
        if self._next_offset() is not None:
            raise ValueError(f'Read the complete prepared source first; first unread offset: {self._next_offset()}')
        draft = self._load_draft(draft_path)
        self.draft_path = ToolContext(self.project).resolve(draft_path)
        draft_hash = fingerprint(draft)
        if getattr(self, 'last_review_hash', None) == draft_hash and getattr(self, 'last_review_result', None):
            return self._result(self.last_review_result, not self.last_review_result.get('publishable', False))
        self.approval = None
        self.review_count += 1
        if self.review_count > 3:
            raise ValueError('Independent review budget exhausted (initial review plus two role revisions)')
        self._save_state()
        directory = self.job / f"review-{self.review_count}"
        directory.mkdir(exist_ok=True)
        try:
            self._validate(draft)
            reviewed, report = prepare_analysis(
                draft,
                self.document,
                self._review_request,
                directory,
                lambda action, **details: (
                    self.on_progress(action, details) if self.on_progress else None
                ),
                model_name=getattr(self.model, "model", "injected"),
                validate=self._validate,
                figure_candidates=self.candidates,
                revision_context={
                    "dataset_catalog": self.catalog,
                    "existing_classification_labels": self.vocabulary,
                },
                max_revisions=0,
                previous_report=getattr(self, "last_report", None),
                allow_coverage_gaps=self.review_count == 3,
            )
        except RuntimeError:
            # A transport/format failure did not produce an evidence assessment.
            # Keep the draft and allow retry without spending a revision round.
            self.review_count -= 1
            self._save_state()
            raise
        except ValueError as exc:
            path = directory / "quality.json"
            report = (
                json.loads(path.read_text())
                if path.is_file()
                else {"status": "needs_revision", "errors": [str(exc)]}
            )
            if path.is_file():
                self.last_report = report
            self.last_review_hash = draft_hash
            self.last_review_result = {'status': 'needs_revision', 'error': str(exc),
                'report': report, 'report_path': self._display(path)}
            self._save_state()
            return self._result(self.last_review_result, True)
        self.last_report = report
        self._check_inputs()
        if fingerprint(self._load_draft(draft_path)) != draft_hash:
            raise ValueError(
                "Draft changed during independent review; call wiki_review again"
            )
        self.approval = {
            "draft_hash": draft_hash,
            "draft": deepcopy(reviewed),
            "report": deepcopy(report),
            "directory": directory,
        }
        write_json(
            directory / "approval.json",
            {
                "draft_hash": self.approval["draft_hash"],
                "document_id": self.document["document_id"],
            },
        )
        self.last_review_hash = draft_hash
        self.last_review_result = {'status': report['status'], 'publishable': True,
            'issues': report.get('issues', []), 'report_path': self._display(directory / 'quality.json')}
        self._save_state()
        return self._result(self.last_review_result)

    def wiki_publish(self, ctx, draft_path: str) -> ToolResult:
        """Publish the exact independently reviewed draft to the shared Wiki. Rejects missing/stale approval or changed sources; preserves node notes, citations, figures, relationships and update identity."""
        self._require_prepared()
        if self.receipt and self.receipt.get("status") in {"completed", "skipped"}:
            return self._result(self.receipt)
        self._check_inputs()
        if (
            not self.approval
            or fingerprint(self._load_draft(draft_path)) != self.approval["draft_hash"]
        ):
            raise ValueError(
                "Publish requires independent approval of this exact draft; call wiki_review again"
            )
        draft = deepcopy(self.approval["draft"])
        validated = self._validate(draft)
        with self._lock():
            # Verify under the shared publication lock as well.
            self._check_inputs()
            try:
                state = self.publisher.publish(
                    self._display(self.pdf),
                    self.document,
                    self.figures,
                    draft,
                    self.approval["report"],
                    validated,
                    self.approval["directory"],
                    provenance=self.provenance,
                    research_id=self.research_id,
                    project_sources=self.sources,
                    complete_relations=self.options.get("complete_relations", True),
                )
                if self.research_id:
                    retire_experiments(
                        self.root,
                        self.research_id,
                        {(self.project / p).resolve() for p in state.experiment_paths},
                    )
                outputs = [
                    state.wiki_path,
                    state.idea_path,
                    *state.experiment_paths,
                    state.evidence_path,
                    state.quality_path,
                    *state.figure_paths,
                ]
                paper = (self.project / state.wiki_path).resolve()
                outputs += [
                    str(metadata_path(paper, kind))
                    for kind in ("source", "references", "figures")
                ]
                paths = [str((self.project / p).resolve()) for p in outputs]
                self._save_receipt(
                    "completed",
                    **asdict(state),
                    outputs=paths,
                    output_hashes={
                        p: hashlib.sha256(Path(p).read_bytes()).hexdigest()
                        for p in paths
                    },
                )
            except Exception as exc:
                self._save_receipt("failed", error=str(exc))
                raise
        return self._result(self.receipt)

    def wiki_abort(self, ctx, reason: str) -> ToolResult:
        """Close an unsuccessful assignment, including preparation failures, with a concrete reason and retained draft paths. Does not roll back published content or rerun research."""
        if not reason.strip():
            raise ValueError("A concrete failure reason is required")
        if self.receipt and self.receipt.get("status") in {"completed", "skipped"}:
            raise ValueError("Published work cannot be aborted")
        if not hasattr(self, "signature"):
            # Preparation may fail before a PDF/source fingerprint is available.
            self.root = Path(
                self.options.get("wiki_root") or resolve_wiki_root()
            ).resolve()
            self.job = getattr(
                self, "job", self.project / ".paper-agent/wiki-curation" / uuid4().hex
            )
            self.draft_path = self.job / "draft.json"
            self.receipt_path = self.job / "receipt.json"
            self.inputs = {}
            self.signature = None
            self.research_id = None
            self.project_evidence = self.options.get("project_evidence", False)
        self.aborted = True
        self.approval = None
        self._save_receipt(
            "failed", error=reason, draft_path=self._display(self.draft_path)
        )
        return self._result(self.receipt)

    def _save_receipt(self, status, **values):
        self.receipt = {
            "status": status,
            "role": "wiki_agent",
            "research_id": self.research_id,
            "wiki_root": str(self.root),
            "signature": self.signature,
            "inputs": self.inputs,
            "project_evidence": self.project_evidence,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            **values,
        }
        for key in ("wiki_path", "idea_path", "evidence_path", "quality_path"):
            if self.receipt.get(key):
                self.receipt[key] = str((self.project / self.receipt[key]).resolve())
        if "experiment_paths" in self.receipt:
            self.receipt["experiment_paths"] = [
                str((self.project / p).resolve())
                for p in self.receipt["experiment_paths"]
            ]
        write_json(self.receipt_path, self.receipt)

    def _display(self, path):
        return ToolContext(self.project).display_path(path)

    def check_completion(self):
        if getattr(self, "aborted", False) and self.receipt.get("status") == "failed":
            return
        if not self.receipt or self.receipt.get("status") not in {
            "completed",
            "skipped",
        }:
            raise ValueError(
                "Wiki task needs a successful wiki_publish or verified unchanged-input receipt"
            )


# Source preparation

SOURCE_VERSION = "wiki-sources-v1"
MAX_SOURCE_BYTES = 256000
REPORTS = (
    "refine-logs/FINAL_PROPOSAL.md",
    "refine-logs/EXPERIMENT_PLAN.md",
    "refine-logs/EXPERIMENT_RESULTS.md",
    "data-search/DATA_REPORT.md",
)
TEXT_SUFFIXES = {".json", ".csv", ".md", ".txt", ".yaml", ".yml", ".py", ".tex", ".log"}


@dataclass(frozen=True)
class PaperSources:
    document: dict
    figures: dict


def collect_project_sources(
    project: Path, extra_paths: list[str] | None = None
) -> list[dict]:
    """Collect existing reports, explicitly referenced results and selected text files."""
    project = project.resolve()
    ctx = ToolContext(project)
    paths = {name for name in REPORTS if ctx.resolve(name).is_file()}
    for name in tuple(paths):
        text = ctx.resolve(name).read_text(encoding="utf-8")
        paths.update(re.findall(r"\bresults/[\w./-]+\.(?:json|csv|md|txt)\b", text))
    # Missing automatically discovered results are skipped. Explicit selections
    # are required to exist, so a misspelled evidence path cannot disappear silently.
    selected = set()
    for name in extra_paths or []:
        path = ctx.resolve(name)
        if path.suffix.lower() not in TEXT_SUFFIXES:
            raise ValueError(f"Unsupported project evidence: {name}")
        if not path.is_file():
            raise FileNotFoundError(f"Project evidence not found: {name}")
        selected.add(path.relative_to(project).as_posix())
    paths.update(selected)
    sources = []
    for path in sorted({ctx.resolve(name) for name in paths}):
        if not path.is_file():
            continue
        raw = path.read_bytes()
        sources.append(
            {
                "path": path.relative_to(project).as_posix(),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "content": (
                    raw.decode("utf-8")
                    if len(raw) <= MAX_SOURCE_BYTES
                    else f"Large source retained at {path}; contents not included in analysis."
                ),
            }
        )
    return sources


def prepare_paper_sources(
    project: Path, pdf: Path, sources: list[dict], *, expected_sha256: str, on_progress=None
) -> PaperSources:
    """Parse the PDF and append project evidence without altering the parser cache."""
    figures = read_mineru_paper(project, pdf, on_progress=on_progress, role='wiki_agent')
    document = deepcopy(figures.get("document"))
    if not document or document.get("pdf_sha256") != expected_sha256:
        raise ValueError(
            "Structured PDF source is missing or does not match the manuscript"
        )
    for index, source in enumerate(sources):
        document["blocks"].append(
            {
                "id": f"project-{index}",
                "page": None,
                "type": "text",
                "text": source["content"],
                "source_path": source["path"],
                "source_kind": "project_artifact",
                "sha256": source["sha256"],
                "bbox": None,
                "parent_id": None,
                "section_id": None,
                "excluded_reason": None,
            }
        )
    if sources:
        document["document_id"] = hashlib.sha256(
            json.dumps(
                [document["document_id"], sources],
                sort_keys=True,
                ensure_ascii=False,
            ).encode()
        ).hexdigest()
    return PaperSources(document, figures)


def dataset_catalog_context(project: Path) -> tuple[dict, set[str]]:
    """Offer registered dataset identities and source notes as synthesis context."""
    catalog = project / "电池资源库/电芯/数据集"
    if not catalog.is_dir():
        catalog = Path(__file__).resolve().parents[2] / "电池资源库/电芯/数据集"
    index = catalog.parent / "共享指南/数据集目录.md"
    if not index.is_file():
        index = catalog / "数据集目录.md"
    readmes = sorted(catalog.glob("*_README.md"))
    ids = {path.name.removesuffix("_README.md") for path in readmes}
    notes = {}
    for path in readmes:
        content = path.read_text(encoding="utf-8")
        if "## BatteryLife 数据集检索\n" in content:
            notes[path.name.removesuffix("_README.md")] = content.split(
                "## BatteryLife 数据集检索\n", 1
            )[1].strip()
    return {
        "path": str(catalog),
        "index": index.read_text(encoding="utf-8") if index.is_file() else "",
        "catalog_ids": sorted(ids),
        "source_notes": notes,
    }, ids


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
            *research_wiki.CLASSIFICATION_FIELDS,
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
    paper_classification = research_wiki.validate_classification(metadata)
    terms, references = validate_extraction(draft, pages, {}, check_prose=False)
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
    extracted = validate_extracted(draft, document["pages"], research_id=research_id)
    validate_figure_selection(draft.get("figure_selection"), candidates)
    draft["figure_selection"] = limit_to_one_figure(
        {
            "candidates": candidates,
            "selection": draft["figure_selection"],
        }
    )["selection"]
    names = validate_datasets(draft, catalog_ids)
    classifications = [
        research_wiki.validate_classification(node)
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
                research_wiki.init_wiki(str(self.wiki_root), separate_origins=True)
            root = corpus_root(self.wiki_root, "autoresearch" if research_id else "literature")
            if not (root / "papers").is_dir():
                self._progress("initialize_wiki", path=str(root))
                research_wiki.init_wiki(str(root))
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
                candidate = root / "papers" / (research_wiki.slugify(
                    title, research_wiki._last_name(authors[0]) if authors else "", year) + ".md")
                arxiv_match = research_wiki._find_existing_page_by_arxiv(
                    root, metadata.get("arxiv_id") or "") if metadata.get("arxiv_id") else None
                for existing in (candidate, arxiv_match):
                    if existing is not None and existing.is_file():
                        assert_same_identity(existing, metadata)
                        path = existing
            if path is not None and path.is_file() and not research_id:
                assert_same_identity(path, metadata)
            if path is None or not path.is_file():
                path = research_wiki.ingest_paper(
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
            research_wiki.update_paper_sections(
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
                research_wiki.update_idea_sections(
                    str(root),
                    idea_slug,
                    {
                        "Thesis": idea["thesis"],
                        "strcture": idea["structure"],
                    },
                    refresh_indexes=False,
                )
            else:
                research_wiki.upsert_idea(
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
                exp_path = research_wiki.add_experiment(
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
                research_wiki.update_node_classification(node_path, classification)
            self._progress("write_citations", references=len(references))
            save_references(path, terms, references)
            citation_counts = refresh_citations(root)
            state.citation_count = citation_counts.get(f"paper:{state.slug}", 0)
            self._progress("rebuild_wiki_indexes")
            research_wiki.rebuild_index(str(root))
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
        research_wiki.rebuild_index(str(root))
