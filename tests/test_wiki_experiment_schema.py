import sys

import pytest

from paper_agent.tools import research_wiki


@pytest.mark.parametrize("entry_point", ["python", "cli"])
def test_experiment_creation_has_current_body_and_no_removed_metadata(tmp_path, monkeypatch, entry_point):
    research_wiki.init_wiki(str(tmp_path))
    assert not (tmp_path / "query_pack.md").exists()
    if entry_point == "python":
        page = research_wiki.add_experiment(
            str(tmp_path), 'example', results='MAE: 0.1', protocol='Evidence: Fig. 5 [p. 6]')
    else:
        monkeypatch.setattr(sys, "argv", [
            "paper-wiki", "add_experiment", str(tmp_path), "--slug", "example",
            "--results", "MAE: 0.1", "--protocol", "Evidence: Fig. 5 [p. 6]",
        ])
        research_wiki.main()
        page = tmp_path / "experiments/example.md"
    text = page.read_text()
    header = text.split('\n---\n', 1)[0]
    for key in {'verdict', 'confidence', 'date', 'hardware', 'duration', 'provenance', 'research_object'}:
        assert not any(line.startswith(key + ':') for line in header.splitlines())
    assert '**verdict:**' not in text
    assert 'MAE: 0.1' in text
    assert 'Evidence: Fig. 5 [p. 6]' in text
    assert [line for line in text.splitlines() if line.startswith('## ')] == [
        '## Question', '## Hypothesis', '## Protocol', '## Results',
    ]


def test_standalone_experiment_update_refreshes_title_and_indexes(tmp_path, monkeypatch):
    research_wiki.init_wiki(str(tmp_path))
    assert not (tmp_path / "query_pack.md").exists()
    page = research_wiki.add_experiment(str(tmp_path), "example", title="Original", results="Old result")
    rebuilds = []
    original = research_wiki.rebuild_index

    def rebuild(*args, **kwargs):
        rebuilds.append("rebuild_index")
        return original(*args, **kwargs)

    monkeypatch.setattr(research_wiki, "rebuild_index", rebuild)
    research_wiki.add_experiment(str(tmp_path), "example", title="Updated", results="New result",
                                 update_on_exist=True)
    assert '# Updated\n' in page.read_text() and 'New result' in page.read_text()
    assert 'Old result' not in page.read_text()
    assert rebuilds == ["rebuild_index"]
