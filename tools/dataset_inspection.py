"""Bounded structural inspection of explicitly registered, trusted local data."""
import csv
import json
import math
import pickle
import subprocess
import sys
from pathlib import Path

from .core import ToolContext, ToolResult, tool
from .data_access import registered_data_sources


def summary(value, depth=0, budget=None):
    if budget is None:
        budget = [250]
    budget[0] -= 1
    if budget[0] < 0:
        return {"truncated": True}
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, str):
        return value[:300]
    if hasattr(value, 'shape') and hasattr(value, 'dtype'):
        return {'type': type(value).__name__, 'shape': list(value.shape), 'dtype': str(value.dtype)}
    if depth >= 5:
        return {'type': type(value).__name__, 'length': len(value) if hasattr(value, '__len__') else None}
    if isinstance(value, dict):
        keys = list(value)
        return {'type': 'dict', 'count': len(keys),
                'fields': {str(k): summary(value[k], depth + 1, budget) for k in keys[:40]}}
    if isinstance(value, (list, tuple)):
        indices = sorted({0, len(value) - 1}) if value else []
        return {'type': type(value).__name__, 'length': len(value),
                'samples': {str(i): summary(value[i], depth + 1, budget) for i in indices}}
    return {'type': type(value).__name__}


@tool(read_only=True)
def list_battery_data_sources(ctx: ToolContext) -> ToolResult:
    """List read-only dataset paths configured in battery resource README local-data fields."""
    return ToolResult(json.dumps({'sources': registered_data_sources(ctx.knowledge_roots)}, ensure_ascii=False))


@tool(read_only=True)
def inspect_battery_data(ctx: ToolContext, path: str) -> ToolResult:
    """Inspect real JSON/CSV/PKL fields, counts and bounded samples (not a full scientific audit).

    PKL files must come from trusted datasets explicitly registered by the user in
    the resource library. Inspection uses this runner's Python environment.
    """
    target = ctx.resolve_read(path)
    if not target.is_file():
        return ToolResult(f'Data file not found: {path}', True)
    if target.suffix.lower() not in {'.pkl', '.pickle', '.json', '.csv'}:
        return ToolResult('Supported data formats: PKL, JSON, CSV', True)
    try:
        result = subprocess.run([sys.executable, '-c',
                                 'from paper_agent.tools.dataset_inspection import main; main()', str(target)],
                                capture_output=True, text=True, timeout=30,
                                cwd=Path(__file__).resolve().parents[2])
    except subprocess.TimeoutExpired:
        return ToolResult('Data inspection exceeded 30 seconds; use a smaller file.', True)
    if result.returncode:
        return ToolResult('Data inspection failed (check data format and Python dependencies): ' + result.stderr[-3000:], True)
    return ToolResult(result.stdout)


def main():
    target = Path(sys.argv[1])
    if target.suffix.lower() in {'.pkl', '.pickle'}:
        with target.open('rb') as stream:
            value = pickle.load(stream)
    elif target.suffix.lower() == '.json':
        value = json.loads(target.read_text())
    else:
        with target.open(newline='', encoding='utf-8') as stream:
            rows = csv.DictReader(stream)
            sample = []
            count = 0
            for row in rows:
                count += 1
                if len(sample) < 2:
                    sample.append(row)
            value = {'row_count': count, 'sample_rows': sample}
    print(json.dumps({'path': str(target), 'bytes': target.stat().st_size,
                      'scope': 'structural sample; not a complete dataset audit',
                      'summary': summary(value)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
