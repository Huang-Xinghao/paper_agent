import json

import httpx
import pytest

from paper_agent.agent.wiki import WikiAgent
from paper_agent.agent.model_client import ModelResponse
from paper_agent.tools.zotero import ZoteroClient
from test_wiki_agent import analysis


def transport(request):
    path = request.url.path
    if request.url.host == 'files.example.org':
        assert 'Zotero-API-Key' not in request.headers
        return httpx.Response(200, content=b'%PDF-1.4 fake paper')
    assert request.headers['Zotero-API-Key'] == 'test-secret'
    if path.endswith('/collections/COLL1234/items/top'):
        if request.url.params['start'] == '0':
            return httpx.Response(200, json=[{'key': 'ITEM1234', 'data': {'itemType': 'journalArticle'}},
                                            {'key': 'FAIL1234', 'data': {'itemType': 'journalArticle'}}])
    if path.endswith('/items/ITEM1234') or path.endswith('/items/FAIL1234'):
        return httpx.Response(200, json={'version': 2, 'data': {
            'itemType': 'journalArticle', 'title': 'Zotero authoritative title',
            'creators': [{'creatorType': 'author', 'firstName': 'Alice', 'lastName': 'Smith'}],
            'date': '2025-03-01', 'DOI': '10.1234/battery.2025'}})
    if path.endswith('/items/ITEM1234/children'):
        return httpx.Response(200, json=[{'key': 'PDF12345', 'data': {'itemType': 'attachment',
                              'contentType': 'application/pdf', 'linkMode': 'imported_file'}}])
    if path.endswith('/items/FAIL1234/children'):
        return httpx.Response(200, json=[])
    if path.endswith('/items/PDF12345/file'):
        return httpx.Response(302, headers={'Location': 'https://files.example.org/paper.pdf'})
    if path.endswith('/items/PDF12345/children'):
        return httpx.Response(200, json=[{'key': 'NOTE1234', 'data': {'itemType': 'annotation',
                      'annotationType': 'highlight', 'annotationText': 'Important result', 'annotationComment': 'My interest',
                      'annotationPageLabel': 'iii'}}])
    raise AssertionError(str(request.url))


def client():
    return ZoteroClient('123', api_key='test-secret', transport=httpx.MockTransport(transport))


def fake_reader(monkeypatch):
    monkeypatch.setattr('paper_agent.agent.wiki.read_mineru_paper', lambda *args: {
        'status': 'completed', 'figures': [],
        'pages': [{'page': 1, 'text': 'Paper methods and results'}]})


def test_download_metadata_annotations_and_secret_boundary(tmp_path):
    c = client()
    try:
        paper = c.prepare(tmp_path, 'ITEM1234')
        assert paper.pdf_path.read_bytes().startswith(b'%PDF-')
        assert paper.metadata['authors'] == ['Alice Smith']
        assert paper.metadata['year'] == 2025
        assert paper.annotations[0]['page_label'] == 'iii'
        assert paper.provenance['item_key'] == 'ITEM1234'
        assert 'test-secret' not in json.dumps(paper.provenance)
    finally:
        c.close()


def test_batch_writes_sources_skips_unchanged_and_retries_failures(tmp_path, monkeypatch):
    fake_reader(monkeypatch)
    class Model:
        calls = 0
        def complete(self, messages, tools):
            self.calls += 1
            payload = json.loads(messages[1]['content'])
            assert payload['user_annotations'][0]['text'] == 'Important result'
            assert payload['source_metadata']['title'] == 'Zotero authoritative title'
            return ModelResponse(content=json.dumps(analysis()))
    model = Model()
    agent = WikiAgent(tmp_path, model_client=model)
    c = client()
    try:
        result = agent.ingest_zotero(c, collection_key='COLL1234')
        assert (result['completed'], result['failed']) == (1, 1)
        path = tmp_path / result['items'][0]['wiki_path']
        assert 'Zotero authoritative title' in path.read_text()
        source = json.loads(path.with_suffix('.source.json').read_text())
        assert source['annotations'][0]['comment'] == 'My interest'
        repeat = agent.ingest_zotero(c, collection_key='COLL1234')
        assert (repeat['skipped'], repeat['failed']) == (1, 1)
        assert model.calls == 1
        path.unlink()
        assert agent.ingest_zotero(c, item_key='ITEM1234')['completed'] == 1
        assert model.calls == 2
        assert agent.ingest_zotero(c, item_key='ITEM1234', force=True)['completed'] == 1
        assert model.calls == 3
    finally:
        c.close()


def test_reanalysis_archives_superseded_experiments(tmp_path, monkeypatch):
    fake_reader(monkeypatch)
    class Model:
        changed = False
        def complete(self, messages, tools):
            result = analysis()
            if self.changed:
                result['experiments'] = [result['experiments'][0]]
            return ModelResponse(content=json.dumps(result))
    model = Model()
    agent = WikiAgent(tmp_path, model_client=model)
    c = client()
    try:
        agent.ingest_zotero(c, item_key='ITEM1234')
        assert len(list((tmp_path / 'research-wiki/experiments').glob('*.md'))) == 2
        model.changed = True
        result = agent.ingest_zotero(c, item_key='ITEM1234', force=True)
        assert result['completed'] == 1
        assert len(list((tmp_path / 'research-wiki/experiments').glob('*.md'))) == 1
        assert len(list((tmp_path / 'research-wiki/archive/zotero/ITEM1234').glob('*.md'))) == 1
    finally:
        c.close()


def test_pagination_and_validation():
    starts = []
    def handle(request):
        start = int(request.url.params['start'])
        starts.append(start)
        return httpx.Response(200, json=[{'key': 'ITEM1234', 'data': {'itemType': 'journalArticle'}}] * (100 if start == 0 else 1))
    c = ZoteroClient('123', transport=httpx.MockTransport(handle))
    try:
        assert len(c.collection_items('COLL1234')) == 101
        assert starts == [0, 100]
        with pytest.raises(ValueError):
            c.key('../bad')
    finally:
        c.close()


def test_bad_download_does_not_write_pdf(tmp_path):
    def handle(request):
        if request.url.path.endswith('/file'):
            return httpx.Response(200, content=b'<html>Login</html>')
        return transport(request)
    c = ZoteroClient('123', api_key='test-secret', transport=httpx.MockTransport(handle))
    try:
        with pytest.raises(ValueError, match='not a PDF'):
            c.prepare(tmp_path, 'ITEM1234')
        assert not list(tmp_path.rglob('*.pdf'))
    finally:
        c.close()


def test_archive_does_not_appear_in_active_wiki_search(tmp_path, monkeypatch):
    monkeypatch.setenv("PAPER_AGENT_WIKI_ROOT", str(tmp_path / "research-wiki"))
    from paper_agent.tools import ToolContext
    from paper_agent.tools.wiki import search_wiki
    for folder in ('papers', 'archive/zotero/ITEM1234'):
        path = tmp_path / 'research-wiki' / folder
        path.mkdir(parents=True)
        (path / 'paper.md').write_text('battery result', encoding='utf-8')
    result = json.loads(search_wiki(ToolContext(tmp_path), query='battery').output)
    assert result['count'] == 1
    assert result['matches'][0]['entity_type'] == 'papers'


@pytest.fixture
def local_library(tmp_path):
    import sqlite3
    data_dir = tmp_path / 'Zotero'
    data_dir.mkdir()
    db = sqlite3.connect(data_dir / 'zotero.sqlite')
    db.executescript('''
        CREATE TABLE libraries (libraryID INTEGER, type TEXT);
        CREATE TABLE items (itemID INTEGER, libraryID INTEGER, key TEXT, version INTEGER, itemTypeID INTEGER);
        CREATE TABLE itemTypesCombined (itemTypeID INTEGER, typeName TEXT);
        CREATE TABLE fieldsCombined (fieldID INTEGER, fieldName TEXT);
        CREATE TABLE itemData (itemID INTEGER, fieldID INTEGER, valueID INTEGER);
        CREATE TABLE itemDataValues (valueID INTEGER, value TEXT);
        CREATE TABLE creators (creatorID INTEGER, firstName TEXT, lastName TEXT);
        CREATE TABLE creatorTypes (creatorTypeID INTEGER, creatorType TEXT);
        CREATE TABLE itemCreators (itemID INTEGER, creatorID INTEGER, creatorTypeID INTEGER, orderIndex INTEGER);
        CREATE TABLE itemAttachments (itemID INTEGER, parentItemID INTEGER, contentType TEXT, linkMode INTEGER, path TEXT);
        CREATE TABLE itemAnnotations (itemID INTEGER, parentItemID INTEGER, type INTEGER, text TEXT, comment TEXT, pageLabel TEXT, position TEXT);
        CREATE TABLE collections (collectionID INTEGER, libraryID INTEGER, key TEXT);
        CREATE TABLE collectionItems (collectionID INTEGER, itemID INTEGER);
        CREATE TABLE deletedItems (itemID INTEGER);
        INSERT INTO libraries VALUES (1, 'user'), (2, 'group');
        INSERT INTO itemTypesCombined VALUES (1, 'journalArticle'), (2, 'attachment'), (3, 'annotation');
        INSERT INTO items VALUES (1, 1, 'ITEM1234', 2, 1), (2, 1, 'PDF12345', 1, 2),
            (3, 1, 'NOTE1234', 1, 3), (4, 1, 'DEAD1234', 1, 1), (5, 2, 'GRUP1234', 1, 1);
        INSERT INTO deletedItems VALUES (4);
        INSERT INTO fieldsCombined VALUES (1, 'title'), (2, 'date'), (3, 'DOI');
        INSERT INTO itemDataValues VALUES (1, 'Zotero authoritative title'), (2, '2025-03-01'), (3, '10.1234/battery.2025');
        INSERT INTO itemData VALUES (1, 1, 1), (1, 2, 2), (1, 3, 3);
        INSERT INTO creators VALUES (1, 'Alice', 'Smith');
        INSERT INTO creatorTypes VALUES (1, 'author');
        INSERT INTO itemCreators VALUES (1, 1, 1, 0);
        INSERT INTO itemAttachments VALUES (2, 1, 'application/pdf', 0, 'storage:paper.pdf');
        INSERT INTO itemAnnotations VALUES (3, 2, 1, 'Important result', 'My interest', 'iii', '');
        INSERT INTO collections VALUES (1, 1, 'COLL1234');
        INSERT INTO collectionItems VALUES (1, 1), (1, 4), (1, 5);
    ''')
    db.commit()
    db.close()
    pdf = data_dir / 'storage/PDF12345/paper.pdf'
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b'%PDF-1.4 fake paper')
    return data_dir


def test_local_and_web_share_preparation_and_identity(tmp_path, local_library, monkeypatch):
    from paper_agent.tools.zotero import LocalZoteroClient
    original = (local_library / 'zotero.sqlite').read_bytes()
    local = LocalZoteroClient('123', local_library)
    web = client()
    try:
        source = local.prepare(tmp_path / 'project', 'ITEM1234')
        remote = web.prepare(tmp_path / 'project', 'ITEM1234')
        assert source.metadata == remote.metadata
        assert source.annotations == remote.annotations
        assert source.fingerprint == remote.fingerprint
        assert source.pdf_path == remote.pdf_path
        assert [r['key'] for r in local.collection_items('COLL1234')] == ['ITEM1234']
        with pytest.raises(ValueError, match='not found'):
            local.item('GRUP1234')
        with pytest.raises(ValueError, match='not found'):
            local.item('DEAD1234')
    finally:
        local.close()
        web.close()
    assert (local_library / 'zotero.sqlite').read_bytes() == original


def test_local_import_uses_existing_wiki_pipeline_without_http(tmp_path, local_library, monkeypatch):
    from paper_agent.tools.zotero import LocalZoteroClient
    fake_reader(monkeypatch)
    def no_http(*args, **kwargs):
        raise AssertionError('Local import must not use HTTP')
    monkeypatch.setattr(httpx.Client, 'get', no_http)
    class Model:
        calls = 0
        def complete(self, messages, tools):
            self.calls += 1
            assert json.loads(messages[1]['content'])['user_annotations'][0]['comment'] == 'My interest'
            return ModelResponse(content=json.dumps(analysis()))
    model = Model()
    agent = WikiAgent(tmp_path / 'project', model_client=model)
    c = LocalZoteroClient('123', local_library)
    try:
        result = agent.ingest_zotero(c, collection_key='COLL1234')
        assert result['completed'] == 1
        assert agent.ingest_zotero(c, item_key='ITEM1234')['skipped'] == 1
        assert model.calls == 1
    finally:
        c.close()


def test_local_missing_and_absolute_linked_pdf(tmp_path, local_library):
    import sqlite3
    from paper_agent.tools.zotero import LocalZoteroClient
    pdf = local_library / 'storage/PDF12345/paper.pdf'
    pdf.unlink()
    c = LocalZoteroClient('123', local_library)
    try:
        with pytest.raises(FileNotFoundError):
            c.prepare(tmp_path / 'project', 'ITEM1234')
    finally:
        c.close()
    linked = tmp_path / 'linked.pdf'
    linked.write_bytes(b'%PDF-1.4 linked local file')
    db = sqlite3.connect(local_library / 'zotero.sqlite')
    db.execute('UPDATE itemAttachments SET linkMode=2, path=?', (str(linked),))
    db.commit()
    db.close()
    c = LocalZoteroClient('123', local_library)
    try:
        assert c.prepare(tmp_path / 'project', 'ITEM1234').pdf_path.read_bytes() == linked.read_bytes()
    finally:
        c.close()


def test_local_snapshot_includes_committed_wal(local_library):
    import sqlite3
    from paper_agent.tools.zotero import LocalZoteroClient
    db = sqlite3.connect(local_library / 'zotero.sqlite')
    db.execute('PRAGMA journal_mode=WAL')
    db.execute("UPDATE itemDataValues SET value='Updated local title' WHERE valueID=1")
    db.commit()
    c = LocalZoteroClient('123', local_library)
    try:
        assert c.item('ITEM1234')['data']['title'] == 'Updated local title'
    finally:
        c.close()
        db.close()
