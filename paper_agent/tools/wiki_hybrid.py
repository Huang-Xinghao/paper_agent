"""Bounded hybrid section retrieval and navigation over recorded Wiki relations.

The embedding worker is opt-in and never downloads models. Wiki remains authoritative;
vectors are disposable, content-addressed derived data outside the knowledge library.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import tempfile

from .core import ToolContext, ToolResult
from .wiki import _quality_records, _source_record, _wiki_root
from .wiki_retrieval import (
    ENTITY_TYPES,
    Page,
    Section,
    load_pages,
    location,
    paper_owners,
    rank_pages,
)

RELATIONS = {
    "cites",
    "addresses_same_problem",
    "shares_method",
    "may_apply_to",
    "inspired_by",
    "tested_by",
    "contradicts",
}
NODE_TYPES = {
    "papers": "paper",
    "ideas": "idea",
    "experiments": "exp",
    "claims": "claim",
}


def _json(value):
    return json.dumps(value, ensure_ascii=False, indent=2)


@dataclass
class Chunk:
    page: Page
    section: Section
    offset: int
    text: str
    key: str


def section_chunks(pages: list[Page], root: Path) -> list[Chunk]:
    result = []
    for page in pages:
        for section in page.sections:
            raw = "\n".join(page.lines[section.start : section.end])
            if section.heading == "Connections" and "Edges are recorded" in raw:
                continue
            # 窗口仅服务向量/词法召回，保留所属栏目及精确偏移供继续读上下文。
            # 小窗口控制多语言E5的512-token限制；截断在worker结果中另行检测。
            for start in range(0, len(raw), 340):
                text = raw[start : start + 400]
                if not text.strip() or text.strip().startswith(("_TODO", "<!--")):
                    continue
                key = hashlib.sha256(
                    f"{page.path.relative_to(root).as_posix()}:{section.heading}:{start}:{page.fingerprint}".encode()
                ).hexdigest()
                result.append(Chunk(page, section, start, text, key))
                if start + 400 >= len(raw):
                    break
    return result


def _embedding_identity(model_dir: Path):
    identity = json.loads((model_dir / "identity.json").read_text(encoding="utf-8"))
    # 内容而非仅目录名进入缓存身份，替换模型文件后不能复用旧坐标。
    for name in ("model.onnx", "tokenizer.json"):
        digest = hashlib.sha256()
        with (model_dir / name).open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        identity[name + "_sha256"] = digest.hexdigest()
    return identity


def encode_texts(
    texts: list[str], cache_dir: Path, identity: dict, python: str, model_dir: Path
):
    cache_dir.mkdir(parents=True, exist_ok=True)
    keys = [
        hashlib.sha256(_json({"identity": identity, "text": t}).encode()).hexdigest()
        for t in texts
    ]
    values = {}
    missing = {}
    for key, text in zip(keys, texts):
        path = cache_dir / (key + ".json")
        if path.is_file():
            try:
                values[key] = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                missing[key] = text
        else:
            missing[key] = text
    if missing:
        # 临时文件和缓存均在配置的E盘路径；不放进系统C盘临时目录。
        with tempfile.TemporaryDirectory(prefix="encode-", dir=cache_dir) as temp:
            source, destination = Path(temp) / "input.json", Path(temp) / "output.json"
            source.write_text(
                _json({"texts": list(missing.values())}), encoding="utf-8"
            )
            worker = (
                Path(__file__).resolve().parents[2] / "scripts/wiki_embedding_worker.py"
            )
            process = subprocess.run(
                [
                    python,
                    str(worker),
                    "--model-dir",
                    str(model_dir),
                    "--input",
                    str(source),
                    "--output",
                    str(destination),
                ],
                capture_output=True,
                text=True,
                timeout=240,
            )
            if process.returncode:
                raise RuntimeError(
                    "Local embedding worker failed; inspect the configured ONNX runtime"
                )
            result = json.loads(destination.read_text(encoding="utf-8"))
            if len(result["vectors"]) != len(missing):
                raise ValueError("Embedding batch count mismatch")
            for key, vector, length in zip(
                missing, result["vectors"], result["token_lengths"]
            ):
                if not vector or not all(
                    isinstance(v, (int, float)) and math.isfinite(v) for v in vector
                ):
                    raise ValueError("Non-finite embedding")
                if length >= 512:
                    raise ValueError(
                        "Embedding token window saturated; reduce source chunk size"
                    )
                value = {"vector": vector, "token_length": length}
                target = cache_dir / (key + ".json")
                temporary = cache_dir / (key + f".{os.getpid()}.tmp")
                temporary.write_text(json.dumps(value), encoding="utf-8")
                temporary.replace(target)
                values[key] = value
    return [values[key]["vector"] for key in keys]


def semantic_ranks(chunks: list[Chunk], query: str):
    python = os.environ.get("PAPER_AGENT_EMBEDDING_PYTHON")
    configured = os.environ.get("PAPER_AGENT_EMBEDDING_DIR")
    if not python or not configured:
        raise RuntimeError(
            "Set PAPER_AGENT_EMBEDDING_PYTHON and PAPER_AGENT_EMBEDDING_DIR for semantic retrieval"
        )
    model_dir = Path(configured).resolve()
    identity = _embedding_identity(model_dir)
    cache = Path(
        os.environ.get("PAPER_AGENT_EMBEDDING_CACHE", str(model_dir / "vectors"))
    ).resolve()
    texts = [f"passage: {c.page.title}\n{c.section.heading}\n{c.text}" for c in chunks]
    vectors = encode_texts(
        texts + ["query: " + query], cache, identity, python, model_dir
    )
    query_vector = vectors[-1]
    if any(len(v) != len(query_vector) for v in vectors):
        raise ValueError("Embedding dimensions mismatch")
    ranked = sorted(
        [
            (i, sum(a * b for a, b in zip(v, query_vector)))
            for i, v in enumerate(vectors[:-1])
        ],
        key=lambda item: (-item[1], chunks[item[0]].key),
    )
    return ranked, identity


def _chunk_result(chunk: Chunk, root: Path):
    raw = "\n".join(chunk.page.lines[chunk.section.start : chunk.section.end])
    before = raw[: chunk.offset]
    line = chunk.section.start + before.count("\n") + 1
    char = len(before.rsplit("\n", 1)[-1])
    return {
        **location(chunk.page, chunk.section, root),
        "chunk_id": chunk.key,
        "excerpt": chunk.text,
        "excerpt_start_line": line,
        "excerpt_start_char": char,
        "excerpt_end_line": line + chunk.text.count("\n"),
        "excerpt_truncated": chunk.offset > 0
        or chunk.offset + len(chunk.text) < len(raw),
    }


def _exact_paths(pages: list[Page], query: str):
    query = query.strip().casefold()
    normalized = re.sub(r"\W", "", query)
    if not normalized:
        return set()
    matches = set()
    for page in pages:
        title = page.title.casefold()
        # 完整标题、slug、DOI或标题冒号前的方法名；不把任意子串当精确命中。
        names = {title, page.path.stem.casefold(), title.split(":", 1)[0]}
        names |= set(re.findall(r"10\.\d{4,9}/[^\s<>\"）)]+", page.text.casefold()))
        if normalized in {re.sub(r"\W", "", name) for name in names}:
            matches.add(page.path)
    return matches


def search_wiki_hybrid(
    ctx: ToolContext,
    query: str,
    limit: int = 5,
    entity_type: str | None = None,
    semantic: bool = True,
) -> ToolResult:
    """Search exact names and BM25 sections, optionally fuse local multilingual E5 with RRF.
    Returns parent-paper candidates with exact excerpts, channel ranks and section/line locations.
    semantic=True requires a configured local encoder; unavailable semantic is an explicit error.
    Scores are relevance signals, never evidence validity. Continue reading conditions/limits.
    """
    if not query.strip() or len(query) > 2000:
        return ToolResult("query must contain 1 to 2000 characters", True)
    if entity_type and entity_type not in ENTITY_TYPES:
        return ToolResult("Unsupported entity_type", True)
    root = _wiki_root()
    pages = load_pages(root)
    owners, warnings = paper_owners(root, pages)
    chosen = [p for p in pages if not entity_type or p.path.parent.name == entity_type]
    chunks = section_chunks(chosen, root)
    if not chunks:
        return ToolResult(_json({"matches": [], "warnings": warnings}))
    proxy = [
        Page(
            Path(str(i)),
            c.text,
            c.text.splitlines(),
            f"{c.page.title}\n{c.section.heading}",
            [Section(c.section.heading, 0, len(c.text.splitlines()))],
        )
        for i, c in enumerate(chunks)
    ]
    lexical = [(int(p.path.name), score) for p, score in rank_pages(proxy, query)]
    exact = _exact_paths(chosen, query)
    channels = {"bm25": lexical[:40]}
    identity = None
    # 精确标识请求直接定位，避免无必要的语义模型调用。
    if semantic and not exact:
        try:
            dense, identity = semantic_ranks(chunks, query)
        except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
            return ToolResult(
                _json(
                    {
                        "error": str(exc),
                        "semantic_status": "unavailable",
                        "hint": "Use semantic=false explicitly for lexical-only results; do not treat this as a full hybrid run.",
                    }
                ),
                True,
            )
        channels["e5"] = dense[:40]
    fused = defaultdict(float)
    ranks = defaultdict(dict)
    for channel, rows in channels.items():
        for rank, (i, score) in enumerate(rows, 1):
            fused[i] += 1 / (60 + rank)
            ranks[i][channel] = {"rank": rank, "raw_score": round(score, 6)}
    for i, chunk in enumerate(chunks):
        if chunk.page.path in exact:
            fused[i] += 1
            ranks[i]["exact"] = {"matched": True}
    by_path = {p.path: p for p in pages}
    groups = {}
    for i in sorted(fused, key=lambda i: (-fused[i], chunks[i].key)):
        chunk = chunks[i]
        for parent in (
            (owners.get(chunk.page.path) or [chunk.page.path])
            if not entity_type
            else [chunk.page.path]
        ):
            record = groups.setdefault(parent, {"score": fused[i], "hits": []})
            # 同栏目相邻窗口只展示一次；最多3个栏目，避免重叠窗口吞没阅读预算。
            if len(record["hits"]) < 3 and not any(
                hit["wiki_path"] == chunk.page.path.relative_to(root).as_posix()
                and hit["section"] == chunk.section.heading
                for hit in record["hits"]
            ):
                record["hits"].append(
                    {**_chunk_result(chunk, root), "channels": ranks[i]}
                )
    matches = []
    for path, group in sorted(
        groups.items(), key=lambda pair: (-pair[1]["score"], str(pair[0]))
    )[: max(1, min(limit, 10))]:
        page = by_path[path]
        matches.append(
            {
                "wiki_path": path.relative_to(root).as_posix(),
                "title": page.title,
                "score": round(group["score"], 6),
                "hits": group["hits"],
                "quality_records": _quality_records(root, path),
                "source_record": _source_record(root, path),
            }
        )
    return ToolResult(
        _json(
            {
                "query": query,
                "ranking": "section-rrf-v1",
                "matches": matches,
                "semantic_status": (
                    "skipped_exact"
                    if exact
                    else "enabled" if semantic else "explicitly_disabled"
                ),
                "embedding_identity": identity,
                "warnings": warnings,
                "note": "RRF ranks fuse candidate lists, not scientific support. Search all channels even when lexical results exist; read conditions and counterevidence.",
            }
        )
    )


def expand_wiki_relations(
    ctx: ToolContext,
    path: str,
    relation: str = "all",
    direction: str = "both",
    limit: int = 6,
) -> ToolResult:
    """Navigate one recorded research relation from a Wiki node, optionally via its source Idea.
    relation is all or one of cites,addresses_same_problem,shares_method,may_apply_to,
    inspired_by,tested_by,contradicts. direction is in/out/both. No edges are invented.
    Results preserve actual direction, unverified hypotheses, source identity and graph line.
    A connected method is not proven applicable; read the target Paper and limitations.
    """
    root = _wiki_root()
    pages = load_pages(root)
    by_node = {f"{NODE_TYPES[p.path.parent.name]}:{p.path.stem}": p for p in pages}
    by_path = {p.path: node for node, p in by_node.items()}
    source = (root / path).resolve()
    node = path if path in by_node else by_path.get(source)
    if (
        node is None
        or direction not in {"in", "out", "both"}
        or relation not in RELATIONS | {"all"}
    ):
        return ToolResult("Unknown Wiki node, relation or direction", True)
    graph = root / "graph/edges.jsonl"
    if not graph.is_file():
        return ToolResult(_json({"neighbors": [], "warnings": ["No recorded graph"]}))
    if not graph.resolve().is_relative_to(root):
        return ToolResult("Graph path escapes Wiki", True)
    raw = graph.read_text(encoding="utf-8")
    edges, warnings = [], []
    for line, text in enumerate(raw.splitlines(), 1):
        if not text.strip():
            continue
        try:
            edge = json.loads(text)
            if (
                edge["from"] not in by_node
                or edge["to"] not in by_node
                or edge["type"] not in RELATIONS
            ):
                warnings.append(f"Skipped invalid graph record at line {line}")
                continue
            edges.append((line, edge))
        except (ValueError, KeyError, TypeError):
            warnings.append(f"Skipped malformed graph record at line {line}")
    starts = {node: None}
    if node.startswith("paper:"):
        for line, edge in edges:
            if edge["type"] == "inspired_by" and edge["to"] == node:
                starts[edge["from"]] = {
                    "from": edge["from"],
                    "to": node,
                    "graph_line": line,
                }
    available = defaultdict(lambda: {"out": 0, "in": 0})
    for _, edge in edges:
        if edge["from"] in starts and edge["to"] not in starts:
            available[edge["type"]]["out"] += 1
        if edge["to"] in starts and edge["from"] not in starts:
            available[edge["type"]]["in"] += 1
    # 此处使用已逐条验证的边，避免单条坏记录使有效来源路径一并丢失。
    owners = defaultdict(set)
    for _, edge in edges:
        if edge["type"] == "inspired_by" and edge["to"].startswith("paper:"):
            owners[by_node[edge["from"]].path].add(by_node[edge["to"]].path)
    for _, edge in edges:
        if edge["type"] == "tested_by":
            owners[by_node[edge["to"]].path].update(
                owners.get(by_node[edge["from"]].path, set())
            )
    neighbors = []
    for line, edge in edges:
        if relation != "all" and edge["type"] != relation:
            continue
        outward = edge["from"] in starts and direction in {"out", "both"}
        inward = edge["to"] in starts and direction in {"in", "both"}
        if not (outward or inward):
            continue
        start = edge["from"] if outward else edge["to"]
        target = edge["to"] if outward else edge["from"]
        if target in starts:
            continue
        page = by_node[target]
        evidence = str(edge.get("evidence", ""))
        neighbors.append(
            {
                "node_id": target,
                "wiki_path": page.path.relative_to(root).as_posix(),
                "title": page.title,
                "paper_read_paths": [
                    p.relative_to(root).as_posix()
                    for p in (
                        sorted(owners.get(page.path, set()))
                        or ([page.path] if page.path.parent.name == "papers" else [])
                    )
                ],
                "relation": edge["type"],
                "from": edge["from"],
                "to": edge["to"],
                "traversal_direction": "out" if outward else "in",
                "via_source_idea": starts[start],
                "status": (
                    "unverified_transfer_hypothesis"
                    if edge["type"] == "may_apply_to"
                    else "recorded_relation_not_independent_replication"
                ),
                "evidence_excerpt": evidence[:1100],
                "evidence_truncated": len(evidence) > 1100,
                "graph_path": "graph/edges.jsonl",
                "graph_line": line,
                "graph_sha256": hashlib.sha256(raw.encode()).hexdigest(),
                "content_sha256": page.fingerprint,
                "quality_records": _quality_records(root, page.path),
            }
        )
    neighbors.sort(
        key=lambda x: (
            x["relation"] == "inspired_by",
            x["relation"] == "tested_by",
            x["graph_line"],
        )
    )
    count = len(neighbors)
    selected = neighbors[: max(1, min(limit, 10))]
    return ToolResult(
        _json(
            {
                "start": node,
                "neighbors": selected,
                "total": count,
                "truncated": len(selected) < count,
                "warnings": warnings,
                "available_relation_types": sorted({e["type"] for _, e in edges}),
                "available_relations_at_start": dict(available),
                "note": "If the requested type is empty, inspect available_relations_at_start and retry another type or all. No edge is not evidence of no relationship. Graph statements may be inferred from notes; verify endpoints and keep transfer claims unverified.",
            }
        )
    )
