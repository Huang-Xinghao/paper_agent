"""Projects share original PDFs without gaining general write access to the Wiki."""
import io
import json

import pytest
from pypdf import PdfWriter

from paper_agent.tools.core import ToolContext
from paper_agent.tools import literature


@pytest.fixture
def library(tmp_path, monkeypatch):
    wiki = tmp_path / 'wiki'
    raw = wiki / 'raw-paper'
    raw.mkdir(parents=True)
    monkeypatch.setenv('PAPER_AGENT_WIKI_ROOT', str(wiki))
    project = tmp_path / 'project'
    project.mkdir()
    stream = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.write(stream)
    return ToolContext(project), raw, stream.getvalue()


def test_independent_project_discovers_and_reads_shared_pdf(library):
    ctx, raw, content = library
    pdf = raw / 'battery-lifetime.pdf'
    pdf.write_bytes(content)
    (raw / 'aaa_unrelated.pdf').write_bytes(content)
    discovery = json.loads(literature.discover_local_papers(ctx, query='battery').output)
    assert discovery['found'] == 2
    path = discovery['papers'][0]['path']
    assert path == str(pdf)
    assert json.loads(literature.read_local_paper(ctx, path).output)['page_count'] == 1
    assert json.loads(literature.read_paper_pages(ctx, path).output)['total_pages'] == 1
    with pytest.raises(ValueError, match='escapes workspace'):
        ctx.resolve(pdf, write=True)


@pytest.mark.parametrize('provider', ['arxiv', 'openalex'])
def test_download_cache_is_shared_across_projects(library, monkeypatch, provider):
    ctx, raw, content = library
    requests = []

    def download(request, timeout):
        requests.append(request.full_url)
        return io.BytesIO(content)

    monkeypatch.setattr(literature.urllib.request, 'urlopen', download)
    if provider == 'arxiv':
        call = lambda context: literature.download_arxiv_paper(context, 'https://arxiv.org/abs/2601.12345')
    else:
        call = lambda context: literature.download_openalex_paper(context, {
            'doi': '10.1234/paper', 'pdf_urls': ['https://example.org/paper.pdf']})
    first = json.loads(call(ctx).output)
    other_project = ctx.workspace.parent / 'other-project'
    other_project.mkdir()
    second = json.loads(call(ToolContext(other_project)).output)
    assert first['status'] == 'downloaded' and second['status'] == 'cached'
    assert first['path'] == second['path']
    assert len(requests) == 1
    assert list(raw.glob('*.pdf'))[0].read_bytes() == content
    assert not (ctx.workspace / 'papers').exists()


def test_library_symlinks_cannot_escape_read_or_download_boundary(library):
    ctx, raw, content = library
    outside = raw.parent.parent / 'private.pdf'
    outside.write_bytes(content)
    (raw / 'arxiv-2601.12345.pdf').symlink_to(outside)
    assert json.loads(literature.discover_local_papers(ctx).output)['found'] == 0
    with pytest.raises(ValueError, match='outside workspace'):
        literature.read_local_paper(ctx, str(raw / 'arxiv-2601.12345.pdf'))
    with pytest.raises(ValueError, match='inside the shared library'):
        literature.download_arxiv_paper(ctx, 'https://arxiv.org/abs/2601.12345')
    assert outside.read_bytes() == content
