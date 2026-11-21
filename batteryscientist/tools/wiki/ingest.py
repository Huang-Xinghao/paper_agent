"""Wiki ingestion tools: staged sources, reviewed drafts and verified publication."""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import fcntl
from functools import partial
import hashlib
import json
import logging
from pathlib import Path
import re
from uuid import uuid4

from batteryscientist.storage import write_json

from ..core import ToolContext, ToolResult, ToolSpec
from ..paper_figures import read_mineru_paper
from ..paper_preprocessing import source_context
from .prompts import PROJECT_SOURCE_PROMPT, PROMPT_VERSION
from .publish import (
    WikiPublisher,
    figure_candidates,
    retire_experiments,
    validate_draft,
)
from .relations import classification_vocabulary
from .review import QUALITY_VERSION, prepare_analysis, request_json_analysis
from .search import resolve_wiki_root
from .storage import metadata_path


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
        return self.project / '.batteryscientist/wiki-curation/active' / f'{key}.json'

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
        for path in (self.project / '.batteryscientist/wiki-curation').glob('*/source.json'):
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
                parameter_limits=({"offset": {"minimum": 0},
                                   "limit": {"minimum": 1, "maximum": 40000}}
                                  if name == "wiki_read_source" else {}),
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
        self.job = legacy[0] if legacy else self.project / ".batteryscientist/wiki-curation" / uuid4().hex
        self.job.mkdir(parents=True, exist_ok=True)
        self.draft_path = self.job / "draft.json"
        key = fingerprint(self._display(pdf))
        self.receipt_path = (
            self.project / ".batteryscientist/research-memory.json"
            if project_evidence
            else self.project / ".batteryscientist/wiki-curation/receipts" / f"{key}.json"
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
            self.job = self.project / '.batteryscientist/wiki-curation' / uuid4().hex
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
        """Read prepared source by character cursor (limit 1..40000), preserving block IDs and quotes. Continue with next_offset until null before submitting a draft for review."""
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
        from batteryscientist.integrations.progress import OperationProgress
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
                self, "job", self.project / ".batteryscientist/wiki-curation" / uuid4().hex
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
        catalog = Path(__file__).resolve().parents[3] / "电池资源库/电芯/数据集"
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
