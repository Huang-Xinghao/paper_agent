"""Move paper sidecars into metadata/<slug>/, updating recorded paths with backups."""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil

from paper_agent.tools.wiki_metadata import KINDS, metadata_path


def migrate(root: Path, backup: Path, state_files=()):
    root = root.resolve()
    moves = {}
    for paper in sorted((root / 'papers').glob('*.md')):
        for kind in sorted(KINDS):
            old, new = paper.with_suffix(f'.{kind}.json'), metadata_path(paper, kind)
            if old.exists():
                if new.exists() and new.read_bytes() != old.read_bytes():
                    raise ValueError(f'Conflicting metadata: {old} and {new}')
                moves[old] = new
    # Back up every changed input before mutating any files.
    replacements = {}
    for old, new in moves.items():
        replacements[str(old)] = str(new)
        replacements[str(old.relative_to(root))] = str(new.relative_to(root))
    def rewrite(text):
        for old, new in sorted(replacements.items(), key=lambda pair: -len(pair[0])):
            text = text.replace(old, new)
        return text
    updates = {}
    files = [*(root / 'metadata').rglob('*.json'), *moves.keys(),
             root / 'graph/edges.jsonl', root / 'zotero-sync.json', *map(Path, state_files)]
    for path in files:
        if not path.is_file():
            continue
        original = path.read_text(encoding='utf-8')
        updated = rewrite(original)
        if original != updated:
            updates[path] = updated
    if not moves and not updates:
        return {'moved': 0, 'updated': 0}
    backup.mkdir(parents=True, exist_ok=False)
    all_paths = sorted(set(moves) | set(updates))
    saved = {}
    for index, path in enumerate(all_paths):
        target = backup / f'{index:05d}-{path.name}'
        shutil.copy2(path, target)
        saved[str(path)] = str(target)
    manifest = {'created_at': datetime.now(timezone.utc).isoformat(), 'root': str(root),
                'moves': {str(k): str(v) for k, v in moves.items()}, 'backups': saved}
    (backup / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    for old, new in moves.items():
        new.parent.mkdir(parents=True, exist_ok=True)
        old.replace(new)
    for path, text in updates.items():
        target = moves.get(path, path)
        temporary = target.with_suffix('.migration.tmp')
        temporary.write_text(text, encoding='utf-8')
        temporary.replace(target)
    return {'moved': len(moves), 'updated': len(updates), 'backup': str(backup)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wiki-root', type=Path, required=True)
    parser.add_argument('--backup', type=Path, required=True)
    parser.add_argument('--state-file', type=Path, action='append', default=[])
    args = parser.parse_args()
    print(json.dumps(migrate(args.wiki_root, args.backup, args.state_file), indent=2))
