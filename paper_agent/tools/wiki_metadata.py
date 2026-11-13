"""Canonical per-paper metadata paths, with read compatibility for older Wikis."""
from pathlib import Path

KINDS = {'source', 'quality', 'references', 'figures', 'relations'}


def metadata_path(paper: Path, kind: str, *, existing: bool = False) -> Path:
    if kind not in KINDS or paper.parent.name != 'papers' or paper.suffix != '.md':
        raise ValueError('Expected a Wiki paper Markdown path and a supported metadata kind')
    target = paper.parent.parent / 'metadata' / paper.stem / f'{kind}.json'
    if existing and not target.exists():
        legacy = paper.with_suffix(f'.{kind}.json')
        if legacy.exists():
            return legacy
    return target


def metadata_records(root: Path, kind: str):
    for paper in sorted((root / 'papers').glob('*.md')):
        path = metadata_path(paper, kind, existing=True)
        if path.is_file():
            yield paper, path
