"""Publish all main-paper figures from MinerU caches and archive original PDFs."""

from __future__ import annotations
import argparse
import copy
import hashlib
import json
import re
import shutil
from pathlib import Path

from paper_agent.agent.model_client import load_env_file
from paper_agent.tools.figure_grouping import ANCHOR, LABEL, group_page, union, gap
from paper_agent.tools.paper_figures import read_mineru_paper, save_architecture_figures
from pypdf import PdfReader
import pypdfium2 as pdfium


def prepared_pages(pages):
    """Accept Markdown-wrapped anchors and recover aligned caption continuations."""
    result = copy.deepcopy(pages)
    for page in result:
        blocks = page["blocks"]
        for block in blocks:
            content = block.get("content", "")
            stripped = re.sub(r"^\s*(?:\*\*|__)", "", content)
            if ANCHOR.match(stripped):
                block["content"] = stripped
        anchors = [b for b in blocks if ANCHOR.match(b.get("content", ""))]
        for anchor in anchors:
            box = anchor["bbox"]
            for other in blocks:
                ob = other["bbox"]
                if (
                    other is anchor
                    or other["type"] != "text"
                    or ANCHOR.match(other.get("content", ""))
                ):
                    continue
                if (
                    0 <= ob[0] - box[2] <= 0.06
                    and abs(ob[1] - box[1]) <= 0.012
                    and abs(ob[3] - box[3]) <= 0.03
                ):
                    # Preserve original text; treating it as a caption allows the
                    # existing two-column continuation logic to attach it.
                    other["type"] = "image_caption"
    return result


def attach_visuals(figure, visuals, page, reason, blocks):
    boxes = [v["bbox"] for v in visuals]
    for b in blocks:
        if LABEL.fullmatch(b.get("content", "").strip()) and any(
            gap(b["bbox"], v["bbox"]) <= 0.03 for v in visuals
        ):
            boxes.append(b["bbox"])
        for c in b.get("captions", []):
            if LABEL.fullmatch(c.get("content", "").strip()) and any(
                gap(c["bbox"], v["bbox"]) <= 0.03 for v in visuals
            ):
                boxes.append(c["bbox"])
    box = union(boxes)
    figure.update(
        page=page,
        crop_bbox=[
            max(0, box[0] - 0.008),
            max(0, box[1] - 0.006),
            min(1, box[2] + 0.008),
            min(1, box[3] + 0.003),
        ],
        panel_count=len(visuals),
        source_blocks=[v["source_index"] for v in visuals],
        review_required=True,
    )
    figure["review_reasons"] = [
        r for r in figure["review_reasons"] if r != "no_visual_blocks_above_caption"
    ] + [reason]


def regroup(pages):
    pages = prepared_pages(pages)
    groups, diagnostics = [], []
    for page in pages:
        gs, diag = group_page(page)
        # A single caption and a visually separate, otherwise empty interval
        # establish a recoverable distant-caption case without crossing prose.
        if len(gs) == 1 and not gs[0]["crop_bbox"] and diag["unassigned_visuals"]:
            g = gs[0]
            visuals = diag["unassigned_visuals"]
            box = union([v["bbox"] for v in visuals])
            intervening = [
                b
                for b in page["blocks"]
                if b["type"] in ("text", "paragraph_title")
                and not ANCHOR.match(b.get("content", ""))
                and len(b.get("content", "")) > 35
                and b["bbox"][1] >= box[3]
                and b["bbox"][3] <= g["caption_bbox"][1]
            ]
            if box[3] < g["caption_bbox"][1] and not intervening:
                attach_visuals(
                    g,
                    visuals,
                    page["page_idx"] + 1,
                    "recovered_distant_caption",
                    page["blocks"],
                )
                # MinerU can omit vector-only lower panels entirely. Keep the
                # whole verified figure interval, ending before its caption.
                g["crop_bbox"][3] = g["caption_bbox"][1] - 0.002
                diag["unassigned_visuals"] = []
        groups.append(gs)
        diagnostics.append(diag)
    # Cross-page figures are restricted to an otherwise uncaptioned preceding
    # page and a missing visual at the top of the immediately following page.
    for index in range(1, len(pages)):
        for g in groups[index]:
            prev = diagnostics[index - 1]["unassigned_visuals"]
            if (
                not g["crop_bbox"]
                and g["caption_bbox"][1] < 0.2
                and not groups[index - 1]
                and prev
                and pages[index]["page_idx"] == pages[index - 1]["page_idx"] + 1
            ):
                g["caption_page"] = g["page"]
                attach_visuals(
                    g,
                    prev,
                    pages[index - 1]["page_idx"] + 1,
                    "recovered_cross_page_caption",
                    pages[index - 1]["blocks"],
                )
                diagnostics[index - 1]["unassigned_visuals"] = []
    return [g for gs in groups for g in gs], diagnostics


def main_page_limit(pages):
    """Keep main article/methods/references; exclude appendices and supplements."""
    seen_references = False
    for page in pages:
        headings = [
            b.get("content", "").strip()
            for b in page["blocks"]
            if b["type"] == "paragraph_title"
        ]
        if any(re.match(r"^(?:references|bibliography)$", h, re.I) for h in headings):
            seen_references = True
        if any(
            re.match(
                r"^(?:supplementary (?:information|material)|appendix|[A-Z]\s+APPENDIX)\b",
                h,
                re.I,
            )
            for h in headings
        ):
            return page["page_idx"]
        if seen_references and any(
            re.match(r"^A\s+(?:Reproducibility statement|End-of-life Definition)\b", h)
            for h in headings
        ):
            return page["page_idx"]
    return len(pages)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project", type=Path, default=Path(__file__).resolve().parents[1]
    )
    args = parser.parse_args()
    project = args.project.resolve()
    root = project / "research-wiki"
    load_env_file(project / ".env")
    raw = root / "raw-paper"
    raw.mkdir(exist_ok=True)
    manifest_path = root / "zotero-sync.json"
    manifest = json.loads(manifest_path.read_text())
    report = {
        "scope": "All numbered figures in main papers; supplementary materials and appendices excluded",
        "papers": [],
    }
    raw_records = []
    for source_file in sorted((root / "papers").glob("*.source.json")):
        slug = source_file.name.removesuffix(".source.json")
        source = json.loads(source_file.read_text())
        pdf = project / source["pdf_path"]
        extraction = read_mineru_paper(project, pdf)
        structured = json.loads(
            (Path(extraction["cache"]).parent / "structured_content.json").read_text()
        )
        limit = main_page_limit(structured["pages"])
        # Nature extended-data pages can lack a detected heading or caption.
        # They are not included as unnumbered main-paper visuals below.
        figures, diagnostics = regroup(structured["pages"][:limit])
        figures = [f for f in figures if not f["number"].upper().startswith("S")]
        stage = project / ".paper-agent/wiki-figure-backfill" / slug
        stage.mkdir(parents=True, exist_ok=True)
        (stage / "figures").mkdir(exist_ok=True)
        pdf_doc = pdfium.PdfDocument(pdf)
        for f in figures:
            if not f["crop_bbox"]:
                continue
            pg = pdf_doc[f["page"] - 1]
            im = pg.render(scale=2.5).to_pil().convert("RGB")
            pg.close()
            x0, y0, x1, y1 = f["crop_bbox"]
            w, h = im.size
            name = f"figures/page_{f['page']}_figure_{f['number']}.png"
            im.crop((round(x0 * w), round(y0 * h), round(x1 * w), round(y1 * h))).save(
                stage / name
            )
            f["image"] = name
        # Preserve a genuine graphical abstract, without assigning it a figure
        # number or inventing a scientific caption.
        unnumbered = []
        for diag in diagnostics:
            if diag["page"] != 1 or not diag["unassigned_visuals"]:
                continue
            for i, v in enumerate(diag["unassigned_visuals"], 1):
                pg = pdf_doc[0]
                im = pg.render(scale=2.5).to_pil().convert("RGB")
                pg.close()
                x0, y0, x1, y1 = v["bbox"]
                w, h = im.size
                name = f"figures/page_1_unnumbered_{i}.png"
                im.crop(
                    (round(x0 * w), round(y0 * h), round(x1 * w), round(y1 * h))
                ).save(stage / name)
                unnumbered.append(
                    {
                        "number": f"unnumbered {i}",
                        "page": 1,
                        "image": name,
                        "caption": "Original unnumbered visual on PDF page 1. No numbered caption was detected.",
                        "review_required": True,
                        "review_reasons": ["unnumbered_visual_no_caption"],
                    }
                )
        pdf_doc.close()
        original = pdf
        recovery = source["source"].get("pdf_recovery", {})
        if recovery.get("original_total_pages") and recovery.get("original_pdf_pages"):
            candidate = (
                project
                / "papers/zotero-recovery"
                / f"{source['source']['item_key']}.pdf"
            )
            if not candidate.is_file():
                raise FileNotFoundError("Original full PDF is missing")
            original = candidate
        raw_path = raw / f"{slug}.pdf"
        shutil.copyfile(original, raw_path)
        sha = hashlib.sha256(raw_path.read_bytes()).hexdigest()
        assert sha == hashlib.sha256(original.read_bytes()).hexdigest()
        paths = save_architecture_figures(
            root, slug, {"figures": figures + unnumbered, "cache": str(stage)}
        )
        for f in figures + unnumbered:
            if not f.get("image"):
                continue
            caption = root / "figures" / slug / (Path(f["image"]).stem + ".md")
            with caption.open("a") as stream:
                stream.write(f"Raw PDF: [original paper](../../raw-paper/{slug}.pdf)\n")
                if f.get("caption_page"):
                    stream.write(f"Caption PDF page: {f['caption_page']}\n")
        page_lines = [
            f"# {slug}",
            "",
            f"[Original PDF](../../raw-paper/{slug}.pdf)",
            "",
            f"Extraction scope: main article PDF pages 1–{limit}; supplementary material excluded.",
            "",
        ]
        for f in figures + unnumbered:
            if f.get("image"):
                name = Path(f["image"]).name
                page_lines += [
                    f"## Figure {f['number']} · PDF page {f['page']}",
                    "",
                    f'![Figure {f["number"]}]({name})',
                    "",
                    f["caption"],
                    "",
                ]
                if f["review_required"]:
                    page_lines += [
                        "Review required: " + ", ".join(f["review_reasons"]),
                        "",
                    ]
        (root / "figures" / slug / "README.md").write_text("\n".join(page_lines))
        identity = f"{source['source']['library_type']}/{source['source']['library_id']}/{source['source']['item_key']}"
        added = [str(Path(p).relative_to(project)) for p in paths] + [
            str(raw_path.relative_to(project))
        ]
        manifest[identity]["outputs"] = list(
            dict.fromkeys(manifest[identity]["outputs"] + added)
        )
        record = {
            "paper_id": f"paper:{slug}",
            "item_key": source["source"]["item_key"],
            "raw_pdf": str(raw_path.relative_to(root)),
            "sha256": sha,
            "original_pdf_pages": len(PdfReader(raw_path).pages),
            "analyzed_pdf_pages": extraction["pages"][-1]["page"],
            "figure_scope_pages": [1, limit],
        }
        raw_records.append(record)
        item = {
            "slug": slug,
            "main_page_limit": limit,
            "numbered_figures": len([f for f in figures if f.get("image")]),
            "unnumbered_visuals": len(unnumbered),
            "figures": figures,
            "missing_numbered_crops": [
                {"number": f["number"], "page": f["page"]}
                for f in figures
                if not f.get("image")
            ],
            "review_required": [
                {
                    "number": f["number"],
                    "page": f["page"],
                    "reasons": f["review_reasons"],
                }
                for f in figures + unnumbered
                if f["review_required"]
            ],
        }
        report["papers"].append(item)
        print(
            slug,
            item["numbered_figures"],
            "numbered",
            len(unnumbered),
            "unnumbered",
            "missing",
            item["missing_numbered_crops"],
            flush=True,
        )
    report["numbered_figures"] = sum(p["numbered_figures"] for p in report["papers"])
    report["unnumbered_visuals"] = sum(
        p["unnumbered_visuals"] for p in report["papers"]
    )
    report["raw_pdfs"] = len(raw_records)
    (raw / "manifest.json").write_text(
        json.dumps(raw_records, ensure_ascii=False, indent=2) + "\n"
    )
    (raw / "README.md").write_text(
        "# Original paper PDFs\n\nOriginal downloaded PDFs, named by Wiki paper identifier. Supplementary pages, if bundled in an original file, are retained in this archive but excluded from figure extraction.\n\n"
        + "\n".join(
            f"- [{r['paper_id']}]({Path(r['raw_pdf']).name}) — {r['original_pdf_pages']} PDF pages"
            for r in raw_records
        )
        + "\n"
    )
    (root / "figures/README.md").write_text(
        "# Paper figures\n\nAll numbered main-paper figures, with original captions. Additional unnumbered visuals and uncertain crops are marked for review. Supplementary material and appendices are excluded.\n\n"
        + "\n".join(
            f"- [{p['slug']}]({p['slug']}/README.md) — {p['numbered_figures']} figures"
            for p in report["papers"]
        )
        + "\n"
    )
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    (root / "figure-backfill-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )
    print(
        "TOTAL",
        report["numbered_figures"],
        "numbered",
        report["unnumbered_visuals"],
        "unnumbered",
        report["raw_pdfs"],
        "PDFs",
        flush=True,
    )


if __name__ == "__main__":
    main()
