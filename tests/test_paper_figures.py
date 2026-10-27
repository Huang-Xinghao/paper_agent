"""Architecture selection, publication, and fixed WikiAgent integration."""
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from paper_agent.tools.paper_figures import (
    architecture_reason, read_mineru_paper, save_architecture_figures,
)


def test_caption_selection_does_not_default_to_first_figure():
    assert architecture_reason('Fig. 4 | Overall architecture of the proposed model.')
    assert architecture_reason('Fig. 1 | Model motivation, model architecture, and deployment.')
    assert architecture_reason('Fig. 2. 模型结构与训练流程。')
    assert not architecture_reason('Fig. 1 | Dataset description. a Model architectures used for comparison.')
    assert not architecture_reason('Fig. 3 | Results and analysis. a Prediction results of the proposed framework.')


def extraction(tmp_path):
    cache = tmp_path / 'cache'
    (cache / 'figures').mkdir(parents=True)
    from PIL import Image
    Image.new('RGB', (30, 20), 'white').save(cache / 'figures/figure-4.png')
    return {'status': 'completed', 'cache': str(cache), 'total_figures': 5,
            'pdf_sha256': '123', 'figures': [
                {'number': '4', 'page': 7, 'caption': 'Fig. 4 | Model architecture.',
                 'image': 'figures/figure-4.png', 'review_required': True,
                 'review_reasons': ['ambiguous_layout'], 'selection_reason': 'architecture'}]}


def test_assets_original_caption_and_review_flag_survive_publication(tmp_path):
    data = extraction(tmp_path)
    root = tmp_path / 'research-wiki'
    outputs = save_architecture_figures(root, 'test-paper', data)
    assert all(Path(p).is_file() for p in outputs)
    directory = root / 'figures/test-paper'
    assert sorted(p.name for p in directory.iterdir()) == ['figure-4.md', 'figure-4.png']
    caption = (directory/'figure-4.md').read_text()
    assert 'PDF page: 7' in caption and 'ambiguous_layout' in caption
    assert data['figures'][0]['caption'] in caption
    assert 'Source: paper:test-paper' in caption
    assert save_architecture_figures(root, 'empty', {'figures': []}) == []


def test_missing_mineru_runtime_is_an_error(tmp_path, monkeypatch):
    monkeypatch.delenv('PAPER_AGENT_MINERU_PYTHON', raising=False)
    monkeypatch.setattr('paper_agent.tools.paper_figures.shutil.which', lambda _: None)
    with pytest.raises(RuntimeError, match='MinerU is required'):
        read_mineru_paper(tmp_path, tmp_path/'unopened.pdf')


def test_configured_parser_failure_is_not_cached(tmp_path, monkeypatch):
    monkeypatch.setattr('paper_agent.tools.paper_figures.runtime_python', lambda: '/fake/bin/python')
    pdf = tmp_path/'paper.pdf'
    pdf.write_bytes(b'%PDF-test')
    def run(cmd, **kwargs):
        if '-c' in cmd:
            return SimpleNamespace(stdout='4.0.12')
        raise subprocess.CalledProcessError(1, cmd, stderr='parse failed')
    monkeypatch.setattr('paper_agent.tools.paper_figures.subprocess.run', run)
    with pytest.raises(RuntimeError, match='parse failed'):
        read_mineru_paper(tmp_path, pdf)
    assert not list((tmp_path/'.paper-agent').rglob('groups.json'))


def test_wiki_writes_architecture_without_visual_model_call(tmp_path, monkeypatch):
    from test_wiki_agent import make_pdf, analysis
    from paper_agent.agent.wiki import WikiAgent
    from paper_agent.agent.model_client import ModelResponse
    pdf = make_pdf(tmp_path, monkeypatch)
    data = extraction(tmp_path) | {'pages': [{'page': 1, 'text': 'MinerU full text'}]}
    monkeypatch.setattr('paper_agent.agent.wiki.read_mineru_paper', lambda *args: data)
    class Model:
        calls = 0
        def complete(self, messages, tools):
            self.calls += 1
            assert all(isinstance(m['content'], str) for m in messages)
            return ModelResponse(content=json.dumps(analysis(datasets=[])))
    model = Model()
    state = WikiAgent(tmp_path, model_client=model).go('Read the paper', paper_path=pdf)
    note = (tmp_path/state.wiki_path).read_text()
    assert model.calls == 1
    assert '## Architecture Figures' not in note
    assert 'Fig. 4 | Model architecture.' not in note
    assert 'Fig. 4 | Model architecture.' in (tmp_path / state.figure_paths[1]).read_text()
    assert all((tmp_path/p).is_file() for p in state.figure_paths)


def test_viewer_embeds_local_assets_but_rejects_paths_outside_assets(tmp_path):
    from scripts.visualize_wiki import build
    from paper_agent.tools import research_wiki
    root = tmp_path/'research-wiki'
    research_wiki.init_wiki(str(root))
    path = research_wiki.ingest_paper(str(root), title='Test', authors=['Author'], year=2026)
    from PIL import Image
    assets = root/'assets/papers'/path.stem
    assets.mkdir(parents=True)
    Image.new('RGB', (30,20), 'white').save(assets/'figure.png')
    research_wiki.update_paper_sections(str(root), path.stem, {'Method':
        f'![Figure](../assets/papers/{path.stem}/figure.png)'})
    with path.open('a') as stream:
        stream.write('\n![secret](../../private.png)\n')
    (tmp_path/'private.png').write_bytes(b'private')
    data = build(root, root/'visualization.html')
    images = data['nodes'][0]['images']
    assert len(images) == 1
    assert next(iter(images.values())).startswith('data:image/png;base64,')


def test_pdf_hash_cache_avoids_reparsing_and_invalidates_changed_pdf(tmp_path, monkeypatch):
    import zipfile
    monkeypatch.setattr('paper_agent.tools.paper_figures.runtime_python', lambda: '/fake/bin/python')
    pdf = tmp_path/'paper.pdf'
    pdf.write_bytes(b'%PDF-original')
    parses = []
    def run(cmd, **kwargs):
        if '-c' in cmd:
            return SimpleNamespace(stdout='4.0.12')
        if 'parse' in cmd:
            parses.append(cmd)
            with zipfile.ZipFile(cmd[cmd.index('-o')+1], 'w') as bundle:
                bundle.writestr('nested/structured_content.json', json.dumps({'pages': [{'page_idx': 0, 'blocks': [{'type': 'text', 'content': 'Full paper'}]}]}))
        else:
            output = Path(cmd[cmd.index('--output')+1])
            output.mkdir()
            (output/'groups.json').write_text(json.dumps({'figures': [], 'page_diagnostics': []}))
        return SimpleNamespace(stdout='')
    monkeypatch.setattr('paper_agent.tools.paper_figures.subprocess.run', run)
    first = read_mineru_paper(tmp_path, pdf)
    assert read_mineru_paper(tmp_path, pdf)['cache'] == first['cache']
    assert len(parses) == 1
    assert first['pages'] == [{'page': 1, 'text': 'Full paper'}]
    pdf.write_bytes(b'%PDF-revised')
    assert read_mineru_paper(tmp_path, pdf)['cache'] != first['cache']
    assert len(parses) == 2


def test_mineru_fulltext_preserves_order_formulas_tables_nested_captions_and_blank_pages():
    from paper_agent.tools.paper_figures import pages_from_structured
    blocks = [
        {'type': 'paragraph_title', 'content': 'Method'},
        {'type': 'text', 'content': 'Body evidence'},
        {'type': 'equation', 'content': r'E = mc^2'},
        {'type': 'table', 'content': '<table><tr><td>0.92</td></tr></table>',
         'captions': [{'content': 'Table 1. Accuracy'}]},
        {'type': 'image', 'content': '', 'captions': [{'content': 'Fig. 2. Architecture'}],
         'footnotes': [{'content': 'Figure source note'}]},
    ]
    pages = pages_from_structured({'is_full_document': True, 'pages': [
        {'page_idx': 0, 'blocks': blocks}, {'page_idx': 1, 'blocks': []},
        {'page_idx': 2, 'blocks': [{'content': 'Last page'}]}]})
    assert pages == [
        {'page': 1, 'text': 'Method\n\nBody evidence\n\nE = mc^2\n\n'
         '<table><tr><td>0.92</td></tr></table>\n\nTable 1. Accuracy\n\n'
         'Fig. 2. Architecture\n\nFigure source note'},
        {'page': 2, 'text': ''}, {'page': 3, 'text': 'Last page'}]


@pytest.mark.parametrize('data', [
    {'is_full_document': False, 'pages': [{'page_idx': 0, 'blocks': []}]},
    {'pages': [{'page_idx': 0, 'blocks': []}, {'page_idx': 2, 'blocks': []}]},
    {'pages': [{'page_idx': 0, 'blocks': []}]},
    {'pages': [{'page_idx': 0, 'blocks': [{'content': 'First page'}]}],
     'metadata': {'document': {'page_count': 2}}},
])
def test_mineru_rejects_partial_missing_or_empty_fulltext(data):
    from paper_agent.tools.paper_figures import pages_from_structured
    with pytest.raises(ValueError):
        pages_from_structured(data)
