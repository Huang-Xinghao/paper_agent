"""Deterministic, read-only lexical retrieval over live Wiki Markdown files."""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

ENTITY_TYPES = ("papers", "ideas", "experiments", "claims")
STOPWORDS = set(
    "a an the of and or to in for with from by on is are this that how what paper method".split()
)
TOKEN = re.compile(r"[a-z0-9]+|[\u3400-\u9fff]+", re.I)


def tokenize(text: str) -> list[str]:
    """中文用相邻双字覆盖自然语言查询；不声称具有同义或跨语言召回能力。"""
    result = []
    for word in TOKEN.findall(text.casefold()):
        if "\u3400" <= word[0] <= "\u9fff":
            result.extend(word[i : i + 2] for i in range(len(word) - 1))
            if len(word) == 1:
                result.append(word)
        elif word not in STOPWORDS:
            result.append(word)
    return result


@dataclass
class Section:
    heading: str
    start: int  # 0-based, includes the heading when present
    end: int  # exclusive


@dataclass
class Page:
    path: Path
    text: str
    lines: list[str]
    title: str
    sections: list[Section]

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()


def load_page(path: Path) -> Page:
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    start = 0
    if lines and lines[0] == "---":
        start = next((i + 1 for i in range(1, len(lines)) if lines[i] == "---"), 0)
    title = next(
        (line[2:].strip() for line in lines[start:] if line.startswith("# ")), path.stem
    )
    # 二级标题作为稳定阅读单元，内部三级标题（如问题记录）保留在所属栏目内。
    headings = [
        (i, line[3:].strip())
        for i, line in enumerate(lines[start:], start)
        if line.startswith("## ")
    ]
    sections = []
    if not headings or headings[0][0] > start:
        sections.append(
            Section("Overview", start, headings[0][0] if headings else len(lines))
        )
    sections.extend(
        Section(h, i, headings[n + 1][0] if n + 1 < len(headings) else len(lines))
        for n, (i, h) in enumerate(headings)
    )
    return Page(path, text, lines, title, sections)


def load_pages(root: Path) -> list[Page]:
    # 只收录活动实体目录的直接子文件，排除日志、索引副本、归档及库外符号链接。
    return [
        load_page(path.resolve())
        for kind in ENTITY_TYPES
        for path in sorted((root / kind).glob("*.md"))
        if path.is_file() and path.resolve().is_relative_to(root)
    ]


def _body(page: Page) -> str:
    return "\n".join(page.lines[page.sections[0].start :]) if page.sections else ""


def rank_pages(pages: list[Page], query: str) -> list[tuple[Page, float]]:
    terms = set(tokenize(query))
    counts = [Counter(tokenize(page.title + "\n" + _body(page))) for page in pages]
    lengths = [sum(count.values()) for count in counts]
    mean = sum(lengths) / max(1, len(lengths))
    frequency = Counter(term for count in counts for term in count)
    ranked = []
    for page, count, length in zip(pages, counts, lengths):
        score = sum(
            math.log(1 + (len(pages) - frequency[t] + 0.5) / (frequency[t] + 0.5))
            * count[t]
            * 2.2
            / (count[t] + 1.2 * (0.25 + 0.75 * length / max(mean, 1)))
            for t in terms
            if count[t]
        )
        if score > 0:
            ranked.append((page, score))
    return sorted(ranked, key=lambda item: (-item[1], item[0].path.as_posix()))


def location(page: Page, section: Section, root: Path) -> dict:
    content = "\n".join(page.lines[section.start : section.end])
    # 页码是 Wiki 中已有的原文标注，不冒充本轮独立 PDF 核验。
    refs = list(
        dict.fromkeys(
            re.findall(r"PDF[^\n。；]{0,100}?(?:页|pp?\.[^）)\n]{1,60})", content)
        )
    )
    return {
        "wiki_path": page.path.relative_to(root).as_posix(),
        "section": section.heading,
        "start_line": section.start + 1,
        "end_line": section.end,
        "read_offset": section.start,
        "read_limit": section.end - section.start,
        "source_mentions": refs[:8],
        "content_sha256": page.fingerprint,
    }


def excerpts(page: Page, query: str, root: Path, limit: int = 3) -> list[dict]:
    terms = set(tokenize(query))
    results = []
    for section in page.sections:
        for i in range(section.start, section.end):
            line = page.lines[i]
            if not line.strip() or line.startswith("#"):
                continue
            matched = terms & set(tokenize(line))
            if not matched:
                continue
            first = min(
                (line.casefold().find(t) for t in matched if t in line.casefold()),
                default=0,
            )
            offset = max(0, first - 120)
            excerpt = line[offset : offset + 700]
            results.append(
                {
                    **location(page, section, root),
                    "line": i + 1,
                    "excerpt": excerpt,
                    "excerpt_start_char": offset,
                    "excerpt_truncated": offset > 0
                    or offset + len(excerpt) < len(line),
                    "matched_terms": sorted(matched),
                    "_coverage": len(matched),
                }
            )
    results.sort(key=lambda item: (-item["_coverage"], item["line"]))
    # 优先展示不同栏目，避免长篇方法描述挤掉条件与限制。
    chosen = []
    seen = set()
    for result in results:
        if result["section"] in seen:
            continue
        seen.add(result["section"])
        result.pop("_coverage")
        chosen.append(result)
        if len(chosen) == limit:
            break
    return chosen


def paper_owners(
    root: Path, pages: list[Page]
) -> tuple[dict[Path, list[Path]], list[str]]:
    """只沿来源关系归并，不把主题相似或迁移假设误当成来源。"""
    import json

    by_node = {
        f"{dict(papers='paper', ideas='idea', experiments='exp', claims='claim')[p.path.parent.name]}:{p.path.stem}": p.path
        for p in pages
    }
    path = root / "graph/edges.jsonl"
    if not path.is_file():
        return {}, []
    try:
        edges = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        parents = defaultdict(set)
        for edge in edges:
            if (
                edge["type"] == "inspired_by"
                and edge["to"] in by_node
                and edge["to"].startswith("paper:")
            ):
                parents[edge["from"]].add(by_node[edge["to"]])
        owners = {
            by_node[node]: sorted(paths)
            for node, paths in parents.items()
            if node in by_node
        }
        for edge in edges:
            if edge["type"] == "tested_by" and edge["to"] in by_node:
                owners.setdefault(by_node[edge["to"]], [])
                owners[by_node[edge["to"]]] = sorted(
                    set(owners[by_node[edge["to"]]]) | parents[edge["from"]]
                )
        return owners, []
    except (OSError, ValueError, KeyError, TypeError):
        return {}, ["来源图无法读取；派生页面保留为独立结果，未猜测归属。"]


CARD_HEADINGS = (
    "One-line thesis",
    "Problem / Gap",
    "Method",
    "Key Results",
    "Limitations / Failure Modes",
)


def paper_card(page: Page, root: Path, max_chars: int = 1000) -> dict:
    fields = []
    for heading in CARD_HEADINGS:
        section = next((s for s in page.sections if s.heading == heading), None)
        if section is None:
            fields.append({"section": heading, "status": "missing", "content": ""})
            continue
        body = "\n".join(page.lines[section.start + 1 : section.end]).strip()
        placeholder = not body or body.startswith(
            ("_TODO", "_Not yet", "_To be", "<!--")
        )
        fields.append(
            {
                **location(page, section, root),
                "status": "missing" if placeholder else "present",
                "content": "" if placeholder else body[:max_chars],
                "truncated": not placeholder and len(body) > max_chars,
            }
        )
    return {
        "title": page.title,
        "wiki_path": page.path.relative_to(root).as_posix(),
        "content_sha256": page.fingerprint,
        "fields": fields,
        "note": "原栏目摘录，不是新生成的结论；present 仅表示有正文，不代表证据核验通过。",
    }
