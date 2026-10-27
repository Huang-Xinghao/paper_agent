"""Collect the current manuscript dependencies, never a recursive paper archive."""

import re
from pathlib import Path


def paper_review_inputs(workspace: Path) -> tuple[Path, ...]:
    root = workspace.resolve()
    selected = {Path("paper/main.pdf")}
    visited = set()
    graphics = [root, root / "paper"]

    def resolve(name, parent, extensions):
        if "\\" in name or "#" in name:
            raise ValueError(f"Cannot resolve dynamic LaTeX dependency: {name}")
        bases = [root, parent, root / "paper", *graphics]
        for base in bases:
            for suffix in ("", *extensions):
                path = (base / (name + suffix)).resolve()
                if not path.is_relative_to(root):
                    continue
                if any(part in {"audit", "backup", "backups", ".recovery"} for part in path.relative_to(root).parts):
                    continue
                if path.is_file():
                    return path
        raise ValueError(f"Missing or excluded LaTeX dependency: {name}")

    def visit(path):
        if path in visited:
            return
        visited.add(path)
        selected.add(path.relative_to(root))
        source = re.sub(r"(?<!\\)%[^\n]*", "", path.read_text(encoding="utf-8"))
        for block in re.findall(r"\\graphicspath\s*\{((?:\s*\{[^{}]*\}\s*)+)\}", source):
            for name in re.findall(r"\{([^{}]*)\}", block):
                graphics.extend([(root / name).resolve(), (path.parent / name).resolve()])
        for command, names in re.findall(
                r"\\(input|include|subfile|bibliography|addbibresource|includegraphics)\*?\s*(?:\[[^\]]*\]\s*)?\{([^{}]+)\}", source):
            for name in names.split(",") if command == "bibliography" else [names]:
                extensions = ((".tex",) if command in {"input", "include", "subfile"}
                              else (".bib",) if command in {"bibliography", "addbibresource"}
                              else (".pdf", ".png", ".jpg", ".jpeg", ".eps"))
                dependency = resolve(name.strip(), path.parent, extensions)
                if dependency.suffix == ".tex":
                    visit(dependency)
                else:
                    selected.add(dependency.relative_to(root))

    visit(root / "paper/main.tex")
    for name in ("PAPER_PLAN.md", "refine-logs/EXPERIMENT_RESULTS.md"):
        if (root / name).is_file():
            selected.add(Path(name))
    return tuple(sorted(selected))
