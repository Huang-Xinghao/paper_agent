"""Group MinerU 4.x image/chart blocks using caption anchors and geometry.

No paper-specific page numbers or figure counts. Captions below figures are
supported. Above-figure captions, cross-page figures, and ambiguous layouts are
reported for review rather than silently assumed correct.
"""
import argparse
import heapq
import json
import math
import re
from pathlib import Path

ANCHOR = re.compile(r'^(?:fig(?:ure)?\.?\s*)(S?\d+)\s*(?:[|:．.\u2013\u2014-])\s*', re.I)
LABEL = re.compile(r'^\(?[a-zA-Z]\)?[.)]?$')


def overlap(a, b, axis=0):
    return max(0, min(a[axis+2], b[axis+2]) - max(a[axis], b[axis]))


def union(boxes):
    return [min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes)]


def gap(a, b):
    dx = max(0, a[0]-b[2], b[0]-a[2])
    dy = max(0, a[1]-b[3], b[1]-a[3])
    return math.hypot(dx, dy)


def group_page(page):
    blocks = page['blocks']
    panels = [dict(b, source_index=i) for i, b in enumerate(blocks)
              if b['type'] in ('image', 'chart')]
    captions = []
    seen = set()
    for b in blocks:
        candidates = list(b.get('captions', []))
        if b['type'] in ('image_caption', 'chart_caption', 'text'):
            candidates += [b]
        for c in candidates:
            content = c.get('content', '')
            if not isinstance(content, str) or not content.strip() or 'bbox' not in c:
                continue
            key = (tuple(c['bbox']), content.strip())
            if key not in seen:
                seen.add(key)
                captions.append({'bbox': c['bbox'], 'content': content.strip(),
                                 'nested_caption': c is not b or b['type'] in ('image_caption', 'chart_caption')})
    anchors = [c for c in captions if ANCHOR.match(c['content'])]
    groups = [{'number': ANCHOR.match(c['content']).group(1), 'anchor': c,
               'caption_parts': [c], 'review_reasons': []} for c in anchors]
    unattached = []
    for c in captions:
        if c in anchors or LABEL.fullmatch(c['content']):
            continue
        # Only MinerU-designated captions can be continuations. Do not absorb
        # unrelated body paragraphs just because they share a horizontal row.
        if not c['nested_caption']:
            continue
        matches = []
        for i, g in enumerate(groups):
            a = g['anchor']['bbox']
            if abs(a[1]-c['bbox'][1]) <= .025 and gap(a, c['bbox']) <= .08:
                matches.append((gap(a, c['bbox']), i))
        matches.sort()
        if matches:
            groups[matches[0][1]]['caption_parts'].append(c)
            if len(matches) > 1:
                groups[matches[0][1]]['review_reasons'].append('ambiguous_caption_continuation')
        else:
            unattached.append(c)
    for g in groups:
        g['caption_bbox'] = union([c['bbox'] for c in g['caption_parts']])

    # Graph edges connect nearby visual blocks. A real caption or substantive
    # body text between two blocks prevents merging across it.
    barriers = [g['caption_bbox'] for g in groups]
    barriers += [b['bbox'] for b in blocks if b['type'] in ('text', 'paragraph_title')
                 and len(str(b.get('content', ''))) > 35 and not ANCHOR.match(str(b.get('content', '')))]

    def blocked(a, b):
        box = union([a, b])
        return any(overlap(box, t) > .015 and overlap(box, t, 1) > .008
                   and overlap(a, t, 1) < .008 and overlap(b, t, 1) < .008
                   for t in barriers)

    edges = [[] for _ in panels]
    for i, p in enumerate(panels):
        for j in range(i):
            a, b = p['bbox'], panels[j]['bbox']
            distance = gap(a, b)
            if distance <= .08 and not blocked(a, b):
                edges[i].append((j, distance+.005))
                edges[j].append((i, distance+.005))

    distances = []
    for g in groups:
        c = g['caption_bbox']
        dist = [math.inf] * len(panels)
        queue = []
        for i, p in enumerate(panels):
            b = p['bbox']
            # A seed must be above and horizontally intersect its caption.
            if -.008 <= c[1]-b[3] <= .10 and overlap(b, c) > .02 and not blocked(b, c):
                dist[i] = max(0, c[1]-b[3])
                heapq.heappush(queue, (dist[i], i))
        while queue:
            d, i = heapq.heappop(queue)
            if d != dist[i]:
                continue
            for j, weight in edges[i]:
                if d+weight < dist[j]:
                    dist[j] = d+weight
                    heapq.heappush(queue, (dist[j], j))
        distances.append(dist)

    assigned = [[] for _ in groups]
    unassigned = []
    for i, p in enumerate(panels):
        choices = sorted((d[i], k) for k, d in enumerate(distances) if math.isfinite(d[i]))
        if not choices:
            unassigned.append(p)
            continue
        assigned[choices[0][1]].append(p)
        if len(choices) > 1:
            # Even a clear nearest winner cannot establish figure identity.
            for _, k in choices:
                groups[k]['review_reasons'].append('connected_to_multiple_figure_captions')

    results = []
    for g, members in zip(groups, assigned):
        if not members:
            g['review_reasons'].append('no_visual_blocks_above_caption')
            crop = None
        else:
            boxes = [p['bbox'] for p in members]
            # Subfigure labels may have been misclassified as captions/text.
            for c in captions:
                if LABEL.fullmatch(c['content']) and any(gap(c['bbox'], p['bbox']) <= .025 for p in members):
                    if c['bbox'][3] <= g['caption_bbox'][1]+.005:
                        boxes.append(c['bbox'])
            crop = union(boxes)
            crop = [max(0, crop[0]-.008), max(0, crop[1]-.006),
                    min(1, crop[2]+.008), min(g['caption_bbox'][1]-.002, crop[3]+.003)]
            if crop[3] <= crop[1]:
                crop = None
                g['review_reasons'].append('invalid_crop')
            elif any(overlap(crop, b) > .02 and overlap(crop, b, 1) > .01 for b in barriers[len(groups):]):
                g['review_reasons'].append('crop_overlaps_body_text')
        reasons = sorted(set(g['review_reasons']))
        results.append({'number': g['number'], 'page': page['page_idx']+1,
                        'caption': ' '.join(c['content'] for c in sorted(g['caption_parts'], key=lambda c: (c['bbox'][1], c['bbox'][0]))),
                        'caption_bbox': g['caption_bbox'], 'crop_bbox': crop,
                        'panel_count': len(members),
                        'source_blocks': [p['source_index'] for p in members],
                        'review_required': bool(reasons), 'review_reasons': reasons})
    diagnostics = {'page': page['page_idx']+1, 'unassigned_visuals': unassigned,
                   'unattached_caption_parts': unattached}
    return results, diagnostics


def run(pdf_path, input_path, output):
    import pypdfium2 as pdfium
    from PIL import Image, ImageDraw
    data = json.loads(input_path.read_text())
    output.mkdir(parents=True, exist_ok=True)
    (output/'figures').mkdir(exist_ok=True)
    figures, diagnostics = [], []
    pdf = pdfium.PdfDocument(pdf_path)
    for page in data['pages']:
        groups, diag = group_page(page)
        if diag['unassigned_visuals'] or diag['unattached_caption_parts']:
            diagnostics.append(diag)
        if not groups:
            continue
        pdf_page = pdf[page['page_idx']]
        rendered = pdf_page.render(scale=2.5).to_pil().convert('RGB')
        overlay = rendered.copy()
        draw = ImageDraw.Draw(overlay)
        w, h = rendered.size
        for g in groups:
            if g['crop_bbox']:
                x0,y0,x1,y1 = g['crop_bbox']
                box = (round(x0*w), round(y0*h), round(x1*w), round(y1*h))
                path = f"figures/page_{g['page']}_figure_{g['number']}.png"
                rendered.crop(box).save(output/path)
                g['image'] = path
                color = 'orange' if g['review_required'] else 'green'
                draw.rectangle(box, outline=color, width=5)
                draw.text((box[0], max(0,box[1]-18)), 'Figure '+g['number'], fill=color)
            caption_path = f"figures/page_{g['page']}_figure_{g['number']}_caption.txt"
            (output/caption_path).write_text(g['caption']+'\n')
        overlay.save(output/f"page_{page['page_idx']+1}_overlay.jpg")
        pdf_page.close()
        figures.extend(groups)
    pdf.close()
    numbers = [f['number'] for f in figures]
    for f in figures:
        if numbers.count(f['number']) > 1:
            f['review_required'] = True
            f['review_reasons'].append('duplicate_figure_number_across_document')
    report = {'strategy': 'caption_anchors_and_geometry', 'input': str(input_path),
              'figures': figures, 'page_diagnostics': diagnostics,
              'limitations': ['Captions above figures and cross-page figures require review.',
                              'No review flag is a heuristic result, not a correctness guarantee.']}
    (output/'groups.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    lines = ['# 自动 Figure 分组结果', '',
             '根据图注编号、坐标邻接、正文边界自动分组；没有预设本论文页码或图片数量。',
             '绿色边框表示未触发复核规则，橙色表示需复核；无标记不等于保证正确。',
             '适用于图注在图下方的页面，跨页图、上置图注和复杂布局需要进一步处理。', '',
             '[分组与诊断 JSON](groups.json)', '',
             f'共发现 {len(figures)} 个 Figure，合并 {sum(f["panel_count"] for f in figures)} 个图片/图表块。', '']
    for f in figures:
        lines += [f"## Figure {f['number']} — 第 {f['page']} 页", '',
                  f"合并 {f['panel_count']} 个块；需复核：{f['review_required']}；原因：{', '.join(f['review_reasons']) or '未触发规则'}", '']
        if f.get('image'):
            lines += [f"![Figure {f['number']}]({f['image']})", '']
        lines += [f['caption'], '', f"[页面边框预览](page_{f['page']}_overlay.jpg)", '']
    if diagnostics:
        lines += ['## 待处理页面', '', '存在未分配图片或未关联图注，详见 groups.json。', '']
    (output/'README.md').write_text('\n'.join(lines))
    print(json.dumps({'figures': len(figures), 'panels': sum(f['panel_count'] for f in figures),
                      'review_required': sum(f['review_required'] for f in figures),
                      'pages_with_unassigned_items': len(diagnostics)}, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pdf', required=True, type=Path)
    parser.add_argument('--input', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    run(args.pdf, args.input, args.output)
