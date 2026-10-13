"""Explicit read-only data roots registered in dataset resource documents."""
from pathlib import Path
import re


def registered_data_sources(knowledge_roots):
    sources = []
    # Only dedicated local-data fields grant access, never arbitrary paths in prose.
    pattern = re.compile(r'^\s*-\s*(处理后数据路径|原始数据路径(?:（可选）)?|寿命标签路径|标签数据路径)\s*[：:]\s*`(/[^`\n]+)`', re.M)
    for root in knowledge_roots:
        for document in sorted((Path(root) / '数据集').glob('*.md')):
            for kind, value in pattern.findall(document.read_text(encoding='utf-8')):
                path = Path(value).resolve()
                if path == Path('/'):
                    continue
                sources.append({'resource_id': '数据集/' + document.stem,
                                'kind': kind, 'path': str(path), 'exists': path.exists()})
    return sources


def resolve_data_read(workspace, knowledge_roots, value):
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = Path(workspace) / candidate
    candidate = candidate.resolve()
    if candidate.is_relative_to(Path(workspace).resolve()):
        return candidate
    for source in registered_data_sources(knowledge_roots):
        if candidate.is_relative_to(Path(source['path'])):
            return candidate
    raise ValueError(f'path is outside workspace and registered data roots: {value}')
