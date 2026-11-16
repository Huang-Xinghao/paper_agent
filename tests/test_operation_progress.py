"""Slow tools expose real stages and stop advertising completed work."""
import json
from pathlib import Path
from types import SimpleNamespace
import zipfile

from paper_agent.integrations.progress import live_operations
from paper_agent.tools import paper_figures


def test_pdf_parse_reports_real_stages_and_reuses_complete_cache(tmp_path, monkeypatch):
    pdf = tmp_path / 'paper.pdf'
    pdf.write_bytes(b'%PDF local fixture')
    monkeypatch.setattr(paper_figures, 'runtime_python', lambda: '/parser/python')
    calls, events = [], []

    def run(command, **kwargs):
        calls.append(command)
        if command[1] == '-c':
            return SimpleNamespace(stdout='1.0')
        if command[1] == 'parse':
            assert live_operations(tmp_path)[0]['stage'] == 'parsing_pdf'
            output = Path(command[command.index('-o') + 1])
            with zipfile.ZipFile(output, 'w') as bundle:
                bundle.writestr('structured_content.json', json.dumps({'pages': [
                    {'page_idx': 0, 'blocks': [{'type': 'text', 'content': 'Original paper evidence.'}]}]}))
        else:
            assert live_operations(tmp_path)[0]['stage'] == 'grouping_figures'
            output = Path(command[command.index('--output') + 1])
            output.mkdir()
            (output / 'groups.json').write_text(json.dumps({'figures': [], 'page_diagnostics': []}))
        return SimpleNamespace(stdout='')

    monkeypatch.setattr(paper_figures.subprocess, 'run', run)
    first = paper_figures.read_mineru_paper(tmp_path, pdf,
        on_progress=lambda action, details: events.append(dict(details)))
    assert first['status'] == 'completed'
    assert {'parsing_pdf', 'reading_structure', 'grouping_figures'} <= {e['stage'] for e in events}
    assert not live_operations(tmp_path)
    events.clear()
    second = paper_figures.read_mineru_paper(tmp_path, pdf,
        on_progress=lambda action, details: events.append(dict(details)))
    assert first['document'] == second['document']
    assert 'using_cached_parse' in {e['stage'] for e in events}
    assert len([c for c in calls if c[1] == 'parse']) == 1
