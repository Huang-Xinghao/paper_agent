"""Lossless source blocks and conservative cleaning for academic papers."""

from __future__ import annotations

from collections import defaultdict
import hashlib
import json
import math
import re

PREPROCESSING_VERSION = "paper-evidence-v1"
NOISE_TYPES = {"header", "footer", "page_header", "page_footer", "page_number"}
TYPE_ALIASES = {
    "doc_title": "heading",
    "paragraph_title": "heading",
    "title": "heading",
    "equation": "formula",
    "interline_equation": "formula",
    "image": "figure",
    "chart": "figure",
    "image_caption": "caption",
    "chart_caption": "caption",
    "table_caption": "caption",
    "table_footnote": "footnote",
    "image_footnote": "footnote",
    "chart_footnote": "footnote",
    "page_footnote": "footnote",
}


def _bbox(value):
    if (
        isinstance(value, (list, tuple))
        and len(value) == 4
        and all(type(n) in (int, float) and math.isfinite(n) for n in value)
        and value[0] <= value[2]
        and value[1] <= value[3]
    ):
        return list(value)
    return None


def document_from_structured(
    data: dict, *, pdf_sha256: str = "", parser_identity: dict | None = None
) -> dict:
    """Keep original strings/coordinates; filtered blocks remain in the evidence store."""
    if data.get("is_full_document") is False:
        raise ValueError("MinerU returned a partial document")
    pages = data.get("pages")
    if (
        not isinstance(pages, list)
        or not pages
        or [p.get("page_idx") for p in pages] != list(range(len(pages)))
    ):
        raise ValueError("MinerU output has missing or unordered PDF pages")
    count = data.get("metadata", {}).get("document", {}).get("page_count")
    if count is not None and count != len(pages):
        raise ValueError("MinerU page count does not match the full PDF")
    blocks, diagnostics = [], []

    def append(block, page, local_id, parent=None, relation=None):
        text = block.get("content", "")
        if not isinstance(text, str):
            raise ValueError("Unsupported MinerU block content; expected text")
        original_type = str(block.get("type", "text")).lower()
        kind = TYPE_ALIASES.get(original_type, original_type)
        if relation:
            kind = "caption" if relation == "captions" else "footnote"
        source = {
            "id": local_id,
            "page": page,
            "type": kind,
            "original_type": original_type,
            "text": text,
            "bbox": _bbox(block.get("bbox")),
            "parent_id": parent,
            "relation": relation,
            "excluded_reason": None,
        }
        if original_type in NOISE_TYPES and parent is None:
            source["excluded_reason"] = "parser_marked_page_decoration"
        blocks.append(source)
        for field in ("captions", "footnotes"):
            for index, child in enumerate(block.get(field, [])):
                append(child, page, f"{local_id}-{field[0]}{index}", local_id, field)

    for page in pages:
        for index, block in enumerate(page.get("blocks", [])):
            append(block, page["page_idx"] + 1, f"p{page['page_idx'] + 1}-b{index}")

    # 只识别有归一化坐标、跨页重复的短边栏文字。表头、图注、公式和章节标题不参与。
    repeated = defaultdict(list)
    for block in blocks:
        box, text = block["bbox"], block["text"].strip()
        if (
            block["type"] == "text"
            and block["parent_id"] is None
            and box
            and all(0 <= n <= 1 for n in box)
            and (box[3] <= 0.06 or box[1] >= 0.94)
            and 0 < len(text) <= 120
        ):
            repeated[re.sub(r"\s+", " ", text)].append(block)
    for group in repeated.values():
        if len({b["page"] for b in group}) >= max(3, math.ceil(len(pages) * 0.6)):
            for block in group:
                block["excluded_reason"] = "repeated_margin_text"

    headings, current = [], None
    for block in blocks:
        if block["excluded_reason"]:
            block["section_id"] = None
            continue
        if block["type"] == "heading" and block["text"].strip():
            current = block["id"]
            headings.append(
                {
                    "id": current,
                    "title": block["text"].strip(),
                    "page": block["page"],
                    "bbox": block["bbox"],
                }
            )
        block["section_id"] = current
        if block["type"] in {"table", "formula"} and not block["text"].strip():
            diagnostics.append(
                {
                    "page": block["page"],
                    "block_id": block["id"],
                    "issue": f"empty_{block['type']}",
                    "action": "inspect_page_or_reparse",
                }
            )
        if block["type"] in {"caption", "footnote"} and block["parent_id"] is None:
            diagnostics.append(
                {
                    "page": block["page"],
                    "block_id": block["id"],
                    "issue": "unlinked_caption_or_note",
                    "action": "inspect_original_layout; no parent inferred",
                }
            )
        if "\ufffd" in block["text"]:
            diagnostics.append(
                {
                    "page": block["page"],
                    "block_id": block["id"],
                    "issue": "replacement_characters",
                    "action": "inspect_page_or_reparse",
                }
            )
    cleaned_pages = []
    for page in range(1, len(pages) + 1):
        content = [
            b["text"].strip()
            for b in blocks
            if b["page"] == page and not b["excluded_reason"] and b["text"].strip()
        ]
        cleaned_pages.append({"page": page, "text": "\n\n".join(content)})
        if not content:
            diagnostics.append(
                {
                    "page": page,
                    "issue": "no_readable_text",
                    "action": "inspect_page_or_ocr",
                }
            )
    if not any(p["text"] for p in cleaned_pages):
        raise ValueError("MinerU returned no readable paper content")
    fingerprint = hashlib.sha256(
        json.dumps(
            [PREPROCESSING_VERSION, pdf_sha256, parser_identity or {}, data],
            sort_keys=True,
            ensure_ascii=False,
        ).encode()
    ).hexdigest()
    return {
        "schema_version": PREPROCESSING_VERSION,
        "pdf_sha256": pdf_sha256,
        "parser": parser_identity or {},
        "document_id": fingerprint,
        "page_count": len(pages),
        "pages": cleaned_pages,
        "blocks": blocks,
        "sections": headings,
        "diagnostics": diagnostics,
        "coordinate_system": "parser_native; bbox copied without rescaling",
        "ocr_policy": "MinerU standard tier; diagnostic actions are recommendations, not OCR completion",
    }


def source_context(document: dict) -> dict:
    """Evidence IDs are local to this immutable document version, not globally unique."""
    return {
        "document_id": document["document_id"],
        "pdf_sha256": document["pdf_sha256"],
        "page_count": document["page_count"],
        "sections": document["sections"],
        "diagnostics": document["diagnostics"],
        "blocks": [b for b in document["blocks"] if not b["excluded_reason"]],
    }


def evidence_errors(evidence, document: dict) -> list[str]:
    if not isinstance(evidence, list) or not evidence:
        return ["nonempty evidence array required"]
    lookup = {b["id"]: b for b in document["blocks"]}
    errors = []
    for item in evidence:
        if not isinstance(item, dict):
            errors.append("evidence must contain objects")
            continue
        block_id = item.get("block_id")
        block = lookup.get(block_id) if isinstance(block_id, str) else None
        quote = item.get("quote")
        if not block or block["excluded_reason"]:
            errors.append(f"unknown or excluded block: {item.get('block_id')}")
        elif (
            not isinstance(quote, str)
            or not quote.strip()
            or quote not in block["text"]
        ):
            errors.append(f"quote is not a verbatim excerpt: {block['id']}")
    return errors


def evidence_context(evidence: list[dict], document: dict) -> list[dict]:
    """Expand excerpts to surrounding blocks AND all related table/figure notes."""
    blocks = [b for b in document["blocks"] if not b["excluded_reason"]]
    cited = {e["block_id"] for e in evidence}
    wanted = set(cited)
    for index, block in enumerate(blocks):
        if block["id"] in cited:
            wanted.update(
                b["id"]
                for b in blocks[max(0, index - 1) : index + 2]
                if b["page"] == block["page"]
            )
    parents = {b["parent_id"] or b["id"] for b in blocks if b["id"] in wanted}
    return [
        b
        for b in blocks
        if b["id"] in wanted or b["id"] in parents or b["parent_id"] in parents
    ]
