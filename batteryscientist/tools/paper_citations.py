"""Count distinct papers cited by the reachable manuscript body.

This checks citation coverage, not the truth or relevance of a source; those
remain part of the planning, writing and review instructions.
"""

from pathlib import Path
import re


MIN_BODY_PAPERS = 20
_INPUT = re.compile(r"\\(?:input|include)\s*\{([^}]+)\}")
_CITE = re.compile(
    r"\\(?:cite(?:p|t|alp|alt|author|year|num)?|parencite|textcite|autocite|footcite)"
    r"\*?\s*(?:\[[^\]]*\]\s*)*\{([^}]+)\}"
)


def _uncomment(text):
    return re.sub(r"(?<!\\)(?:\\\\)*%[^\n]*", "", text)


def validate_body_citations(project, *, compiled=False):
    """Reject fewer than 20 distinct, bibliographically resolved body papers."""
    project = Path(project).resolve()
    main = project / "paper/main.tex"
    bibliography = set()

    def resolve(name, parent, suffix):
        path = Path(name.strip())
        if not path.suffix:
            path = path.with_suffix(suffix)
        for base in (project, main.parent, parent):
            candidate = (base / path).resolve()
            if candidate.is_relative_to(project) and candidate.is_file():
                return candidate
        raise ValueError(f"Citation check cannot locate manuscript input: {name}")

    def expand(path, stack=()):
        if path in stack:
            raise ValueError(f"Cyclic manuscript input: {path.relative_to(project)}")
        source = _uncomment(path.read_text(encoding="utf-8"))
        for match in re.finditer(r"\\bibliography\s*\{([^}]+)\}|"
                                 r"\\addbibresource(?:\[[^\]]*\])?\s*\{([^}]+)\}", source):
            for name in (match[1] or match[2]).split(","):
                bibliography.add(resolve(name, path.parent, ".bib"))
        return _INPUT.sub(lambda m: expand(resolve(m[1], path.parent, ".tex"),
                                            (*stack, path)), source)

    source = expand(main)
    document = re.search(r"\\begin\{document\}(.*?)\\end\{document\}", source, re.S)
    if document is None:
        raise ValueError("Citation check requires a complete paper/main.tex document")
    body = re.split(r"\\(?:appendix|appendices)\b|\\begin\{appendices\}|"
                    r"\\(?:bibliography|printbibliography)\b|\\begin\{thebibliography\}",
                    document[1], maxsplit=1)[0]
    keys = {key.strip() for match in _CITE.finditer(body)
            for key in match[1].split(",") if key.strip()}
    entries = {}
    for path in sorted(bibliography):
        text = _uncomment(path.read_text(encoding="utf-8"))
        # Delimit entries with balanced braces, including nested title braces.
        for match in re.finditer(r"@(?!comment\b|string\b|preamble\b)\w+\s*\{\s*([^,\s]+)\s*,", text, re.I):
            start, depth, pos = match.end(), 1, match.end()
            while pos < len(text) and depth:
                if text[pos] == "{" and (pos == 0 or text[pos - 1] != "\\"):
                    depth += 1
                elif text[pos] == "}" and text[pos - 1] != "\\":
                    depth -= 1
                pos += 1
            if depth:
                raise ValueError(f"Unclosed BibTeX entry: {match[1]}")
            fields = text[start:pos - 1]
            values = {}
            for field in ("doi", "title"):
                value = re.search(r"\b" + field + r'\s*=\s*(\{.*?\}|".*?")\s*(?:,|$)', fields, re.I | re.S)
                if value:
                    raw = value[1].strip('{}"').lower()
                    if field == "doi":
                        raw = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", raw)
                    values[field] = re.sub(r"[^\w]", "", raw)
            if match[1] in entries:
                raise ValueError(f"Duplicate BibTeX key: {match[1]}")
            entries[match[1]] = values
    missing = sorted(keys - entries.keys())
    if missing:
        raise ValueError("Body citations lack BibTeX entries: " + ", ".join(missing))
    # Merge aliases by either DOI or title, including aliases with missing DOI.
    groups = []
    for key in sorted(keys):
        values = entries[key]
        if not values.get("title"):
            raise ValueError(f"Cited BibTeX entry needs a literal title: {key}")
        identities = {f"{field}:{value}" for field, value in values.items() if value}
        matching = [group for group in groups if group & identities]
        for group in matching:
            identities |= group
            groups.remove(group)
        groups.append(identities)
    count = len(groups)
    if count < MIN_BODY_PAPERS:
        raise ValueError(f"正文实际引用仅{count}篇不同论文，至少需要{MIN_BODY_PAPERS}篇；"
                         "请围绕相关论点定向核实并补齐，未引用书目、附录和nocite不计数。")
    if compiled:
        bbl = main.with_suffix(".bbl")
        if not bbl.is_file():
            raise ValueError("Compiled bibliography is missing: paper/main.bbl")
        text = _uncomment(bbl.read_text(encoding="utf-8"))
        rendered = set(re.findall(r"\\bibitem(?:\s*\[[^\n]*?\])?\s*\{([^}]+)\}|"
                                  r"\\entry\s*\{([^}]+)\}", text))
        rendered_keys = {key for pair in rendered for key in pair if key}
        if keys - rendered_keys:
            raise ValueError("Compiled bibliography omits body citations: "
                             + ", ".join(sorted(keys - rendered_keys)))
    return count
