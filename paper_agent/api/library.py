"""Authenticated, read-only browsing of the shared research libraries."""

import mimetypes
import os
import re
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from .store import REPO
from paper_agent.tools.wiki import resolve_wiki_root

router = APIRouter(prefix="/api/libraries", tags=["libraries"])
Library = Literal["paradigms", "battery"]
MAX_TEXT = 2 * 1024 * 1024
ASSETS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".pdf"}


def library_root(library: Library):
    if library == "paradigms":
        return resolve_wiki_root()
    return Path(os.environ.get("PAPER_AGENT_BATTERY_ROOT", REPO / "电池资源库")).expanduser().resolve()


def document(path, relative):
    text = path.read_text(encoding="utf-8", errors="replace")
    body = re.sub(r"\A---\r?\n.*?\r?\n---(?:\r?\n|$)", "", text, count=1, flags=re.S).strip()
    title = next((line[2:].strip() for line in body.splitlines() if line.startswith("# ")), path.stem)
    excerpt = " ".join(line.strip() for line in body.splitlines()
                       if line.strip() and not line.startswith(("#", "_", "|", "```")))[:240]
    return {"path": relative.as_posix(), "title": title,
            "category": relative.parts[0] if len(relative.parts) > 1 else "overview",
            "excerpt": excerpt, "modified": path.stat().st_mtime}, body


@router.get("/{library}")
def list_library(library: Library, q: str = Query("", max_length=200),
                 category: str = Query("", max_length=100), limit: int = Query(1000, ge=1, le=1000)):
    root = library_root(library)
    if not root.is_dir():
        return {"available": False, "items": [], "categories": [], "total": 0, "matched": 0, "truncated": False}
    documents = []
    truncated = False
    for directory, folders, files in os.walk(root, followlinks=False):
        folders[:] = sorted(name for name in folders if not name.startswith(".") and not (Path(directory) / name).is_symlink())
        for name in sorted(files):
            path = Path(directory) / name
            if name.startswith(".") or path.suffix.lower() != ".md" or path.is_symlink():
                continue
            try:
                if path.stat().st_size > MAX_TEXT:
                    continue
                if len(documents) >= 5000:
                    truncated = True
                    break
                item, body = document(path, path.relative_to(root))
                documents.append((item, body))
            except OSError:
                continue
        if truncated:
            break
    counts = {}
    for item, _ in documents:
        counts[item["category"]] = counts.get(item["category"], 0) + 1
    terms = q.casefold().split()
    matches = [item for item, body in documents
               if (not category or item["category"] == category)
               and all(term in f'{item["title"]} {item["path"]} {body}'.casefold() for term in terms)]
    matches.sort(key=lambda item: (item["category"] != "overview", item["path"]))
    return {"available": True, "items": matches[:limit],
            "categories": [{"id": key, "count": counts[key]} for key in sorted(counts)],
            "total": len(documents), "matched": len(matches), "truncated": truncated or len(matches) > limit}


def public_path(library, relative, suffixes):
    root = library_root(library)
    parts = Path(relative).parts
    if not relative or Path(relative).is_absolute() or any(part.startswith(".") for part in parts):
        raise HTTPException(400, "资源路径无效")
    path = root / relative
    resolved = path.resolve()
    if (not resolved.is_relative_to(root) or not resolved.is_file()
            or resolved.suffix.lower() not in suffixes
            or any((root.joinpath(*parts[:index + 1])).is_symlink() for index in range(len(parts)))):
        raise HTTPException(404, "资源不存在或不支持预览")
    return resolved


@router.get("/{library}/document")
def read_document(library: Library, path: str = Query(max_length=1000)):
    target = public_path(library, path, {".md"})
    if target.stat().st_size > MAX_TEXT:
        raise HTTPException(413, "文档过大，暂不支持预览")
    item, body = document(target, Path(path))
    return {**item, "content": body}


@router.get("/{library}/asset")
def read_asset(library: Library, path: str = Query(max_length=1000)):
    target = public_path(library, path, ASSETS)
    return FileResponse(target, media_type=mimetypes.guess_type(target.name)[0] or "application/octet-stream")


@router.get('/paradigms/graph')
def paradigm_graph():
    """Expose recorded wiki relationships, never infer scientific links from labels."""
    import json

    catalog = list_library('paradigms', q='', category='', limit=1000)
    root = library_root('paradigms')
    nodes = []
    for item in catalog['items']:
        target = public_path('paradigms', item['path'], {'.md'})
        raw = target.read_text(encoding='utf-8', errors='replace')
        header = re.match(r'\A---\r?\n(.*?)\r?\n---(?:\r?\n|$)', raw, re.S)
        metadata = {}
        if header:
            for key in ('node_id', 'type', 'task', 'method', 'scenario', 'research_field'):
                match = re.search(rf'^{key}:\s*(.+)$', header[1], re.M)
                if match:
                    value = match[1].strip()
                    try:
                        metadata[key] = json.loads(value)
                    except ValueError:
                        metadata[key] = value.strip('\"\'')
        if not isinstance(metadata.get('node_id'), str):
            continue
        nodes.append({**item, 'id': metadata['node_id'],
                      'tags': [tag for key in ('task', 'method', 'scenario', 'research_field')
                               for tag in (metadata.get(key) if isinstance(metadata.get(key), list) else [])
                               if isinstance(tag, str)]})
    ids = {node['id'] for node in nodes}
    edges = []
    seen = set()
    edge_path = root / 'graph' / 'edges.jsonl'
    # Reuse the same path boundary and symlink protections as document reading.
    if edge_path.exists():
        try:
            target = public_path('paradigms', 'graph/edges.jsonl', {'.jsonl'})
            if target.stat().st_size <= MAX_TEXT:
                for line in target.read_text(encoding='utf-8', errors='replace').splitlines():
                    try:
                        edge = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(edge, dict):
                        continue
                    source, dest, relation = edge.get('from'), edge.get('to'), edge.get('type')
                    if not all(isinstance(value, str) for value in (source, dest, relation)):
                        continue
                    key = (source, dest, relation)
                    if source in ids and dest in ids and key not in seen:
                        seen.add(key)
                        edges.append({'source': source, 'target': dest, 'type': relation,
                                      'evidence': str(edge.get('evidence', ''))[:2000]})
        except HTTPException:
            pass
    return {'available': catalog['available'], 'nodes': nodes, 'edges': edges,
            'truncated': catalog['truncated']}


@router.get('/paradigms/figures')
def paradigm_figures():
    """Read archived figure images and their original caption sidecars."""
    root = library_root('paradigms')
    papers = {}
    catalog = list_library('paradigms', q='', category='papers', limit=1000)
    for item in catalog['items']:
        papers[Path(item['path']).stem] = item
    items = []
    truncated = False
    figure_root = root / 'figures'
    if figure_root.is_dir() and not figure_root.is_symlink():
        for directory, folders, files in os.walk(figure_root, followlinks=False):
            folders[:] = sorted(name for name in folders if not name.startswith('.')
                                and not (Path(directory) / name).is_symlink())
            for name in sorted(files):
                image = Path(directory) / name
                if name.startswith('.') or image.suffix.lower() not in ASSETS - {'.pdf'}:
                    continue
                relative = image.relative_to(root).as_posix()
                try:
                    public_path('paradigms', relative, ASSETS - {'.pdf'})
                except HTTPException:
                    continue
                if len(items) >= 1000:
                    truncated = True
                    break
                text = ''
                try:
                    note = public_path('paradigms', image.with_suffix('.md').relative_to(root).as_posix(), {'.md'})
                    if note.stat().st_size <= MAX_TEXT:
                        text = note.read_text(encoding='utf-8', errors='replace')
                except HTTPException:
                    pass
                source = re.search(r'^Source:\s*paper:([^\s]+)', text, re.M)
                paper_id = source[1] if source else image.parent.name
                paper = papers.get(paper_id)
                title = re.search(r'^Paper title:\s*(.+)$', text, re.M)
                label = re.search(r'^#\s+(.+)$', text, re.M)
                page = re.search(r'^PDF page:\s*(\d+)', text, re.M)
                caption = '\n'.join(line for line in text.splitlines()
                                    if not line.startswith(('# ', 'Source:', 'PDF page:', 'Paper title:'))).strip()
                items.append({'id': relative, 'image_path': relative, 'paper_id': paper_id,
                              'paper_title': paper['title'] if paper else title[1].strip() if title else paper_id,
                              'paper_path': paper['path'] if paper else None,
                              'label': label[1].strip() if label else image.stem,
                              'page': int(page[1]) if page else None, 'caption': caption})
            if truncated:
                break
    return {'available': root.is_dir(), 'items': items, 'total': len(items), 'truncated': truncated}
