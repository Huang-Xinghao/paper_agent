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

    def field(pattern):
        # The refine skill uses labelled bullets under 核心机制, not a UI schema.
        scope = section(r'^核心机制$') or section(r'^(?:提议的方法|方法设计|研究方法)$') or source
        values = []
        active = False
        fence = None
        for line in scope.splitlines():
            marker = re.match(r'^\s*(`{3,}|~{3,})', line)
            if marker:
                if fence is None:
                    fence = marker[1][0]
                elif marker[1][0] == fence:
                    fence = None
                if active:
                    values.append(line)
                continue
            if fence is None:
                label = re.match(r'^\s*(?:[-*+]\s+|\d+[.、)]\s+)?([^：:\n]+)[：:]\s*(.*)$', line)
                if label:
                    active = bool(re.fullmatch(pattern, label[1].strip().strip('*'), re.I))
                    if active:
                        values.append(label[2].removeprefix('**').strip())
                    continue
                if re.match(r'^\s*(?:#{1,6}\s|[-*+]\s)', line):
                    active = False
            if active:
                values.append(line)
        return '\n'.join(values).strip()

    diagram_source = section(r'^(?:模型架构|系统概述|架构图|系统架构)$') or source
    diagram = next((body.strip() for language, body in re.findall(
        r'```([^\n]*)\n(.*?)```', diagram_source, re.S)
        if language.strip() in {'', 'text', 'plaintext', 'ascii'} and any(c in body for c in ['→', '↓', '->', '├', '▼'])), '')
    architecture = (section(r'^模型架构$') or section(r'表示与精确架构|网络架构|模型结构|核心架构|^架构(?:[、，,:：]|$)')
                    or field(r'(?:模型|网络|系统)?架构') or section(r'^系统概述$'))
    inputs = section(r'^(?:模型输入|输入表示|输入格式|输入特征及格式|输入)$') or field(r'(?:模型)?输入(?:特征及格式|格式)?')
    outputs = section(r'^(?:模型输出|输出与推理|输出标签及格式|输出格式|输出)$') or field(r'(?:模型)?输出(?:标签及格式|格式)?')
    # Older proposals combine input/target definitions and put tensor shapes in
    # the architecture. Reuse those actual paragraphs rather than invent a model.
    combined = section(r'输入[/／]输出|输入与输出') or field(r'输入[/／]输出|输入与输出')
    if not inputs:
        inputs = '\n\n'.join(p for p in architecture.split('\n\n')
                              if not p.lstrip().startswith('|') and re.search(r'输入|通道|插值|单电芯|批量', p)) or combined
    if not outputs:
        outputs = '\n\n'.join(p for p in combined.split('\n\n')
                               if re.search(r'输出|预测|RUL|\\widehat|ŷ|y\s*=', p)) or combined
    return {'diagram': diagram, 'input': inputs, 'architecture': architecture, 'output': outputs}
