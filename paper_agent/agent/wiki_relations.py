"""Evidence-grounded relationships across papers, separate from PDF extraction."""

from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path
from typing import Any, Callable

from paper_agent.tools import research_wiki
from .wiki_prompts import RELATION_SELECTION_PROMPT, RELATION_PROMPT, MAX_RELATION_CANDIDATES


PAPER_RELATIONS = {"extends", "contradicts", "supersedes", "shares_method",
                   "addresses_same_problem"}
IDEA_RELATIONS = {"inspired_by"}
ORIGIN = "wiki-agent-relations-v1"
MAX_CANDIDATES = MAX_RELATION_CANDIDATES


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
    labels: dict[str, set[str]] = {key: set() for key in research_wiki.CLASSIFICATION_FIELDS}
    for directory in ("papers", "ideas", "experiments"):
        for path in sorted((root / directory).glob("*.md")):
            summary = _summary(path)
            for key in labels:
                values = summary.get(key, [])
                if isinstance(values, list):
                    labels[key].update(v for v in values if isinstance(v, str))
    return {key: sorted(values) for key, values in labels.items()}


def discover_relations(root: Path, paper_path: Path, idea_path: Path,
                       analyze: Callable[[str, str], dict[str, Any]]) -> list[dict[str, str]]:
    """Read candidate notes, then validate endpoint types and verbatim evidence."""
    papers = {f"paper:{p.stem}": p for p in sorted((root / "papers").glob("*.md"))
              if p != paper_path}
    if not papers:
        return []
    current_id, idea_id = f"paper:{paper_path.stem}", f"idea:{idea_path.stem}"
    current = {current_id: paper_path.read_text(encoding="utf-8"),
               idea_id: idea_path.read_text(encoding="utf-8")}
    if len(papers) > MAX_CANDIDATES:
        selected = analyze(
            RELATION_SELECTION_PROMPT,
            json.dumps({"current": current,
                        "existing": [_summary(p) for p in papers.values()]}, ensure_ascii=False),
        ).get("candidates")
        if (not isinstance(selected, list) or len(selected) > MAX_CANDIDATES
                or any(not isinstance(n, str) or n not in papers for n in selected)):
            raise ValueError("Invalid relationship candidate IDs")
        papers = {node: papers[node] for node in dict.fromkeys(selected)}
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
    documents = {**current, **{node: path.read_text(encoding="utf-8")
                              for node, path in {**papers, **existing_ideas}.items()}}
    response = analyze(
        RELATION_PROMPT,
        json.dumps({"current_paper": current_id, "current_idea": idea_id,
                    "documents": documents}, ensure_ascii=False),
    )
    relations = response.get("relationships")
    if not isinstance(relations, list):
        raise ValueError("Relationship analysis must contain a relationships array")
    validated, seen = [], set()
    paths = {current_id: paper_path, idea_id: idea_path, **papers, **existing_ideas}
    for edge in relations:
        keys = ("from", "to", "type", "source_quote", "target_quote", "reason")
        if not isinstance(edge, dict) or any(
                not isinstance(edge.get(k), str) or not edge[k].strip() for k in keys):
            raise ValueError("Relationship missing endpoint, type, evidence quote or reason")
        source, target, kind = (edge[k] for k in ("from", "to", "type"))
        paper_pair = (source in papers and target == current_id
                      or source == current_id and target in papers)
        idea_pair = (source == idea_id and target in papers
                     or source in existing_ideas and target == current_id)
        if not (paper_pair and kind in PAPER_RELATIONS or idea_pair and kind in IDEA_RELATIONS):
            raise ValueError("Unsupported relationship type or endpoint pair")
        for node, key in ((source, "source_quote"), (target, "target_quote")):
            body = documents[node].split("\n---\n", 1)[-1]
            if edge[key] not in body:
                raise ValueError(f"Relationship {key} is not a verbatim body excerpt of {node}")
        identity = source, target, kind
        # Symmetric relationships get a canonical direction for stable deduplication.
        if kind in {"shares_method", "addresses_same_problem", "contradicts"}:
            identity = *sorted((source, target)), kind
        if identity in seen:
            continue
        seen.add(identity)
        evidence = (f"{paths[source].relative_to(root)}: {edge['source_quote']}\n"
                    f"{paths[target].relative_to(root)}: {edge['target_quote']}\n"
                    f"{edge['reason']}")
        if research_wiki.scan_for_threats(evidence, scope="strict"):
            raise ValueError("Relationship evidence failed the wiki content scan")
        validated.append({"from": identity[0], "to": identity[1], "type": kind,
                          "evidence": evidence})
    return validated


def write_relations(root: Path, owner: str, relations: list[dict[str, str]],
                    *, origin: str = ORIGIN) -> None:
    """Atomically refresh this paper's inferred edges, preserving other/manual edges."""
    write_relations_batch(root, {owner: relations}, origin=origin)


def write_relations_batch(root: Path, relations_by_owner: dict[str, list[dict[str, str]]],
                          *, origin: str = ORIGIN) -> None:
    """Refresh a set of owners in one staged graph update."""
    if not relations_by_owner:
        return
    edge_path = root / "graph" / "edges.jsonl"
    existing = [json.loads(line) for line in edge_path.read_text(encoding="utf-8").splitlines()
                if line.strip()]
    kept = [e for e in existing
            if not (e.get("origin") == origin and e.get("owner") in relations_by_owner)]
    # Stage through the canonical writer to retain deduplication and evidence hygiene.
    with tempfile.TemporaryDirectory(prefix=".relations-", dir=edge_path.parent) as directory:
        staged_root = Path(directory)
        staged = staged_root / "graph" / "edges.jsonl"
        staged.parent.mkdir()
        staged.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in kept),
                          encoding="utf-8")
        research_wiki.add_edges(str(staged_root), [
            {**edge, "origin": origin, "owner": owner}
            for owner, relations in relations_by_owner.items() for edge in relations
        ])
        quarantine_log = staged.parent / "quarantine.log"
        if quarantine_log.exists():
            with (edge_path.parent / "quarantine.log").open("a", encoding="utf-8") as stream:
                stream.write(quarantine_log.read_text(encoding="utf-8"))
        staged.replace(edge_path)
    for owner, relations in relations_by_owner.items():
        research_wiki.append_log(str(root), f"relationships: {owner} inferred={len(relations)}")
