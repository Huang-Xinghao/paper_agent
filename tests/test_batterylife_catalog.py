from pathlib import Path

import pytest

from paper_agent.tools import batterylife_catalog as catalog


README = """# BatteryLife
### Raw dataset
- Tongji: [link](https://zenodo.org/records/6405084)
- ISU\\_ILCC: [link](https://iastate.figshare.com/example)
## Benchmark
"""


def test_search_uses_exact_official_entry(monkeypatch):
    monkeypatch.setattr(catalog, "_check_url", lambda url, timeout: (True, url))
    result = catalog.search_batterylife_dataset("Tongji", readme=README)
    assert result["success"] is True
    assert result["links"][0]["url"] == "https://zenodo.org/records/6405084"
    assert "NCA" in result["description"]


def test_search_does_not_infer_unknown_dataset(monkeypatch):
    monkeypatch.setattr(catalog, "_check_url", lambda url, timeout: (False, "HTTP 202"))
    monkeypatch.setattr(catalog, "_get_text", lambda url, timeout: "{}")
    result = catalog.search_batterylife_dataset("ISU-ILCC", readme=README)
    assert result["success"] is False
    assert result["links"][0]["reachable"] is False
    with pytest.raises(ValueError, match="Unknown BatteryLife dataset"):
        catalog.search_batterylife_dataset("NCA", readme=README)


def test_figshare_api_fallback_requires_matching_record(monkeypatch):
    monkeypatch.setattr(catalog, "_check_url", lambda url, timeout: (False, "HTTP 202"))
    monkeypatch.setattr(catalog, "_get_text", lambda url, timeout:
                        '{"id": 22582234, "title": "<b>ISU-ILCC Battery Aging Dataset</b>", '
                        '"files": [{"name": "capacity_fade.zip"}]}')
    result = catalog.search_batterylife_dataset("ISU-ILCC", readme=README)
    assert result["success"] is True
    assert result["links"][0]["reachable"] is False
    assert result["links"][1]["url"] == "https://api.figshare.com/v2/articles/22582234"


def test_sync_is_idempotent_and_preserves_existing_content(tmp_path: Path, monkeypatch):
    path = tmp_path / "Tongji_README.md"
    path.write_text("# Original\n\n## 本地数据接入\n\n<LOCAL_DATA_PATH>\n", encoding="utf-8")
    monkeypatch.setattr(catalog, "_get_text", lambda url, timeout: README)
    monkeypatch.setattr(catalog, "_check_url", lambda url, timeout: (True, url))
    first = catalog.sync_batterylife_descriptions(tmp_path)
    content = path.read_text(encoding="utf-8")
    second = catalog.sync_batterylife_descriptions(tmp_path)
    assert first[0]["success"] and second[0]["success"]
    assert path.read_text(encoding="utf-8") == content
    assert "<LOCAL_DATA_PATH>" in content
    assert content.count(catalog.MARKER) == 1
