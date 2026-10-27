#!/usr/bin/env python3
"""Build a self-contained, offline research wiki viewer (standard library only)."""
from __future__ import annotations

import argparse
import base64
import json
import re
from pathlib import Path


def read_page(path: Path, root: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    match = re.match(r"\A---\r?\n(.*?)\r?\n---\r?\n(.*)", text, re.S)
    meta = {}
    body = text
    if match:
        body = match[2]
        parent = None
        for line in match[1].splitlines():
            if not line.strip() or ":" not in line:
                continue
            key, value = line.strip().split(":", 1)
            value = value.strip()
            try:
                value = json.loads(value)
            except (ValueError, TypeError):
                value = value.strip("\"'")
            if line.startswith(" ") and parent:
                meta[parent][key] = value
            else:
                meta[key] = value if value != "" else {}
                parent = key if value == "" else None
    return {**meta, "path": path.relative_to(root).as_posix(), "body": body}


def build(root: Path, output: Path) -> dict:
    nodes = []
    for directory in ("papers", "ideas", "experiments", "claims"):
        for path in sorted((root / directory).glob("*.md")):
            page = read_page(path, root)
            if page.get("node_id"):
                nodes.append(page)
    # Embed only referenced local assets so the exported HTML remains portable.
    for node in nodes:
        images = {}
        for link in re.findall(r"!\[[^\]]*\]\(([^\s)]+)\)", node["body"]):
            asset = (root / node["path"]).parent.joinpath(link).resolve()
            if (asset.is_relative_to((root / "assets").resolve()) and asset.is_file()
                    and asset.suffix.lower() in {".png", ".jpg", ".jpeg"}
                    and asset.stat().st_size <= 10 * 1024 * 1024):
                mime = "image/png" if asset.suffix.lower() == ".png" else "image/jpeg"
                images[link] = f"data:{mime};base64," + base64.b64encode(asset.read_bytes()).decode("ascii")
        node["images"] = images
    ids = {node["node_id"] for node in nodes}
    edges, unresolved = [], []
    edge_path = root / "graph/edges.jsonl"
    if edge_path.exists():
        for line in edge_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                edge = json.loads(line)
                (edges if edge.get("from") in ids and edge.get("to") in ids else unresolved).append(edge)
    documents = [read_page(root / name, root) for name in ("index.md", "log.md", "query_pack.md") if (root / name).exists()]
    data = {"nodes": nodes, "edges": edges, "documents": documents, "unresolved": unresolved}
    template = Path(__file__).with_name("wiki_visualizer").joinpath("template.html").read_text(encoding="utf-8")
    # A wiki page must never be able to terminate the embedded JSON script element.
    payload = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(template.replace("__WIKI_DATA__", payload), encoding="utf-8")
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wiki", nargs="?", type=Path, default=Path(__file__).resolve().parents[1] / "research-wiki")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    root = args.wiki.resolve()
    output = args.output.resolve() if args.output else root / "visualization.html"
    data = build(root, output)
    print(f"Created {output}: {len(data['nodes'])} nodes, {len(data['edges'])} edges")
    if data["unresolved"]:
        print(f"Note: {len(data['unresolved'])} edges have missing endpoints and are listed in the viewer.")


if __name__ == "__main__":
    main()
