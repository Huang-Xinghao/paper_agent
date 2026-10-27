import importlib

import pytest
from fastapi.testclient import TestClient

from paper_agent.api import auth

app = importlib.import_module("paper_agent.api.app").app


@pytest.fixture
def libraries(tmp_path, monkeypatch):
    wiki = tmp_path / "wiki"
    battery = tmp_path / "battery"
    for root in (wiki, battery):
        root.mkdir()
    (wiki / "papers").mkdir()
    (wiki / "index.md").write_text("# 范式索引\n\n研究资料")
    (wiki / "papers/example.md").write_text('---\ntitle: "metadata"\n---\n\n# Battery Transformer\n\n跨电芯泛化证据。')
    (battery / "电芯").mkdir()
    (battery / "资源库总览.md").write_text("# 电池资源总览\n\n[数据集](电芯/Tongji.md)")
    (battery / "电芯/Tongji.md").write_text("# Tongji 数据集\n\n寿命标签与容量。")
    (battery / "电芯/test.png").write_bytes(b"image")
    monkeypatch.setenv("PAPER_AGENT_WEB_DATA", str(tmp_path / "management"))
    monkeypatch.setenv("PAPER_AGENT_PROJECT_ROOTS", str(tmp_path / "projects"))
    monkeypatch.setenv("PAPER_AGENT_WIKI_ROOT", str(wiki))
    monkeypatch.setenv("PAPER_AGENT_BATTERY_ROOT", str(battery))
    with TestClient(app) as client:
        yield client, wiki, battery


def test_catalog_search_categories_and_read(libraries):
    client, wiki, battery = libraries
    result = client.get("/api/libraries/paradigms").json()
    assert result["total"] == 2 and result["available"]
    assert {item["id"] for item in result["categories"]} == {"overview", "papers"}
    filtered = client.get("/api/libraries/paradigms", params={"q": "跨电芯", "category": "papers"}).json()
    assert filtered["matched"] == 1 and filtered["total"] == 2
    assert filtered["items"][0]["title"] == "Battery Transformer"
    assert client.get("/api/libraries/paradigms", params={"q": "TRANSFORMER"}).json()["matched"] == 1
    assert client.get("/api/libraries/paradigms", params={"q": "不存在"}).json()["items"] == []
    read = client.get("/api/libraries/paradigms/document", params={"path": "papers/example.md"})
    assert read.status_code == 200
    assert read.json()["content"].startswith("# Battery Transformer")
    assert str(wiki) not in read.text
    assert client.get("/api/libraries/battery").json()["total"] == 2
    assert client.get("/api/libraries/battery/asset", params={"path": "电芯/test.png"}).content == b"image"
    assert (battery / "电芯/Tongji.md").read_text() == "# Tongji 数据集\n\n寿命标签与容量。"


def test_library_boundaries_and_hidden_files(libraries, tmp_path):
    client, wiki, battery = libraries
    outside = tmp_path / "private.md"
    outside.write_text("private")
    (wiki / "escape.md").symlink_to(outside)
    (wiki / "hidden-dir").symlink_to(battery, target_is_directory=True)
    (wiki / ".secret.md").write_text("private")
    (wiki / ".git").mkdir()
    (wiki / ".git/config.md").write_text("private")
    (wiki / "tool.py").write_text("private")
    listed = client.get("/api/libraries/paradigms").json()
    assert listed["total"] == 2
    for path in ["../private.md", str(outside), ".secret.md", ".git/config.md", "escape.md", "hidden-dir/资源库总览.md", "tool.py", "missing.md"]:
        assert client.get("/api/libraries/paradigms/document", params={"path": path}).status_code in {400, 404}
    assert client.get("/api/libraries/paradigms/asset", params={"path": "index.md"}).status_code == 404
    assert client.put("/api/libraries/paradigms/document", params={"path": "index.md"}, json={"content": "replace"}).status_code == 405
    assert client.get("/api/libraries/unknown").status_code == 422
    assert client.get("/api/libraries/paradigms", params={"limit": 0}).status_code == 422


def test_missing_root_and_large_document(libraries, monkeypatch):
    client, wiki, _ = libraries
    (wiki / "large.md").write_text("x" * (2 * 1024 * 1024 + 1))
    assert client.get("/api/libraries/paradigms").json()["total"] == 2
    assert client.get("/api/libraries/paradigms/document", params={"path": "large.md"}).status_code == 413
    monkeypatch.setenv("PAPER_AGENT_WIKI_ROOT", str(wiki / "not-initialized"))
    assert client.get("/api/libraries/paradigms").json()["available"] is False


def test_library_requires_login(libraries, monkeypatch):
    client, _, _ = libraries
    monkeypatch.setenv("PAPER_AGENT_PASSWORD_HASH", auth.password_hash("test-password"))
    for endpoint in ["/api/libraries/paradigms/figures", "/api/libraries/paradigms/graph", "/api/libraries/paradigms", "/api/libraries/battery/document?path=资源库总览.md", "/api/libraries/battery/asset?path=电芯/test.png"]:
        assert client.get(endpoint).status_code == 401


def test_graph_uses_recorded_edges_and_ignores_unresolved_or_invalid_data(libraries, tmp_path):
    import json
    client, wiki, _ = libraries
    paper = wiki / 'papers/example.md'
    paper.write_text('---\nnode_id: "paper:example"\ntask: ["soh"]\nmethod: ["Transformer"]\n---\n# Paper')
    (wiki / 'ideas').mkdir()
    (wiki / 'ideas/idea.md').write_text('---\nnode_id: idea:example\n---\n# Idea')
    (wiki / 'graph').mkdir()
    edge = {'from': 'idea:example', 'to': 'paper:example', 'type': 'inspired_by', 'evidence': 'source'}
    records = [edge, edge, {**edge, 'to': 'missing'}, {'from': []}, [], None]
    graph = wiki / 'graph/edges.jsonl'
    graph.write_text('\n'.join(json.dumps(x) for x in records) + '\ninvalid json')
    response = client.get('/api/libraries/paradigms/graph')
    assert response.status_code == 200
    data = response.json()
    assert len(data['nodes']) == 2 and len(data['edges']) == 1
    assert data['edges'][0] == {'source': 'idea:example', 'target': 'paper:example', 'type': 'inspired_by', 'evidence': 'source'}
    assert next(n for n in data['nodes'] if n['id'] == 'paper:example')['tags'] == ['soh', 'Transformer']
    assert str(wiki) not in response.text
    graph.unlink()
    outside = tmp_path / 'private.jsonl'
    outside.write_text(json.dumps(edge))
    graph.symlink_to(outside)
    assert client.get('/api/libraries/paradigms/graph').json()['edges'] == []


def test_figure_gallery_preserves_paper_caption_and_path_boundaries(libraries, tmp_path):
    client, wiki, _ = libraries
    figures = wiki / 'figures' / 'example'
    figures.mkdir(parents=True)
    (figures / 'fig1.png').write_bytes(b'image')
    caption = 'Figure 1: Measured capacity and prediction.\nSecond caption line.'
    (figures / 'fig1.md').write_text('# Figure 1\n\nSource: paper:example\nPDF page: 3\n\n' + caption)
    (figures / 'fig2.png').write_bytes(b'image')
    (figures / 'fig2.md').write_text('# Figure 2\n\nSource: paper:missing\nPaper title: Archived paper title\n\nOriginal caption')
    (figures / 'no-caption.jpg').write_bytes(b'image')
    (figures / '.hidden.png').write_bytes(b'hidden')
    outside = tmp_path / 'private.png'
    outside.write_bytes(b'private')
    (figures / 'symlink.png').symlink_to(outside)
    outside_note = tmp_path / 'private.md'
    outside_note.write_text('secret caption')
    (figures / 'no-caption.md').symlink_to(outside_note)
    response = client.get('/api/libraries/paradigms/figures')
    assert response.status_code == 200
    data = response.json()
    assert data['total'] == 3
    first = data['items'][0]
    assert first['paper_title'] == 'Battery Transformer'
    assert first['paper_path'] == 'papers/example.md'
    assert first['caption'] == caption and first['page'] == 3
    assert first['image_path'] == 'figures/example/fig1.png'
    second = data['items'][1]
    assert second['paper_title'] == 'Archived paper title' and second['paper_path'] is None
    assert data['items'][2]['caption'] == ''
    assert 'private' not in response.text and str(wiki) not in response.text
    assert client.get('/api/libraries/paradigms/asset', params={'path': first['image_path']}).content == b'image'
