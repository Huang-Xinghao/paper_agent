"""Regression checks for real corpus failures and reviewed batch recovery."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from test_wiki_curation import curation
from paper_agent.tools import research_wiki
from paper_agent.tools.wiki_assets import figure_candidates
from paper_agent.tools.wiki_recovery import apply_edits, repair_bibliography, source_quote


@pytest.mark.parametrize('author,surname', [
    ('Chen Ma (马晨)', 'Ma'),
    ('Dongzhen Lyu（吕东祯）', 'Lyu'),
    ('Jia-hui Li (李嘉辉)', 'Li'),
    ('Lin Su (苏琳)', 'Su'),
    ('Yican Wang (王伊璨)', 'Wang'),
    ('Dirk Uwe Sauer', 'Sauer'),
    ('王伊璨', '王伊璨'),
])
def test_author_annotations_do_not_replace_romanized_surname(author, surname):
    assert research_wiki._last_name(author) == surname


def test_paper_filename_uses_surname_without_changing_author_metadata(tmp_path):
    research_wiki.init_wiki(str(tmp_path))
    paper = research_wiki.ingest_paper(str(tmp_path), title='A lean statistical cell-to-pack framework',
                                     authors=['Chen Ma (马晨)'], year=2026)
    assert paper.name == 'ma2026_lean_statistical_celltopack.md'
    assert 'Chen Ma (马晨)' in paper.read_text()


@pytest.mark.parametrize('slug', ['hübotter2024_theory', 'zhou2026_kclcacl₂licl', '李嘉辉2025_battery'])
def test_unicode_node_paths_preserve_explicit_identity(tmp_path, slug):
    research_wiki.init_wiki(str(tmp_path))
    idea = research_wiki.upsert_idea(str(tmp_path), slug + '-paper-idea', 'Source idea')
    experiment = research_wiki.add_experiment(str(tmp_path), slug + '-123',
                                             idea='idea:' + idea.stem, title='Reported experiment')
    assert idea.stem == slug + '-paper-idea'
    assert experiment.stem == slug + '-123'
    assert idea.is_file() and experiment.is_file()


def test_duplicate_caption_anchors_get_unique_stable_ids(tmp_path):
    (tmp_path / 'one.png').write_bytes(b'png')
    figures = [dict(page=2, number='1', caption='Figure 1. Architecture.', image='one.png'),
               dict(page=2, number='1', caption='Fig. 1 shows the architecture.'),
               dict(page=2, number='1', caption='Figure 1. Additional panel.')]
    data = {'cache': str(tmp_path), 'figures': figures}
    candidates = figure_candidates(data)
    assert [c['id'] for c in candidates] == ['p2-figure-1', 'p2-figure-1-occurrence-2', 'p2-figure-1-occurrence-3']
    assert candidates[0]['image_available'] and not candidates[1]['image_available']
    assert candidates == figure_candidates(data)


def test_bibliography_repair_recovers_exact_span_and_printed_identifier():
    text = 'Header\nSmith. Paper. 2024. URL [https://arxiv.org/abs/2406.14711](https://arxiv.org/abs/2406.14711).\nNext'
    quote = 'Smith. Paper. 2024. URL https://arxiv.org/abs/2406.14711.'
    draft = {'coined_terms': [], 'references': [dict(label='1', title='Paper', authors=['Smith'],
        year=2024, quote=quote, pages=[9], doi=None, arxiv_id=None)]}
    source = {'pages': [{'page': 3, 'text': text}]}
    fixed, changes = repair_bibliography(draft, source)
    ref = fixed['references'][0]
    assert ref['quote'] in text and ref['pages'] == [3]
    assert ref['arxiv_id'] == '2406.14711'
    assert draft['references'][0]['pages'] == [9]
    assert changes and repair_bibliography(fixed, source)[1] == []
    assert source_quote('Smith. Paper. 2025.', source['pages']) is None


@pytest.mark.parametrize('link', ['[link](https://arxiv.org/abs/2406.14711)',
                                  '<a href="https://arxiv.org/abs/2406.14711">link</a>'])
def test_bibliography_repair_cannot_import_hidden_link_identifiers(link):
    quote = f'Smith. Paper. 2024. {link}.'
    draft = {'coined_terms': [], 'references': [dict(label='1', title='Paper', authors=['Smith'],
        year=2024, quote=quote, pages=[1], doi=None, arxiv_id=None)]}
    fixed, changes = repair_bibliography(draft, {'pages': [{'page': 1, 'text': quote}]})
    assert fixed['references'][0]['arxiv_id'] is None and not changes


def test_journal_doi_suffix_cannot_be_readded_as_arxiv_after_review():
    from paper_agent.tools.wiki_relations import _printed_identifiers, validate_extraction
    quote = 'Zhou. Hidden risk. 2026. https://doi.org/10.1016/j.apenergy.2026.12865.'
    ref = dict(label='2', title='Hidden risk', authors=['Zhou'], year=2026,
               quote=quote, pages=[1], doi='10.1016/j.apenergy.2026.12865', arxiv_id=None)
    draft = {'coined_terms': [], 'references': [ref]}
    pages = [{'page': 1, 'text': quote}]
    fixed, changes = repair_bibliography(draft, {'pages': pages})
    assert fixed['references'][0]['arxiv_id'] is None and not changes
    ref['arxiv_id'] = '2026.12865'
    with pytest.raises(ValueError, match='identifier is not supported'):
        validate_extraction(draft, pages, {}, check_prose=False)
    assert _printed_identifiers('10.48550/arXiv.2406.14711')[1] == {'2406.14711'}
    assert _printed_identifiers(quote + ' arXiv:2406.14711')[1] == {'2406.14711'}


def test_full_visible_doi_can_use_matching_source_link_to_resolve_wrap_hyphen():
    from paper_agent.tools.wiki_relations import validate_extraction, _source_link_dois
    quote = ('Smith. Paper. 2013. '
             '[https://doi.org/10.1016/j.mi-crorel.2012.12.003]'
             '(https://doi.org/10.1016/j.microrel.2012.12.003).')
    ref = dict(label='42', title='Paper', authors=['Smith'], year=2013,
               quote=quote, pages=[1], doi=None, arxiv_id=None)
    draft = {'coined_terms': [], 'references': [ref]}
    pages = [{'page': 1, 'text': quote}]
    fixed, changes = repair_bibliography(draft, {'pages': pages})
    assert fixed['references'][0]['doi'] == '10.1016/j.microrel.2012.12.003'
    assert changes and not repair_bibliography(fixed, {'pages': pages})[1]
    validate_extraction(fixed, pages, {}, check_prose=False)
    for label in ['link', '10.1016/j.different.2012.12.003']:
        unsupported = quote.replace('https://doi.org/10.1016/j.mi-crorel.2012.12.003]', label + ']')
        assert not _source_link_dois(unsupported)
        fixed['references'][0]['quote'] = unsupported
        with pytest.raises(ValueError, match='identifier is not supported'):
            validate_extraction(fixed, [{'page': 1, 'text': unsupported}], {}, check_prose=False)


def test_abstract_ignores_only_source_identified_administrative_interruptions():
    from paper_agent.tools.wiki_review import abstract_provenance
    nav = r'\***sı** [Supporting Information](https://pubs.acs.org/doi/10.1021/test?goto=supporting-info&ref=pdf)'
    contact = '∗Corresponding author. E-mail: author@example.com'
    document = {'pages': [{'page': 1, 'text': 'Abstract\nFirst scientific sentence.\n' + nav + '\n' + contact},
                          {'page': 2, 'text': 'Second scientific sentence.'}],
                'blocks': [{'page': 1, 'original_type': 'page_footnote', 'text': contact}]}
    report = abstract_provenance('First scientific sentence. Second scientific sentence.', document)
    assert report['match'] == 'publisher_display' and report['pages'] == [1, 2]
    assert len(report['excluded_administrative_text']) == 2
    document['pages'][0]['text'] += '\nAn important scientific qualification.'
    assert abstract_provenance('First scientific sentence. Second scientific sentence.', document) is None
    assert abstract_provenance('First changed sentence. Second scientific sentence.', document) is None


def test_abstract_page_break_excludes_typed_publication_notes_but_not_science():
    from paper_agent.tools.wiki_review import abstract_provenance
    notes = ['Received 5 March 2026; Accepted 9 June 2026',
             '2666-5468/© 2026 The Authors. Published by Elsevier Ltd.',
             '[https://doi.org/10.1016/j.test](https://doi.org/10.1016/j.test)']
    document = {'pages': [{'page': 1, 'text': 'First sentence.\n' + '\n'.join(notes)},
                          {'page': 2, 'text': 'Second sentence.'}],
                'blocks': [{'page': 1, 'original_type': 'page_footnote', 'text': text} for text in notes]}
    assert abstract_provenance('First sentence. Second sentence.', document)
    text = 'This result holds only at high temperature.'
    document['pages'][0]['text'] += '\n' + text
    document['blocks'].append({'page': 1, 'original_type': 'page_footnote', 'text': text})
    assert abstract_provenance('First sentence. Second sentence.', document) is None


def test_native_pdf_abstract_recovery_keeps_original_and_rejects_changed_values(tmp_path):
    import hashlib
    pymupdf = pytest.importorskip('pymupdf')
    from paper_agent.tools.wiki_recovery import supplement_native_frontmatter
    from paper_agent.tools.wiki_review import abstract_provenance
    pdf = tmp_path / 'source.pdf'
    with pymupdf.open() as native:
        page = native.new_page()
        page.insert_text((72, 72), 'ABSTRACT: We measured 12 samples. The result is accurate.')
        native.save(pdf)
    source = {'pdf_sha256': hashlib.sha256(pdf.read_bytes()).hexdigest(),
              'pages': [{'page': 1, 'text': 'Corrupt layout extraction.'}], 'blocks': []}
    enriched = supplement_native_frontmatter(source, pdf)
    assert enriched['pages'] == source['pages'] and not source['blocks']
    assert enriched['native_frontmatter']['pages'][0]['raw_text']
    assert abstract_provenance('We measured 12 samples. The result is accurate.', enriched)['match'] == 'native_pdf_text'
    assert abstract_provenance('We measured 13 samples. The result is accurate.', enriched) is None
    with pytest.raises(ValueError, match='hash'):
        supplement_native_frontmatter({**source, 'pdf_sha256': 'wrong'}, pdf)


def test_reference_display_math_and_cross_page_watermark_preserve_identity():
    from paper_agent.tools.wiki_relations import validate_extraction
    quote = ('Smith. <a href="https://doi.org/10.1234/hidden">Persistent Li<sub>1−</sub></a> '
             r'$\mathrm{_{x}Ni_{1/3}O_{2}}$' + '\n'
             'Downloaded from pubs.acs.org/paper.pdf by UNIV user on 08 November 2026\n'
             'Secondary Particles. 2016.')
    draft = {'coined_terms': [], 'references': [dict(label='43',
             title='Persistent Li1−xNi1/3O2 Secondary Particles', authors=['Smith'],
             year=2016, doi=None, arxiv_id=None, pages=[1], quote=quote)]}
    pages = [{'page': 1, 'text': quote}]
    validate_extraction(draft, pages, {}, check_prose=False)
    draft['references'][0]['doi'] = '10.1234/hidden'
    with pytest.raises(ValueError, match='identifier is not supported'):
        validate_extraction(draft, pages, {}, check_prose=False)
    draft['references'][0].update(doi=None, title='Persistent Li1−xNi1/3O3 Secondary Particles')
    with pytest.raises(ValueError, match='metadata is not supported'):
        validate_extraction(draft, pages, {}, check_prose=False)


def test_edits_preserve_identity_and_reference_count():
    draft = {'metadata': {'title': 'Original', 'scenario': []},
             'references': [{'label': '1', 'doi': None}], 'sections': {'Method': 'Old'}}
    fixed = apply_edits(draft, {'edits': [{'path': '/sections/Method', 'value': 'New'},
                                         {'path': '/metadata/scenario', 'value': ['laboratory']}]})
    assert fixed['metadata']['title'] == 'Original'
    assert fixed['sections']['Method'] == 'New' and draft['sections']['Method'] == 'Old'
    for path, value in [('/metadata/title', 'Changed'), ('/references', []), ('/approval', {})]:
        with pytest.raises(ValueError):
            apply_edits(draft, {'edits': [{'path': path, 'value': value}]})


def test_batch_requests_correction_of_invalid_edit_without_partial_mutation(tmp_path):
    from scripts.wiki_batch import BatchWikiAgent
    agent = BatchWikiAgent.__new__(BatchWikiAgent)
    draft = {'experiments': [{'title': 'Old'}], 'references': []}
    calls = []
    def analyze(prompt, body):
        calls.append(json.loads(body))
        return {'edits': [{'path': '/experiments/0/title', 'value': 'New'},
                          *([{'path': '/experiments/1/title', 'value': 'Invalid'}] if len(calls) == 1 else [])]}
    agent.analyze = analyze
    fixed = agent._edit_draft(draft, {'draft': draft}, tmp_path, 0)
    assert fixed['experiments'][0]['title'] == 'New'
    assert draft['experiments'][0]['title'] == 'Old'
    assert 'outside draft' in calls[1]['edit_application_error']
    assert (tmp_path / 'edit-request-0-retry.json').is_file()
    with pytest.raises(ValueError, match='outside draft'):
        apply_edits(draft, {'edits': [{'path': '/experiments/-1/title', 'value': 'Invalid'}]})


def test_batch_adapter_reuses_source_checked_draft_and_publishes(curation, monkeypatch):
    from scripts.wiki_batch import BatchWikiAgent
    from paper_agent.integrations.model import ModelResponse
    from paper_agent.tools.wiki_prompts import REVIEW_PROMPT, GENERATION_PROMPT
    f = curation
    directory = f.project / '.paper-agent/wiki-preprocessing' / f.document['document_id']
    directory.mkdir(parents=True)
    (directory / 'source.json').write_text(json.dumps(f.document))
    saved = deepcopy(f.draft)
    saved['metadata']['authors'] = ['Hübotter']
    (directory / 'draft.json').write_text(json.dumps(saved))
    calls = []
    class Model:
        model = 'fixture'
        def complete(self, messages, tools):
            system = messages[0]['content']; calls.append(system)
            assert system == REVIEW_PROMPT
            draft = json.loads(messages[1]['content'])['draft']
            verdict = {'status': 'supported', 'reason': 'Source-supported.'}
            return ModelResponse(content=json.dumps({
                'claims': [{'id': c['id'], **verdict} for c in draft['critical_claims']],
                'sections': {name: verdict for name in draft['sections']}, 'entities': verdict,
                'coverage': {**verdict, 'missing': []}, 'issues': []}))
    agent = BatchWikiAgent(f.project, wiki_root=f.root, model_client=Model())
    monkeypatch.setattr(agent, '_read_full_paper', lambda *a: {
        'document': f.document, 'figures': [], 'cache': str(f.project / 'cache')})
    state = agent.go('Read', paper_path=str(f.pdf))
    assert (f.project / state.wiki_path).is_file()
    assert (f.project / state.idea_path).is_file()
    assert 'hübotter' in state.idea_path
    assert len(calls) == 1 and GENERATION_PROMPT not in calls
    assert (directory / 'batch-recovery/published.json').is_file()


def test_partial_unicode_nodes_migrate_with_backups_and_graph(tmp_path):
    from scripts.repair_wiki_node_paths import migrate
    root, backup = tmp_path / 'wiki', tmp_path / 'backup'
    research_wiki.init_wiki(str(root))
    old = research_wiki.upsert_idea(str(root), 'h-botter2024_theory-paper-idea', 'Theory')
    expected = root / 'ideas/hübotter2024_theory-paper-idea.md'
    research_wiki.add_edge(str(root), 'idea:' + old.stem, 'paper:example', 'inspired_by')
    report = {'items': [{'status': 'failed', 'error': f"FileNotFoundError: '{expected}'"}]}
    result = migrate(root, report, backup)
    assert len(result['moved']) == 1 and expected.is_file() and not old.exists()
    assert (backup / 'ideas' / old.name).is_file()
    assert 'idea:hübotter2024_theory-paper-idea' in (root / 'graph/edges.jsonl').read_text()
    assert not migrate(root, report, tmp_path / 'again')['moved']


def test_reference_revision_is_revalidated_and_reaudited(curation, tmp_path):
    from paper_agent.tools.wiki_review import prepare_analysis
    from paper_agent.tools.wiki_prompts import REVIEW_PROMPT, REVISION_PROMPT
    f = curation
    draft, doc = deepcopy(f.draft), deepcopy(f.document)
    quote = 'Smith. Source paper. 2024. doi: 10.1234/source'
    doc['pages'][0]['text'] += '\n' + quote
    doc['blocks'].append({'id': 'ref1', 'page': 1, 'text': quote, 'type': 'text',
                         'bbox': None, 'parent_id': None, 'section_id': None, 'excluded_reason': None})
    ref = dict(label='1', title='Source paper', authors=['Smith'], year=2024,
               quote=quote, pages=[1], doi=None, arxiv_id=None)
    draft['references'] = [ref]
    calls = []
    def request(system, user):
        calls.append(system)
        value = json.loads(user)['draft']
        if system == REVISION_PROMPT:
            return {'references': [{**ref, 'doi': '10.1234/source'}], 'metadata': {'title': 'Ignored'}}
        assert system == REVIEW_PROMPT
        verdict = {'status': 'supported', 'reason': 'Grounded in source.'}
        review = {'claims': [{'id': c['id'], **verdict} for c in value['critical_claims']],
                  'sections': {name: verdict for name in value['sections']}, 'entities': verdict,
                  'coverage': {**verdict, 'missing': []}, 'issues': []}
        if value['references'][0]['doi'] is None:
            review['entities'] = {'status': 'partial', 'reason': 'Printed DOI omitted.'}
            review['blocking_findings'] = [{'target': 'entities', 'field': '/references/0/doi',
                'rule': 'bibliography', 'observed': 'null', 'expected': '10.1234/source',
                'evidence': [{'block_id': 'ref1', 'quote': quote}]}]
        return review
    revised, quality = prepare_analysis(draft, doc, request, tmp_path / 'review', lambda *a, **k: None)
    assert quality['status'] == 'passed'
    assert revised['metadata']['title'] == f.draft['metadata']['title']
    assert revised['references'][0]['doi'] == '10.1234/source'
    assert calls == [REVIEW_PROMPT, REVISION_PROMPT, REVIEW_PROMPT]


def test_split_doi_fill_uses_complete_identifier_not_prefix():
    quote = 'Smith. Source paper. 2024. doi: 10.18653/v1/2024. emnlp-main.992.'
    ref = dict(label='1', title='Source paper', authors=['Smith'], year=2024,
               quote=quote, pages=[1], doi=None, arxiv_id=None)
    fixed, changes = repair_bibliography({'coined_terms': [], 'references': [ref]},
                                         {'pages': [{'page': 1, 'text': quote}]})
    assert fixed['references'][0]['doi'] == '10.18653/v1/2024.emnlp-main.992'
    assert changes


def test_citation_index_preserves_conflicts_and_ambiguous_matches():
    from paper_agent.tools.wiki_relations import _citation_index, _citation_candidates, _match
    papers = [dict(id='paper:one', doi='10.1234/one', arxiv_id='2401.00001',
                   title='sourcetitle', year='2024', authors={'smith'}),
              dict(id='paper:two', doi='10.1234/two', arxiv_id='',
                   title='sourcetitle', year='2024', authors={'smith'}),
              dict(id='paper:three', doi='', arxiv_id='',
                   title='differenttitle', year='2025', authors={'brown'})]
    lookup = _citation_index(papers)
    for doi in [None, '10.1234/one', '10.1234/wrong']:
        for arxiv in [None, '2401.00001', '2401.00002']:
            for title in ['Source title', 'Different title', 'Unknown']:
                for year in [None, 2024, 2025]:
                    ref = dict(doi=doi, arxiv_id=arxiv, title=title, year=year, authors=['Smith'])
                    assert _match(ref, papers) == _match(ref, _citation_candidates(ref, lookup))


@pytest.mark.parametrize('text', [
    '| A | B |\n| --- | --- |\n| x | p(y|x) |',
    '| A | B |\n| --- | --- |\n| x | `a|b` |',
    '## Limitations\n_TODO._',
])
def test_publication_rejects_rendering_loss_and_unfinished_sections(text):
    from paper_agent.tools.wiki_publication import markdown_errors
    assert markdown_errors(text)


def test_publication_accepts_escaped_math_and_fenced_examples():
    from paper_agent.tools.wiki_publication import markdown_errors
    assert not markdown_errors('| A | B |\n| --- | --- |\n| x | p(y\\|x) |')
    assert not markdown_errors('```text\nTODO example\n| A | B |\n| --- | --- |\n| a|b|c |\n```')


def test_publisher_checks_existing_identity_before_overwrite(curation):
    from test_wiki_curation import prepared
    f = curation
    research_wiki.init_wiki(str(f.root))
    other = research_wiki.ingest_paper(str(f.root), title='Another study',
        authors=['Different Researcher'], year=2024, doi='10.1234/template')
    before = other.read_bytes()
    f.draft['metadata']['doi'] = '10.1234/template'
    ingestion, ctx = prepared(f)
    ingestion.wiki_read_source(ctx)
    assert not ingestion.wiki_review(ctx, str(ingestion.draft_path)).is_error
    with pytest.raises(ValueError, match='identity conflict'):
        ingestion.wiki_publish(ctx, str(ingestion.draft_path))
    assert other.read_bytes() == before


def test_republication_refreshes_identity_and_archives_only_generated_experiments(curation):
    from test_wiki_curation import prepared
    from paper_agent.tools.wiki_layout import resolve_legacy_path
    f = curation
    ingestion, ctx = prepared(f)
    ingestion.wiki_read_source(ctx)
    assert not ingestion.wiki_review(ctx, str(ingestion.draft_path)).is_error
    assert not ingestion.wiki_publish(ctx, str(ingestion.draft_path)).is_error
    paper = next((f.root / 'papers').glob('*.md'))
    old = next((f.root / 'experiments').glob('*.md'))
    old_content = old.read_bytes()
    manual = research_wiki.add_experiment(str(f.root), paper.stem + '-manual',
        idea='idea:' + paper.stem + '-paper-idea', title='User experiment', tags=['manual'])
    f.draft['idea']['title'] = 'Updated idea title'
    f.draft['metadata']['arxiv_id'] = '2611.00001'
    f.draft['experiments'][0]['title'] += ' revised'
    from paper_agent.tools.wiki_ingest import WikiIngestion
    from test_wiki_curation import CurationModel
    ingestion = WikiIngestion(f.project, model=CurationModel(f))
    ingestion.assign(paper_path=str(f.pdf), complete_relations=False, force=True)
    ingestion.wiki_prepare(ctx, str(f.pdf))
    from paper_agent.storage import write_json
    write_json(ingestion.draft_path, f.draft)
    ingestion.wiki_read_source(ctx)
    assert not ingestion.wiki_review(ctx, str(ingestion.draft_path)).is_error
    assert not ingestion.wiki_publish(ctx, str(ingestion.draft_path)).is_error
    assert '2611.00001' in paper.read_text()
    assert '_TODO' not in paper.read_text()
    assert 'Updated idea title' in next((f.root / 'ideas').glob('*.md')).read_text()
    assert not old.exists() and manual.exists()
    assert resolve_legacy_path(f.root, old.relative_to(f.root)).read_bytes() == old_content
    edges = [json.loads(line) for line in (f.root / 'graph/edges.jsonl').read_text().splitlines()]
    assert not any('exp:' + old.stem in (e['from'], e['to']) for e in edges)
