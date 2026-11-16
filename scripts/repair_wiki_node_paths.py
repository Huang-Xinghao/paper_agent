"""Migrate partial corpus nodes whose Unicode identifiers were previously ASCII-stripped."""
import argparse
import json
from pathlib import Path
import re
import shutil
from paper_agent.storage import write_json


def migrate(root, report, backup):
    root, backup = Path(root).resolve(), Path(backup).resolve()
    moves = {}
    for item in report['items']:
        error = item.get('error', '')
        if item['status'] != 'failed' or 'FileNotFoundError' not in error:
            continue
        match = re.search(r"/ideas/([^/'\n]+)-paper-idea\.md", error)
        if not match:
            continue
        paper_slug = match[1]
        old_paper_slug = re.sub(r'[^a-z0-9._-]+', '-', paper_slug.lower()).strip('-')
        old_idea = root / 'ideas' / f'{old_paper_slug}-paper-idea.md'
        target = root / 'ideas' / f'{paper_slug}-paper-idea.md'
        if old_idea.is_file() and old_idea != target:
            moves[old_idea] = target
        for source in (root / 'experiments').glob(f'{old_paper_slug}-*.md'):
            content = source.read_text()
            if f'idea:{paper_slug}-paper-idea' not in content:
                continue
            suffix = source.stem[len(old_paper_slug):]
            moves[source] = root / 'experiments' / f'{paper_slug}{suffix}.md'
    for source, target in moves.items():
        if target.exists():
            raise ValueError(f'Cannot overwrite existing node: {target}')
    replacements = {}
    for source, target in moves.items():
        prefix = 'idea' if source.parent.name == 'ideas' else 'exp'
        replacements[f'{prefix}:{source.stem}'] = f'{prefix}:{target.stem}'
        replacements[str(source.relative_to(root))] = str(target.relative_to(root))
    changes = {}
    for folder in ['papers', 'ideas', 'experiments']:
        for p in (root / folder).glob('*.md'):
            if p.name == 'README.md':
                continue
            before = p.read_text()
            after = before
            for old, new in replacements.items():
                after = after.replace(old, new)
            if after != before or p in moves:
                changes[p] = after
    graph = root / 'graph/edges.jsonl'
    if graph.is_file():
        before = graph.read_text()
        after = before
        for old, new in replacements.items():
            after = after.replace(old, new)
        if after != before:
            changes[graph] = after
    # Back up all inputs before the first mutation.
    for source in changes:
        target = backup / source.relative_to(root)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    for source, content in changes.items():
        target = moves.get(source, source)
        target.write_text(content)
        if source != target:
            source.unlink()
    result = {'moved': {str(k.relative_to(root)): str(v.relative_to(root)) for k, v in moves.items()},
              'updated_files': len(changes)}
    write_json(backup / 'manifest.json', result)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--wiki-root', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--backup', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(migrate(args.wiki_root, json.loads(args.report.read_text()), args.backup), ensure_ascii=False))
