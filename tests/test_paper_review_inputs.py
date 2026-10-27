from pathlib import Path

import pytest

from paper_agent.agent.paper_review_inputs import paper_review_inputs


def test_collects_referenced_dependencies_and_excludes_archives(tmp_path):
    files = {
        'paper/main.tex': r'''% \input{audit/old}
\input{sections/current}
\bibliography{references}
\graphicspath{{figures/}}
\includegraphics{actual}
''',
        'paper/sections/current.tex': r'\input{figures/tables/current}',
        'paper/references.bib': 'references',
        'figures/tables/current.tex': 'table',
        'figures/actual.pdf': 'pdf',
        'figures/actual.png': 'duplicate',
        'figures/unused.png': 'unused',
        'paper/audit/old.tex': 'historical',
        'paper/main.pdf': 'current pdf',
        'refine-logs/EXPERIMENT_RESULTS.md': 'summary',
    }
    for name, content in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    selected = paper_review_inputs(tmp_path)
    assert set(selected) == {Path(name) for name in files if name not in {
        'figures/actual.png', 'figures/unused.png', 'paper/audit/old.tex'}}


@pytest.mark.parametrize('dependency', ['missing', '../outside', 'audit/old'])
def test_unresolved_or_excluded_dependencies_fail_explicitly(tmp_path, dependency):
    (tmp_path / 'paper/audit').mkdir(parents=True)
    (tmp_path / 'paper/audit/old.tex').write_text('old')
    (tmp_path / 'paper/main.tex').write_text('\\input{' + dependency + '}')
    with pytest.raises(ValueError, match='Missing or excluded'):
        paper_review_inputs(tmp_path)
