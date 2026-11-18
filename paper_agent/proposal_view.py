"""Read the model presentation directly from the current formal proposal."""
import re


def model_overview(source: str) -> dict:
    lines = source.splitlines()
    headings = []
    fence = None
    for index, line in enumerate(lines):
        marker = re.match(r'^\s*(`{3,}|~{3,})', line)
        if marker:
            if fence is None:
                fence = marker[1][0]
            elif marker[1][0] == fence:
                fence = None
            continue
        match = re.match(r'^(#{2,6})\s+(.+)', line) if fence is None else None
        if match:
            title = re.sub(r'^\d+[.、）)]\s*', '', match[2]).strip().strip('*').rstrip('#').strip()
            headings.append((index, len(match[1]), title))

    def section(pattern):
        for index, level, title in headings:
            if re.search(pattern, title, re.I):
                end = next((i for i, depth, _ in headings if i > index and depth <= level), len(lines))
                return '\n'.join(lines[index + 1:end]).strip()
        return ''

    diagram_source = section(r'^(?:模型架构|系统概述|架构图|系统架构)$') or source
    diagram = next((body.strip() for language, body in re.findall(
        r'```([^\n]*)\n(.*?)```', diagram_source, re.S)
        if language.strip() in {'', 'text', 'plaintext', 'ascii'} and any(c in body for c in ['→', '↓', '->', '├', '▼'])), '')
    architecture = section(r'^模型架构$') or section(r'表示与精确架构|网络架构|模型结构|核心架构')
    inputs = section(r'^模型输入$') or section(r'^(?:输入表示|输入格式)$')
    outputs = section(r'^模型输出$') or section(r'^输出与推理$')
    # Older proposals combine input/target definitions and put tensor shapes in
    # the architecture. Reuse those actual paragraphs rather than invent a model.
    combined = section(r'输入[/／]输出|输入与输出')
    if not inputs:
        inputs = '\n\n'.join(p for p in architecture.split('\n\n')
                              if not p.lstrip().startswith('|') and re.search(r'输入|通道|插值|单电芯|批量', p))
    if not outputs:
        outputs = '\n\n'.join(p for p in combined.split('\n\n')
                               if re.search(r'输出|预测|RUL|\\widehat|ŷ|y\s*=', p))
    return {'diagram': diagram, 'input': inputs, 'architecture': architecture, 'output': outputs}
