"""Conservative source-backed repairs for saved Wiki drafts and bounded edits."""
from copy import deepcopy
import re
import hashlib

from .wiki_relations import _bibliography_text, _printed_identifiers, _source_link_dois, validate_extraction


def supplement_native_frontmatter(document, pdf):
    """Add independent front-page PDF text when layout extraction corrupts an abstract.

    Retain the full MinerU source and raw native text. Only physical line-end
    hyphenation is normalized, with the original extraction retained for review.
    """
    if hashlib.sha256(pdf.read_bytes()).hexdigest() != document['pdf_sha256']:
        raise ValueError('Native PDF source hash does not match parsed document')
    try:
        import pymupdf
    except ImportError:
        return document
    try:
        native = pymupdf.open(pdf)
    except pymupdf.FileDataError:
        return document
    output = deepcopy(document)
    records = []
    with native:
        for index in range(min(3, len(native))):
            raw = native[index].get_text('text')
            if not raw.strip():
                continue
            text = re.sub(r'(?<=\w)-[ \t]*\n[ \t]*(?=\w)', '', raw)
            records.append({'page': index + 1, 'text': text, 'raw_text': raw})
            block_id = f'p{index + 1}-native-frontmatter'
            output['blocks'] = [b for b in output['blocks'] if b['id'] != block_id]
            output['blocks'].append({'id': block_id, 'page': index + 1,
                'type': 'native_pdf_text', 'original_type': 'native_pdf_text',
                'text': text, 'bbox': [0, 0, 1, 1], 'parent_id': None,
                'relation': None, 'excluded_reason': None, 'section_id': None})
    output['native_frontmatter'] = {'parser': 'PyMuPDF', 'version': pymupdf.VersionBind,
        'pdf_sha256': document['pdf_sha256'], 'normalization': 'physical-line-end-hyphenation',
        'pages': records}
    return output


def _visible_positions(text):
    """Keep visible Markdown link labels and their offsets in the raw source."""
    positions = []
    cursor = 0
    pattern = r'\[([^\]\n]*)\]\((?:[^()\s]|\([^()\s]*\))*\)'
    for match in re.finditer(pattern, text):
        positions.extend(range(cursor, match.start()))
        positions.extend(range(match.start(1), match.end(1)))
        cursor = match.end()
    positions.extend(range(cursor, len(text)))
    return [(text[i], i) for i in positions if not text[i].isspace()]


def source_quote(quote, pages):
    """Resolve display-only differences to an exact, unique raw source span."""
    pairs = []
    for page in pages:
        pairs.extend((char, page['page'], pos) for char, pos in _visible_positions(page['text']))
    haystack = ''.join(char for char, _, _ in pairs)
    needle = ''.join(char for char, _ in _visible_positions(quote))
    if not needle:
        return None
    start = haystack.find(needle)
    if start < 0 or haystack.find(needle, start + 1) >= 0:
        return None
    first, last = pairs[start], pairs[start + len(needle) - 1]
    selected = [p for p in pages if first[1] <= p['page'] <= last[1]]
    raw = '\n'.join(p['text'][first[2] if p['page'] == first[1] else 0:
                            last[2] + 1 if p['page'] == last[1] else None] for p in selected)
    return raw, [p['page'] for p in selected]


def repair_bibliography(draft, document):
    """Restore exact quote spans and unambiguous printed IDs; never infer an ID."""
    output = deepcopy(draft)
    changes = []
    pages = document['pages']
    by_page = {p['page']: p['text'] for p in pages}
    for index, ref in enumerate(output.get('references', [])):
        if not isinstance(ref, dict) or not isinstance(ref.get('quote'), str):
            continue
        old = deepcopy(ref)
        quoted = ref['quote']
        cited = ref.get('pages', [])
        source = '\n'.join(by_page.get(p, '') for p in cited)
        if ' '.join(quoted.split()) not in ' '.join(source.split()):
            match = source_quote(quoted, pages)
            if match:
                ref['quote'], ref['pages'] = match
        # Identifiers are changed only after the complete quote and identity are grounded.
        probe = {**ref, 'doi': None, 'arxiv_id': None}
        try:
            validate_extraction({**output, 'references': [probe]}, pages, {}, check_prose=False)
        except (ValueError, TypeError, KeyError):
            continue
        dois, arxiv = _printed_identifiers(_bibliography_text(ref['quote']))
        source_links = _source_link_dois(ref['quote'])
        for shown, target in source_links.items():
            dois.discard(shown)
            dois.add(target)
            if ref.get('doi') == shown:
                ref['doi'] = target
        # Split DOI suffixes need to be joined only at punctuation; all characters remain.
        for candidate in re.findall(r'10\.\d{4,9}/[^\s]+(?:\s+[a-z0-9][-._;()/:a-z0-9]*)?',
                                    _bibliography_text(ref['quote']).casefold()):
            joined = re.sub(r'(?<=[./-])\s+', '', candidate).rstrip('.,;:')
            if not re.search(r'\s', joined):
                dois.add(source_links.get(joined, joined))
        dois = {doi for doi in dois if not any(other != doi and other.startswith(doi)
                                               for other in dois)}
        for field, ids in [('doi', dois), ('arxiv_id', arxiv)]:
            if len(ids) == 1 and not ref.get(field):
                candidate = {**ref, field: next(iter(ids))}
                try:
                    validate_extraction({**output, 'references': [candidate]}, pages, {}, check_prose=False)
                    ref[field] = candidate[field]
                except ValueError:
                    pass
        if ref != old:
            changes.append({'index': index, 'before': old, 'after': deepcopy(ref),
                            'basis': 'exact full-source quotation and printed identifier'})
    return output, changes


EDIT_PROMPT = '''Repair the supplied Wiki draft against the COMPLETE source_document and exact validation/review findings.
Return JSON {"edits":[{"path":"/sections/Method","value":"corrected complete field"}]}.
Use JSON pointers into the draft. Prefer the smallest affected field, preserving all unrelated content.
All generated prose and labels must be English; original abstracts, quotations and bibliography retain source language.
Follow the supplied content_contract and schema. Read all source blocks; do not invent experiments, values or evidence.
Correct reference quotes by copying CONTIGUOUS raw source text and its physical pages. Do not shorten the bibliography.
You may repair metadata classification arrays, coined_terms errors, references, scientific sections and entity content.
Paper title/authors/year/venue/DOI/arXiv identity is frozen. Do not modify it. No edits to reviews or approval records.
An absent detail must stay explicitly uncertain. Report source-internal conflicts with evidence for both versions.
Never treat plans, ingestion, validation or reviewer approval as independent scientific replication.
Keep at most one representative overall architecture figure. Do not add unverified labels to satisfy a reviewer.
'''


def apply_edits(draft, response):
    """Apply bounded draft-only JSON pointer replacements, freezing paper identity."""
    result = deepcopy(draft)
    if not isinstance(response, dict) or not isinstance(response.get('edits'), list):
        raise ValueError('Repair requires an edits array')
    allowed = {'metadata', 'sections', 'section_status', 'critical_claims', 'idea',
               'datasets', 'experiments', 'figure_selection', 'references', 'coined_terms'}
    from .research_wiki import CLASSIFICATION_FIELDS
    for edit in response['edits']:
        path = edit.get('path', '')
        if not path.startswith('/') or 'value' not in edit:
            raise ValueError('Each edit needs a JSON pointer and value')
        parts = [part.replace('~1', '/').replace('~0', '~') for part in path[1:].split('/')]
        if parts[0] not in allowed:
            raise ValueError(f'Cannot edit this field: {path}')
        if parts[0] == 'metadata' and (len(parts) < 2 or parts[1] not in CLASSIFICATION_FIELDS):
            raise ValueError('Paper identity cannot be changed by draft repair')
        parent = result
        for part in parts[:-1]:
            if isinstance(parent, list):
                position = int(part)
                if position < 0 or position >= len(parent):
                    raise ValueError('Edit array index outside draft')
                parent = parent[position]
            else:
                parent = parent[part]
        if isinstance(parent, list):
            position = int(parts[-1])
            if position < 0 or position >= len(parent):
                raise ValueError('Edit array index outside draft')
            parent[position] = deepcopy(edit['value'])
        else:
            parent[parts[-1]] = deepcopy(edit['value'])
    before, after = draft.get('references', []), result.get('references', [])
    if len(before) != len(after) or [r.get('label') for r in before] != [r.get('label') for r in after]:
        raise ValueError('Repair must preserve bibliography count, order and labels')
    return result
