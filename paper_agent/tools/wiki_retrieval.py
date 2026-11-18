"""Deterministic, read-only lexical retrieval over live Wiki Markdown files."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from .wiki_layout import entity_paths

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
            result.append("battery" if word == "batteries" else word)
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
    metadata: dict | None = None
    aliases: tuple[str, ...] = ()

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
    metadata = {}
    if start:
        for line in lines[1:start - 1]:
            key, sep, value = line.partition(":")
            if sep:
                try:
                    metadata[key.strip()] = json.loads(value.strip())
                except ValueError:
                    metadata[key.strip()] = value.strip().strip('"').strip("'")
    aliases = []
    if path.parent.name == "papers":
        references = path.parent.parent / "metadata" / path.stem / "references.json"
        try:
            record = json.loads(references.read_text(encoding="utf-8"))
            for term in record.get("coined_terms", []):
                name = term.get("name", "")
                # Only distinctive model-like names; generic phrases are topic evidence.
                if (re.fullmatch(r"[A-Za-z][A-Za-z0-9²_-]{3,60}", name)
                        and (sum(c.isupper() for c in name) >= 2 or any(c.isdigit() for c in name))):
                    aliases.append(name)
        except (OSError, ValueError, TypeError, AttributeError):
            pass
    return Page(path, text, lines, title, sections, metadata, tuple(aliases))


def load_pages(root: Path) -> list[Page]:
    # 只收录活动实体目录的直接子文件，排除日志、索引副本、归档及库外符号链接。
    return [
        load_page(path.resolve())
        for kind in ENTITY_TYPES
        for path in entity_paths(root, kind)
        if path.is_file() and path.resolve().is_relative_to(root)
    ]


def _body(page: Page) -> str:
    return "\n".join(page.lines[page.sections[0].start :]) if page.sections else ""


def identity_priority(page: Page, query: str) -> int:
    """Prioritize declared identities, never names merely mentioned in another body."""
    normalize = lambda value: re.sub(r"[^\w]", "", value.casefold())
    normalized = normalize(query)
    if not normalized:
        return 0
    meta = page.metadata or {}
    names = [page.title, page.path.stem]
    prefix, separator, _ = page.title.partition(":")
    if separator and re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{3,60}", prefix):
        names.append(prefix)
    names.extend(str(meta[k]) for k in ("doi", "arxiv") if meta.get(k))
    if normalized in {normalize(name) for name in names}:
        return 2
    # Full titles may be embedded in a longer discovery query.
    query_words = " ".join(tokenize(query))
    title_words = " ".join(tokenize(page.title))
    if title_words and len(tokenize(page.title)) >= 4 and title_words in query_words:
        return 2
    for alias in page.aliases:
        if re.search(r"(?<!\w)" + re.escape(alias) + r"(?!\w)", query, re.I):
            return 1
    return 0


def rank_pages(pages: list[Page], query: str) -> list[tuple[Page, float]]:
    terms = set(tokenize(query))
    counts = []
    coverage = []
    for page in pages:
        meta = page.metadata or {}
        labels = " ".join(str(v) for key in ("task", "research_field", "method")
                          for v in (meta.get(key) or []) if isinstance(v, str))
        salient = set(tokenize(page.title + " " + labels + " " + " ".join(page.aliases)))
        coverage.append(len(terms & salient) / max(1, len(terms)))
        count = Counter(tokenize(_body(page)))
        count.update(tokenize(page.title) * 3)
        count.update(tokenize(labels) * 2)
        counts.append(count)
    lengths = [sum(count.values()) for count in counts]
    mean = sum(lengths) / max(1, len(lengths))
    frequency = Counter(term for count in counts for term in count)
    ranked = []
    for page, count, length, covered in zip(pages, counts, lengths, coverage):
        score = sum(
            math.log(1 + (len(pages) - frequency[t] + 0.5) / (frequency[t] + 0.5))
            * count[t]
            * 2.2
            / (count[t] + 1.2 * (0.25 + 0.75 * length / max(mean, 1)))
            for t in terms
            if count[t]
        )
        priority = identity_priority(page, query)
        score = score * (0.25 + 0.75 * covered)
        if score > 0 or priority:
            ranked.append((page, score + (2000 if priority == 2 else 25 * covered if priority else 0)))
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


def paper_card(page: Page, root: Path) -> dict:
    """Return the complete stored Markdown, including every section and frontmatter."""
    return {
        "title": page.title,
        "wiki_path": page.path.relative_to(root).as_posix(),
        "read_path": str(page.path.resolve()),
        "content_sha256": page.fingerprint,
        "content": page.text,
        "content_complete": True,
        "note": "完整 Wiki MD 正文，不是搜索时新生成的总结，也不表示本轮已核验 PDF。",
    }
