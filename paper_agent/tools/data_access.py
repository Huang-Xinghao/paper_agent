"""Explicit read-only data roots registered in dataset resource documents."""
import json
from pathlib import Path
import re


def registered_data_sources(knowledge_roots):
    sources = []
    # Only dedicated local-data fields grant access, never arbitrary paths in prose.
    pattern = re.compile(r'^\s*-\s*(数据集根路径|处理后数据路径|原始数据路径(?:（可选）)?|寿命标签路径|标签数据路径)\s*[：:]\s*`(/[^`\n]+)`', re.M)
    for root in knowledge_roots:
        root = Path(root)
        # Keep machine-readable access registration separate from descriptions.
        manifest = root / 'data_sources.json'
        if manifest.is_file():
            for entry in json.loads(manifest.read_text(encoding='utf-8'))['sources']:
                path = Path(entry['path'])
                if not path.is_absolute() or path.resolve() == Path('/'):
                    raise ValueError(f'Invalid registered data path: {path}')
                path = path.resolve()
                sources.append({'resource_id': entry['resource_id'],
                                'kind': entry['kind'], 'path': str(path),
                                'exists': path.exists()})
        for document in sorted(root.rglob('*.md')):
            relative = document.relative_to(root)
            if document.parent.name != '数据集' or any(part.startswith('.') for part in relative.parts):
                continue
            for kind, value in pattern.findall(document.read_text(encoding='utf-8')):
                path = Path(value).resolve()
                if path == Path('/'):
                    continue
                sources.append({'resource_id': relative.with_suffix('').as_posix(),
                                'kind': kind, 'path': str(path), 'exists': path.exists()})
    return sources


def resolve_data_read(workspace, knowledge_roots, value):
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = Path(workspace) / candidate
    candidate = candidate.resolve()
    if candidate.is_relative_to(Path(workspace).resolve()):
        return candidate
    # Figure retrieval returns absolute paths in the shared, read-only library.
    # Resolve both sides so symlinks cannot grant access outside that library.
    from .wiki import resolve_wiki_root
    figure_root = (resolve_wiki_root() / 'figures').resolve()
    if candidate.is_relative_to(figure_root):
        return candidate
    for source in registered_data_sources(knowledge_roots):
        if candidate.is_relative_to(Path(source['path'])):
            return candidate
    raise ValueError(f'path is outside workspace and registered data roots: {value}')
