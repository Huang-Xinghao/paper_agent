import asyncio
import json

from paper_agent.tools import ToolContext, invoke, builtin_tools, tools_for_phase


def pair(root, slug, name, caption):
    directory = root/'figures'/slug
    directory.mkdir(parents=True, exist_ok=True)
    (directory/(name+'.png')).write_bytes(b'image')
    (directory/(name+'.md')).write_text(
        f'# Figure 2\n\nSource: paper:{slug}\nPDF page: 2\n\n{caption}\n')


def search(project, query, **kwargs):
    return asyncio.run(invoke('search_figure_references', ToolContext(project), query=query, **kwargs))


def test_bilingual_relevance_full_caption_and_shared_paths(tmp_path, monkeypatch):
    root = tmp_path/'shared/research-wiki'
    monkeypatch.setenv('PAPER_AGENT_WIKI_ROOT', str(root))
    pair(root, 'informer', 'fig2', 'Informer architecture. Encoder and decoder with attention.')
    pair(root, 'imoe', 'fig1', 'Battery degradation prediction with mixture of experts.')
    project = tmp_path/'project'
    project.mkdir()
    result = json.loads(search(project, '编码器 解码器 注意力', top_k=1).output)
    assert result['library_size'] == 2
    match = result['matches'][0]
    assert match['paper_id'] == 'paper:informer'
    assert match['pdf_page'] == 2
    assert match['caption'] == 'Informer architecture. Encoder and decoder with attention.'
    assert match['image_path'] == str(root/'figures/informer/fig2.png')
    assert 'Source: paper:informer' in match['caption_markdown']
    assert match['score'] > 0
    result = json.loads(search(project, '电池 多专家 退化').output)
    assert result['matches'][0]['paper_id'] == 'paper:imoe'


def test_empty_library_no_match_limits_and_orphan_caption(tmp_path, monkeypatch):
    root = tmp_path/'research-wiki'
    monkeypatch.setenv('PAPER_AGENT_WIKI_ROOT', str(root))
    assert json.loads(search(tmp_path, 'encoder').output)['matches'] == []
    pair(root, 'paper', 'fig', 'Encoder architecture.')
    (root/'figures/paper/orphan.md').write_text('Decoder architecture.')
    assert json.loads(search(tmp_path, 'chemistry').output)['matches'] == []
    assert json.loads(search(tmp_path, 'encoder').output)['library_size'] == 1
    assert search(tmp_path, '   ').is_error
    assert search(tmp_path, 'encoder', top_k=0).is_error
    assert search(tmp_path, 'encoder', top_k=21).is_error


def test_symlinks_cannot_read_outside_library(tmp_path, monkeypatch):
    root = tmp_path/'research-wiki'
    monkeypatch.setenv('PAPER_AGENT_WIKI_ROOT', str(root))
    pair(root, 'paper', 'fig', 'Architecture encoder.')
    external = tmp_path/'secret.md'
    external.write_text('Secret encoder')
    (root/'figures/paper/fig.md').unlink()
    (root/'figures/paper/fig.md').symlink_to(external)
    assert json.loads(search(tmp_path, 'encoder').output)['matches'] == []
    (root/'figures/paper/fig.md').unlink()
    (root/'figures/paper/fig.md').write_text('Encoder architecture')
    (root/'figures/paper/fig.png').unlink()
    (root/'figures/paper/fig.png').symlink_to(external)
    assert json.loads(search(tmp_path, 'encoder').output)['matches'] == []


def test_tool_is_readonly_and_visible_to_figure_phases():
    spec = next(s for s in builtin_tools() if s.name == 'search_figure_references')
    assert spec.read_only
    for phase in ('paper_figure',):
        assert spec in tools_for_phase(phase, builtin_tools())
        names = {s.name for s in tools_for_phase(phase, builtin_tools())}
        assert {'view_image', 'read_battery_resource'} <= names


def test_retrieved_shared_pair_can_be_read_and_viewed(tmp_path, monkeypatch):
    from PIL import Image
    root = tmp_path/'shared/research-wiki'
    monkeypatch.setenv('PAPER_AGENT_WIKI_ROOT', str(root))
    pair(root, 'informer', 'fig2', 'Encoder architecture with attention.')
    image_path = root/'figures/informer/fig2.png'
    Image.new('RGB', (64, 64), 'white').save(image_path)
    project = tmp_path/'project'
    project.mkdir()
    match = json.loads(search(project, 'encoder').output)['matches'][0]
    ctx = ToolContext(project)
    caption = asyncio.run(invoke('read', ctx, path=match['caption_path']))
    assert not caption.is_error
    assert 'Encoder architecture' in caption.output
    viewed = asyncio.run(invoke('view_image', ctx, path=match['image_path']))
    assert not viewed.is_error
    assert viewed.images
    # The read grant is restricted to figures, even when another Wiki file exists.
    private = root/'private.md'
    private.write_text('unrelated')
    assert asyncio.run(invoke('read', ctx, path=str(private))).is_error
    outside = tmp_path/'outside.png'
    Image.new('RGB', (64, 64)).save(outside)
    image_path.unlink()
    image_path.symlink_to(outside)
    assert asyncio.run(invoke('view_image', ctx, path=str(image_path))).is_error
