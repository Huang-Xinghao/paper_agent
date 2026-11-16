"""Resumable corpus adapter using the current Wiki tools and independently reviewed repairs.

Kept separate from the interactive Wiki role: batch jobs already carry exact PDF assignments.
"""
from copy import deepcopy
from functools import partial
from pathlib import Path
import hashlib
import json

from paper_agent.integrations.model import ModelClient, load_env_file
from paper_agent.storage import write_json
from paper_agent.tools import ToolContext
from paper_agent.tools.paper_figures import read_mineru_paper
from paper_agent.tools.paper_preprocessing import source_context
from paper_agent.tools.wiki_assets import figure_candidates
from paper_agent.tools.wiki_ingest import (
    WikiPublisher, dataset_catalog_context, validate_draft,
)
from paper_agent.tools.wiki_prompts import GENERATION_PROMPT
from paper_agent.tools.wiki_recovery import (
    EDIT_PROMPT, apply_edits, repair_bibliography, supplement_native_frontmatter,
)
from paper_agent.tools.wiki_relations import classification_vocabulary
from paper_agent.tools.wiki_review import abstract_provenance, prepare_analysis, request_json_analysis


class BatchWikiAgent:
    """Full source -> saved draft -> bounded repair/review -> serialized publication."""
    def __init__(self, project, *, wiki_root, timeout=600, on_progress=None, model_client=None):
        self.project = Path(project).resolve()
        self.root = Path(wiki_root).resolve()
        self.on_progress = on_progress
        if model_client is None:
            load_env_file(self.project / '.env')
        self.model = model_client or ModelClient(timeout=timeout)
        self.analyze = partial(request_json_analysis, self.model)
        self.publisher = WikiPublisher(self.project, self.root, analyze=self.analyze,
                                       on_progress=on_progress)

    def _progress(self, action, **details):
        if self.on_progress:
            self.on_progress(action, details)

    def _read_full_paper(self, ctx, paper_path):
        return read_mineru_paper(self.project, ctx.resolve_read(paper_path))

    def _edit_draft(self, draft, payload, recovery, round_number):
        for attempt in range(2):
            response = self.analyze(EDIT_PROMPT, json.dumps(payload, ensure_ascii=False))
            suffix = '' if attempt == 0 else '-retry'
            write_json(recovery / f'edit-request-{round_number}{suffix}.json', response)
            try:
                return apply_edits(draft, response)
            except (ValueError, KeyError, IndexError, TypeError) as exc:
                if attempt:
                    raise ValueError(f'Invalid draft edits after correction: {exc}') from exc
                payload = {**payload, 'rejected_edits': response,
                           'edit_application_error': str(exc),
                           'edit_guidance': 'Return corrected edits into the unchanged supplied draft. '
                                            'Array indices are zero-based and must exist. Replace an '
                                            'allowed entire array to append an item; do not use index len(array).'}

    def _saved_draft(self, directory, document):
        source_path = directory / 'source.json'
        if not source_path.is_file():
            return None, {}
        old_source = json.loads(source_path.read_text())
        if (old_source.get('document_id') != document['document_id']
                or old_source.get('pdf_sha256') != document['pdf_sha256']):
            return None, {}
        # Latest review/draft records carry accumulated scientific corrections.
        for name in ['batch-recovery-draft.json', 'draft.json', 'quality.json', 'generation.json', 'extraction.json']:
            path = directory / name
            if not path.is_file():
                continue
            record = json.loads(path.read_text())
            if name.endswith('-draft.json') or name == 'draft.json':
                draft = record
            else:
                draft = record.get('synthesis')
                if draft is None and record.get('attempts'):
                    draft = record['attempts'][-1].get('draft') or record['attempts'][-1].get('synthesis')
            if isinstance(draft, dict) and isinstance(draft.get('sections'), dict) and isinstance(draft.get('metadata'), dict):
                quality = directory / 'quality.json'
                return deepcopy(draft), json.loads(quality.read_text()) if quality.is_file() else {}
        return None, {}

    def go(self, prompt, *, paper_path, complete_relations=False, reuse_analysis=True,
           recovery_report=None, publication_lock=None):
        ctx = ToolContext(self.project)
        self._progress('read_pdf', paper_path=paper_path)
        figures = self._read_full_paper(ctx, paper_path)
        document = figures['document']
        actual_sha = hashlib.sha256(ctx.resolve_read(paper_path).read_bytes()).hexdigest()
        if document.get('pdf_sha256') != actual_sha:
            raise ValueError('Parsed source does not match the assigned PDF')
        self._progress('pdf_read', page_count=len(document['pages']))
        candidates = figure_candidates(figures)
        directory = self.project / '.paper-agent/wiki-preprocessing' / document['document_id']
        draft, previous = self._saved_draft(directory, document) if reuse_analysis else (None, {})
        if draft and abstract_provenance(draft['sections'].get('Abstract', ''), document) is None:
            document = supplement_native_frontmatter(document, ctx.resolve_read(paper_path))
            figures = {**figures, 'document': document}
        catalog, catalog_ids = dataset_catalog_context(self.project)
        # Read shared nodes only while no other thread is mutating them.
        from contextlib import nullcontext
        with publication_lock if publication_lock is not None else nullcontext():
            vocabulary = classification_vocabulary(self.root)
        if draft is None:
            self._progress('analyze_paper', page_count=len(document['pages']))
            draft = self.analyze(GENERATION_PROMPT, json.dumps({
                'task': prompt, 'source_document': source_context(document),
                'dataset_catalog': catalog, 'existing_classification_labels': vocabulary,
                'figure_candidates': candidates,
            }, ensure_ascii=False))
            write_json(directory / 'generation.json', {'synthesis': draft, 'origin': 'current-batch-tools'})
        else:
            self._progress('resume_draft', checkpoint=str(directory))
        write_json(directory / 'source.json', document)
        recovery = directory / 'batch-recovery'
        if not (recovery / 'original-draft.json').exists():
            write_json(recovery / 'original-draft.json', draft)
        if previous and not (recovery / 'original-quality.json').exists():
            write_json(recovery / 'original-quality.json', previous)
        last_error = ''
        previous_report = None
        reviews = 0
        # Local validation failures do not consume an independent review call.
        for round_number in range(5):
            draft, repairs = repair_bibliography(draft, document)
            if repairs:
                write_json(recovery / f'automatic-repairs-{round_number}.json', repairs)
            write_json(directory / 'batch-recovery-draft.json', draft)
            try:
                self._progress('validate_extraction')
                validate_draft(draft, document, candidates, catalog_ids)
                # Validation may normalize multiple legacy keep decisions before reviewing.
                write_json(directory / 'batch-recovery-draft.json', draft)
                if reviews >= 3:
                    raise ValueError('Three independent reviews exhausted; unresolved factual issues remain')
                reviews += 1
                review_dir = recovery / f'attempt-{len(list(recovery.glob("attempt-*"))) + 1}'
                self._progress('verify_evidence', attempt=reviews)
                reviewed, report = prepare_analysis(
                    draft, document, self.analyze, review_dir,
                    lambda action, **details: self._progress(action, **details),
                    model_name=self.model.model,
                    validate=lambda value: validate_draft(value, document, candidates, catalog_ids),
                    revision_context={'dataset_catalog': catalog, 'existing_classification_labels': vocabulary},
                    figure_candidates=candidates, max_revisions=0,
                    previous_report=previous_report, allow_coverage_gaps=reviews == 3,
                )
                draft = reviewed
                write_json(directory / 'batch-recovery-draft.json', draft)
                self._progress('wait_publication')
                with publication_lock if publication_lock is not None else nullcontext():
                    self.root.mkdir(parents=True, exist_ok=True)
                    # Coordinate with interactive role writers, not only batch threads.
                    import fcntl
                    with (self.root / '.autoresearch.lock').open('a') as stream:
                        fcntl.flock(stream, fcntl.LOCK_EX)
                        validated = validate_draft(draft, document, candidates, catalog_ids)
                        state = self.publisher.publish(paper_path, document, figures, draft, report,
                            validated, review_dir, complete_relations=complete_relations)
                write_json(recovery / 'published.json', {'wiki_path': state.wiki_path,
                    'quality_path': state.quality_path, 'status': report['status']})
                return state
            except ValueError as exc:
                last_error = str(exc)
                if 'review_dir' in locals() and (review_dir / 'quality.json').is_file():
                    previous_report = json.loads((review_dir / 'quality.json').read_text())
                if round_number == 4 or reviews >= 3:
                    break
                self._progress('revise_evidence', error=last_error)
                draft = self._edit_draft(draft, {
                    'content_contract': GENERATION_PROMPT, 'draft': draft,
                    'validation_errors': [last_error], 'previous_review': previous_report,
                    'source_document': source_context(document), 'figure_candidates': candidates,
                    'dataset_catalog': catalog,
                }, recovery, round_number)
                write_json(directory / 'batch-recovery-draft.json', draft)
        raise ValueError(f'Wiki recovery incomplete: {last_error}; draft retained at {directory / "batch-recovery-draft.json"}')

    def refresh_relationships(self, paper_path):
        return self.publisher.refresh_relationships(paper_path)
