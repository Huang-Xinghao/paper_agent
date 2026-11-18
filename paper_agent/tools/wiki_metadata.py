"""Canonical per-paper metadata paths."""
from pathlib import Path
from .wiki_layout import entity_paths

KINDS = {'source', 'quality', 'references', 'figures', 'relations'}


def metadata_path(paper: Path, kind: str) -> Path:
    if kind not in KINDS or paper.parent.name != 'papers' or paper.suffix != '.md':
        raise ValueError('Expected a Wiki paper Markdown path and a supported metadata kind')
    target = paper.parent.parent / 'metadata' / paper.stem / f'{kind}.json'
    return target


def metadata_records(root: Path, kind: str):
    for paper in entity_paths(root, 'papers'):
        path = metadata_path(paper, kind)
        if path.is_file():
            yield paper, path
