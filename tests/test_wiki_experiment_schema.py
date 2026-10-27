from paper_agent.tools import research_wiki


def test_direct_experiment_creation_has_no_removed_metadata(tmp_path):
    research_wiki.init_wiki(str(tmp_path))
    page = research_wiki.add_experiment(
        str(tmp_path), 'example', metrics='MAE: 0.1', reasoning='Evidence: Fig. 5 [p. 6]')
    text = page.read_text()
    header = text.split('\n---\n', 1)[0]
    for key in {'verdict', 'confidence', 'date', 'hardware', 'duration', 'provenance', 'research_object'}:
        assert not any(line.startswith(key + ':') for line in header.splitlines())
    assert '**verdict:**' not in text
    assert 'MAE: 0.1' in text
    assert 'Evidence: Fig. 5 [p. 6]' in text
