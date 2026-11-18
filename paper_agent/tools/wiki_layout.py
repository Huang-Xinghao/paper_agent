"""Wiki origin directories with one shared relationship graph.

Existing flat libraries remain readable while they are migrated.
"""
import json
from pathlib import Path

ORIGINS = ('literature', 'autoresearch')
ENTITY_DIRS = ('papers', 'ideas', 'experiments', 'claims')
CONTENT_DIRS = (*ENTITY_DIRS, 'raw-paper', 'sources', 'metadata', 'figures')


def library_root(root: Path) -> Path:
    root = Path(root).resolve()
    return root.parent if root.name in ORIGINS and (root.parent / 'graph').is_dir() else root


def split_layout(root: Path) -> bool:
    root = library_root(root)
    return any((root / origin).is_dir() for origin in ORIGINS)


def corpus_root(root: Path, origin: str = 'literature') -> Path:
    if origin not in ORIGINS:
        raise ValueError('Unknown Wiki origin')
    root = Path(root).resolve()
    if root.name in ORIGINS and library_root(root) != root:
        return root
    return root / origin if split_layout(root) else root


def corpus_roots(root: Path) -> list[Path]:
    root = Path(root).resolve()
    if root.name in ORIGINS and library_root(root) != root:
        return [root]
    return [root / origin for origin in ORIGINS] if split_layout(root) else [root]


def entity_paths(root: Path, kind: str):
    return sorted(p for corpus in corpus_roots(root) for p in (corpus / kind).glob('*.md')
                  if p.is_file() and not p.is_symlink())


def entity_dirs(root: Path):
    return {corpus / kind for corpus in corpus_roots(root) for kind in ENTITY_DIRS}


def resolve_legacy_path(root: Path, relative: str | Path) -> Path:
    """Resolve pre-migration links without leaving redirect files in the corpus."""
    local_root, relative = Path(root).resolve(), Path(relative)
    direct = local_root / relative
    root = library_root(local_root)
    if direct.exists() or relative.is_absolute() or '..' in relative.parts:
        return direct
    aliases_path = root / 'graph' / 'path_aliases.json'
    if aliases_path.is_file():
        aliases = json.loads(aliases_path.read_text(encoding='utf-8'))
        candidates = [(local_root / relative).relative_to(root)]
        if relative.parts and relative.parts[0] in CONTENT_DIRS:
            candidates.extend(Path(origin) / relative for origin in ORIGINS)
        targets = {root / aliases[str(path)] for path in candidates if str(path) in aliases}
        if len(targets) == 1:
            target = targets.pop().resolve()
            if target.is_relative_to(root) and target.is_file():
                return target
    if relative.parts and relative.parts[0] in CONTENT_DIRS:
        matches = [root / origin / relative for origin in ORIGINS if (root / origin / relative).exists()]
        if len(matches) == 1:
            return matches[0]
    return direct
