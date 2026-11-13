"""Publish completed project research through the existing WikiAgent."""
from __future__ import annotations

from paper_agent.tools.wiki_metadata import metadata_path, metadata_records

import fcntl
import hashlib
import json
from pathlib import Path
from uuid import uuid4

from paper_agent.tools import ToolContext
from paper_agent.tools.wiki import resolve_wiki_root
from paper_agent.agent.runtime.sessions import now
from paper_agent.agent.wiki.agent import WikiAgent
from paper_agent.tools.wiki_prompts import PROMPT_VERSION
from paper_agent.storage import write_json
from paper_agent.tools.wiki_ingest import collect_project_sources as _sources, retire_experiments as _retire_experiments


VERSION = "research-memory-v1"


def archive_research(project, *, paper_path="paper/main.pdf", wiki_root=None,
                     model=None, timeout=600, on_progress=None, force=False,
                     complete_relations=True):
    """Create/update one shared Wiki research entry; unchanged inputs need no model call."""
    project = Path(project).resolve()
    pdf = ToolContext(project).resolve(paper_path)
    if not pdf.is_file() or pdf.suffix.lower() != ".pdf":
        raise ValueError(f"Final manuscript PDF not found: {paper_path}")
    root = Path(wiki_root).expanduser().resolve() if wiki_root else resolve_wiki_root()
    receipt_path = project / ".paper-agent/research-memory.json"
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    root.mkdir(parents=True, exist_ok=True)
    # Different projects publish into the same graph and index.
    with (root / ".autoresearch.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        previous = json.loads(receipt_path.read_text()) if receipt_path.exists() else {}
        research_id = previous.get("research_id") or uuid4().hex
        sources = _sources(project)
        inputs = {str(pdf.relative_to(project)): hashlib.sha256(pdf.read_bytes()).hexdigest(),
                  **{s["path"]: s["sha256"] for s in sources}}
        signature = hashlib.sha256(json.dumps([VERSION, PROMPT_VERSION, inputs], sort_keys=True).encode()).hexdigest()
        published = root / "papers" / f"autoresearch-{research_id}.md"
        if (not force and previous.get("status") == "completed" and previous.get("signature") == signature
                and previous.get("wiki_root") == str(root)
                and all(Path(p).is_file() for p in previous.get("outputs", []))
                and published.is_file()):
            return {**previous, "status": "skipped", "reason": "Research inputs are unchanged"}
        saved_state = project / "workflow-state.json"
        workflow = json.loads(saved_state.read_text()) if saved_state.is_file() else {}
        provenance = {"kind": "autoresearch", "research_id": research_id,
                      "project": str(project), "run_id": workflow.get("run_id"),
                      "manuscript_status": "unpublished", "inputs": inputs}
        record = {"research_id": research_id, "role": "wiki_agent", "wiki_root": str(root),
                  "status": "running", "signature": signature, "started_at": now(),
                  "inputs": inputs, "wiki_path": str(published)}
        write_json(receipt_path, record)
        try:
            agent = WikiAgent(project, wiki_root=root, model=model, timeout=timeout,
                              on_progress=on_progress)
            state = agent.go(
                "Archive the completed autoresearch manuscript and its project evidence in the Wiki. "
                "Preserve important negative results and applicability limits using the existing schema.",
                paper_path=pdf, provenance=provenance, project_sources=sources,
                research_id=research_id, complete_relations=complete_relations)
            current = {(project / path).resolve() for path in state.experiment_paths}
            _retire_experiments(root, research_id, current)
            outputs = [(project / path).resolve() for path in
                       [state.wiki_path, state.idea_path, *state.experiment_paths,
                        state.evidence_path, state.quality_path, *state.figure_paths]]
            outputs.extend(metadata_path(published, kind) for kind in ('source', 'references', 'figures'))
            record.update(status="completed", ended_at=now(), summary=state.final,
                          wiki_path=str((project / state.wiki_path).resolve()),
                          idea_path=str((project / state.idea_path).resolve()),
                          experiment_paths=[str((project / p).resolve()) for p in state.experiment_paths],
                          outputs=[str(p) for p in outputs],
                          relationship_status=state.relationship_status,
                          relationship_error=state.relationship_error,
                          figure_selection_path=state.figure_selection_path,
                          figure_warnings=state.figure_warnings,
                          relationship_warnings=state.relationship_warnings)
            write_json(receipt_path, record)
            return record
        except Exception as exc:
            write_json(receipt_path, {**record, "status": "failed", "error": str(exc), "ended_at": now()})
            raise
