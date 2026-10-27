"""Read-only caption retrieval for the independent figure reference library."""
from __future__ import annotations

from collections import Counter
import json
import math
import re

from .core import ToolContext, ToolResult, tool
from .wiki import resolve_wiki_root

# Bridge common Chinese design requests to English author captions without an API.
CONCEPTS = (
    ('架构', 'architecture', 'architectural'),
    ('编码器', 'encoder'), ('解码器', 'decoder'),
    ('注意力', 'attention'), ('多专家', '专家混合', 'mixture', 'experts', 'expert'),
    ('预测', 'prediction', 'predict', 'forecasting', 'forecast'),
    ('电池', 'battery', 'batteries'), ('退役', 'retired', 'second-life'),
    ('退化', 'degradation'), ('时间序列', 'time-series'),
    ('流程', 'workflow', 'pipeline', 'flowchart'),
    ('融合', 'fusion'), ('输入', 'input', 'inputs'), ('输出', 'output', 'outputs'),
)
STOPWORDS = set('a an the of and or to in for with from by on is are this that figure fig '
                '图 图片 参考 我们 一个 设计 请 帮我 架构图'.split())


def tokens(text: str) -> list[str]:
    text = text.casefold()
    words = re.findall(r'[a-z0-9]+(?:-[a-z0-9]+)*|[\u4e00-\u9fff]+', text)
    result = [w for w in words if w not in STOPWORDS and len(w) > 1]
    for index, aliases in enumerate(CONCEPTS):
        if any((alias in text if re.search(r'[\u4e00-\u9fff]', alias)
                else re.search(r'\b' + re.escape(alias) + r'\b', text)) for alias in aliases):
            result.append(f'concept:{index}')
    return result


@tool(read_only=True)
def search_figure_references(ctx: ToolContext, query: str, top_k: int = 3) -> ToolResult:
    """Rank local figure/caption pairs by caption BM25 and bilingual design terms.

    Returns full captions, source paper IDs, PDF pages and image paths for later
    view_image calls. This searches text, not image pixels; it makes no model call.
    """
    query_tokens = set(tokens(query))
    if not query_tokens:
        return ToolResult('query must contain searchable terms', True,
                          {'error_code': 'EMPTY_QUERY'})
    if not 1 <= top_k <= 20:
        return ToolResult('top_k must be between 1 and 20', True,
                          {'error_code': 'INVALID_TOP_K'})
    root = (resolve_wiki_root() / 'figures').resolve()
    documents = []
    for caption_path in sorted(root.glob('*/*.md')):
        if not caption_path.resolve().is_relative_to(root):
            continue
        image = next((caption_path.with_suffix(ext) for ext in ('.png', '.jpg', '.jpeg')
                      if caption_path.with_suffix(ext).is_file()
                      and caption_path.with_suffix(ext).resolve().is_relative_to(root)), None)
        if image is None:
            continue
        markdown = caption_path.read_text(encoding='utf-8', errors='replace')
        # Source identifiers and page numbers are metadata, not ranking evidence.
        caption = '\n'.join(line for line in markdown.splitlines()
                            if not line.startswith(('# ', 'Source:', 'PDF page:', 'Review required:'))).strip()
        counts = Counter(tokens(caption))
        if not counts:
            continue
        page = re.search(r'^PDF page:\s*(\d+)\s*$', markdown, re.M)
        source = re.search(r'^Source:\s*(paper:[\w.-]+)\s*$', markdown, re.M)
        documents.append((caption_path, image, caption, counts, page, source, markdown))
    average = sum(sum(d[3].values()) for d in documents) / max(1, len(documents))
    frequency = Counter(term for d in documents for term in d[3])
    matches = []
    for path, image, caption, counts, page, source, markdown in documents:
        score = 0.0
        matched = sorted(query_tokens & counts.keys())
        for term in matched:
            tf = counts[term]
            idf = math.log(1 + (len(documents) - frequency[term] + .5) / (frequency[term] + .5))
            score += idf * tf * 2.2 / (tf + 1.2 * (.25 + .75 * sum(counts.values()) / average))
        if score:
            matches.append({'paper_id': source.group(1) if source else 'paper:' + path.parent.name,
                            'image_path': ctx.display_path(image.resolve()),
                            'caption_path': ctx.display_path(path.resolve()),
                            'pdf_page': int(page.group(1)) if page else None,
                            'caption': caption, 'caption_markdown': markdown,
                            'score': round(score, 6), 'matched_terms': matched})
    matches.sort(key=lambda m: (-m['score'], m['image_path']))
    return ToolResult(json.dumps({'query': query, 'method': 'caption_bm25_bilingual_terms',
                                 'library_size': len(documents), 'total_matches': len(matches),
                                 'matches': matches[:top_k]}, ensure_ascii=False, indent=2))
