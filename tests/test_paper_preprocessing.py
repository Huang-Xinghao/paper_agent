"""Synthetic parser fixtures; no fixture text is a quotation from a real paper."""

from copy import deepcopy

import pytest

from paper_agent.tools.paper_preprocessing import (
    document_from_structured,
    evidence_context,
    evidence_errors,
)


def sample():
    return {
        "pages": [
            {
                "page_idx": 0,
                "blocks": [
                    {"type": "header", "content": "Synthetic Journal"},
                    {
                        "type": "paragraph_title",
                        "content": "2 Method",
                        "bbox": [0.1, 0.1, 0.5, 0.15],
                    },
                    {
                        "type": "text",
                        "content": "训练需要历史记录；推理不需要目标对象的历史。",
                    },
                    {
                        "type": "equation",
                        "content": r"E = mc^2, \alpha > 0",
                        "bbox": [0.1, 0.3, 0.8, 0.4],
                    },
                    {
                        "type": "table",
                        "content": "<table><tr><td>RMSE (mV)</td><td>8</td></tr></table>",
                        "bbox": [0.1, 0.5, 0.8, 0.7],
                        "captions": [{"content": "Table 1. Synthetic evaluation"}],
                        "footnotes": [
                            {"content": "Only evaluated on laboratory data."}
                        ],
                    },
                    {
                        "type": "image",
                        "content": "",
                        "captions": [{"content": "Fig. 1. Synthetic architecture"}],
                    },
                ],
            },
            {"page_idx": 1, "blocks": []},
        ]
    }


def test_source_identity_coordinates_and_parent_notes_survive_cleaning():
    raw = sample()
    original = deepcopy(raw)
    doc = document_from_structured(raw, pdf_sha256="pdf-a")
    assert raw == original
    assert "Synthetic Journal" not in doc["pages"][0]["text"]
    header = doc["blocks"][0]
    assert header["text"] == "Synthetic Journal" and header["excluded_reason"]
    table = next(b for b in doc["blocks"] if b["type"] == "table")
    footnote = next(b for b in doc["blocks"] if b["type"] == "footnote")
    assert table["bbox"] == [0.1, 0.5, 0.8, 0.7]
    assert footnote["parent_id"] == table["id"] and footnote["section_id"] == "p1-b1"
    assert footnote["text"] in doc["pages"][0]["text"]
    assert r"\alpha" in doc["pages"][0]["text"]
    assert "训练需要" in doc["pages"][0]["text"]
    assert doc["pages"][1] == {"page": 2, "text": ""}
    assert any(d["issue"] == "no_readable_text" for d in doc["diagnostics"])
    assert (
        document_from_structured(raw, pdf_sha256="pdf-a")["document_id"]
        == doc["document_id"]
    )
    assert (
        document_from_structured(raw, pdf_sha256="pdf-b")["document_id"]
        != doc["document_id"]
    )
    raw["pages"][0]["blocks"][4]["footnotes"][0]["content"] = "Changed note"
    assert (
        document_from_structured(raw, pdf_sha256="pdf-a")["document_id"]
        != doc["document_id"]
    )


def test_mineru_4_document_title_and_page_footnote_remain_source_evidence():
    doc = document_from_structured(
        {
            "pages": [
                {
                    "page_idx": 0,
                    "blocks": [
                        {"type": "doc_title", "content": "Synthetic paper title"},
                        {
                            "type": "page_footnote",
                            "content": "Synthetic author affiliation",
                        },
                    ],
                }
            ]
        }
    )
    assert doc["sections"][0]["title"] == "Synthetic paper title"
    note = doc["blocks"][1]
    assert note["type"] == "footnote" and note["parent_id"] is None
    assert note["excluded_reason"] is None
    assert note["text"] in doc["pages"][0]["text"]


def test_repeated_margin_text_is_filtered_but_tables_and_captions_are_kept():
    doc = document_from_structured(
        {
            "pages": [
                {
                    "page_idx": i,
                    "blocks": [
                        {
                            "content": "Running title",
                            "type": "text",
                            "bbox": [0.1, 0.01, 0.8, 0.04],
                        },
                        {
                            "content": "Table heading",
                            "type": "table",
                            "bbox": [0.1, 0.01, 0.8, 0.04],
                        },
                        {
                            "content": "Figure caption",
                            "type": "image_caption",
                            "bbox": [0.1, 0.95, 0.8, 0.98],
                        },
                        {"content": f"Body {i}", "bbox": [0.1, 0.2, 0.8, 0.5]},
                    ],
                }
                for i in range(3)
            ]
        }
    )
    assert all("Running title" not in p["text"] for p in doc["pages"])
    assert all(
        "Table heading" in p["text"] and "Figure caption" in p["text"]
        for p in doc["pages"]
    )
    assert len([b for b in doc["blocks"] if b["excluded_reason"]]) == 3


def test_unreadable_table_is_diagnosed_not_relabelled_as_author_omission():
    data = sample()
    data["pages"][0]["blocks"][4]["content"] = ""
    doc = document_from_structured(data)
    assert {
        "page": 1,
        "block_id": "p1-b4",
        "issue": "empty_table",
        "action": "inspect_page_or_reparse",
    } in doc["diagnostics"]
    assert any(b["id"] == "p1-b4" for b in doc["blocks"])


@pytest.mark.parametrize(
    "evidence",
    [
        [{"block_id": "missing", "quote": "fake"}],
        [{"block_id": "p1-b2", "quote": "Invented quotation"}],
        [{"block_id": "p1-b0", "quote": "Synthetic Journal"}],
        [{"block_id": [], "quote": "bad ID"}],
        [],
        None,
    ],
)
def test_invalid_or_excluded_evidence_cannot_pass(evidence):
    assert evidence_errors(evidence, document_from_structured(sample()))


def test_table_context_includes_header_caption_and_note_without_reading_whole_page():
    data = sample()
    data["pages"][0]["blocks"] += [
        {"content": f"Unrelated paragraph {i}"} for i in range(12)
    ]
    doc = document_from_structured(data)
    context = evidence_context([{"block_id": "p1-b4", "quote": "RMSE"}], doc)
    ids = {b["id"] for b in context}
    assert {"p1-b4", "p1-b4-c0", "p1-b4-f0"} <= ids
    assert "p1-b17" not in ids
