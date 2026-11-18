"""Publication checks and reversible retirement of generated Wiki content."""

import hashlib
import json
from pathlib import Path
import re
import unicodedata

from paper_agent.storage import write_json
from .wiki_layout import library_root


def assert_same_identity(path: Path, metadata: dict) -> None:
    """A shared DOI or filename alone must never authorize replacing another study."""
    from .research_wiki import _last_name

    front = path.read_text(encoding="utf-8")[4:].split("\n---\n", 1)[0]

    def field(key):
        match = re.search(rf"(?m)^{key}: (.+)$", front)
        return json.loads(match[1]) if match else None

    def key(value):
        return "".join(
            c for c in unicodedata.normalize("NFKC", value).casefold() if c.isalnum()
        )

    old_authors, new_authors = field("authors") or [], metadata.get("authors") or []
    if (
        key(field("title") or "") != key(metadata["title"])
        or not old_authors
        or not new_authors
        or not (
            {key(_last_name(a)) for a in old_authors}
            & {key(_last_name(a)) for a in new_authors}
        )
    ):
        raise ValueError(
            f"Paper identity conflict: {path}; title/authors differ from incoming study"
        )


def markdown_errors(text: str) -> list[str]:
    """Check GFM table widths outside fences, including pipes inside inline code."""
    lines = text.splitlines()
    errors, fence, expected = [], None, None

    def cells(line):
        # A pipe is escaped iff it follows an odd number of backslashes.
        parts, start, separators = [], 0, []
        for i, char in enumerate(line):
            if char == "|" and (len(line[:i]) - len(line[:i].rstrip("\\"))) % 2 == 0:
                parts.append(line[start:i])
                start = i + 1
                separators.append(i)
        parts.append(line[start:])
        if separators and separators[0] == 0:
            parts.pop(0)
        if separators and separators[-1] == len(line) - 1:
            parts.pop()
        return parts

    previous = None
    for n, line in enumerate(lines, 1):
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if fence:
            if (
                marker
                and marker[1][0] == fence[0]
                and len(marker[1]) >= fence[1]
                and not marker[2].strip()
            ):
                fence = None
            continue
        if marker:
            fence = (marker[1][0], len(marker[1]))
            expected = None
            previous = None
            continue
        if re.search(r"(?<![A-Za-z])TODO(?![A-Za-z])", line, re.I):
            errors.append(f"line {n}: TODO placeholder")
        row = cells(line.strip())
        if "|" in line and row and all(re.fullmatch(r"\s*:?-+:?\s*", c) for c in row):
            expected = len(row)
            if previous and len(cells(previous)) != expected:
                errors.append(f"line {n-1}: table header width")
        elif expected is not None:
            if (
                not line.strip()
                or "|" not in line
                or re.match(r"^\s{0,3}(#|>|[-*+] )", line)
            ):
                expected = None
            elif len(row) != expected:
                errors.append(
                    f"line {n}: table has {len(row)} cells; expected {expected}"
                )
        previous = line.strip()
    return errors


def remove_template_placeholders(path: Path) -> None:
    """Omit optional sections whose entire body is the untouched template marker."""
    text = path.read_text(encoding="utf-8")
    # Exact marker/body matching avoids removing user notes or unfinished prose.
    text = re.sub(
        r"(?m)^## (?:Problem / Gap|Method|Limitations / Failure Modes)\n\s*_TODO\._\s*(?=^## |\Z)",
        "",
        text,
    )
    path.write_text(text, encoding="utf-8")


def retire_generated_experiments(
    root: Path, idea_slug: str, current: set[str], tag: str
) -> None:
    """Archive only this publisher's replaced experiments; preserve user experiments."""
    library = library_root(root)
    retired, records = set(), []
    aliases_path = library / "graph/path_aliases.json"
    aliases = json.loads(aliases_path.read_text()) if aliases_path.exists() else {}
    for path in sorted((root / "experiments").glob("*.md")):
        if path.name in current:
            continue
        content = path.read_text(encoding="utf-8")
        front = content[4:].split("\n---\n", 1)[0]
        if not re.search(
            rf'(?m)^idea_id: ["\']?idea:{re.escape(idea_slug)}["\']?$', front
        ):
            continue
        match = re.search(r"(?m)^tags: (.+)$", front)
        if not match or tag not in json.loads(match[1]):
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        target = root / "archive/superseded-experiments" / digest[:16] / path.name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.read_bytes())
        rel, archived = str(path.relative_to(library)), str(target.relative_to(library))
        aliases[rel] = archived
        retired.add("exp:" + path.stem)
        records.append(
            dict(
                original=rel,
                archived=archived,
                sha256=digest,
                reason="Replaced publisher experiment set",
            )
        )
        path.unlink()
    if not retired:
        return
    edge_path = library / "graph/edges.jsonl"
    edges = [
        json.loads(line) for line in edge_path.read_text().splitlines() if line.strip()
    ]
    removed = [e for e in edges if e["from"] in retired or e["to"] in retired]
    bundle = (
        root
        / "archive/superseded-experiments"
        / hashlib.sha256(json.dumps(records, sort_keys=True).encode()).hexdigest()[:16]
    )
    write_json(bundle / "manifest.json", dict(files=records, edges=removed))
    edge_path.write_text(
        "".join(
            json.dumps(e, ensure_ascii=False) + "\n" for e in edges if e not in removed
        ),
        encoding="utf-8",
    )
    # Flatten older aliases that previously targeted a now retired path.
    aliases = {key: aliases.get(value, value) for key, value in aliases.items()}
    write_json(aliases_path, aliases)
