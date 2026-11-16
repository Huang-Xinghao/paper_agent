"""Model-selected method figures, with one visual request for ambiguous candidates."""

from __future__ import annotations

from paper_agent.tools.wiki_metadata import metadata_path, metadata_records

import json
import re
from pathlib import Path

from paper_agent.tools import ToolContext
from paper_agent.tools.image_viewing import view_image
from paper_agent.tools.paper_figures import save_architecture_figures
from paper_agent.tools.wiki_prompts import FIGURE_REVIEW_PROMPT
from paper_agent.storage import write_json


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
