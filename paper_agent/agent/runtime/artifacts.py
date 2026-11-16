"""File selection for human review snapshots (no version ledger)."""
from paper_agent.agent.stages import BY_NODE


def node_files(project, node):
    paths = set(BY_NODE[node].review_paths)
    patterns = {'paper_figure': ('figures/*.pdf', 'figures/*.png', 'figures/*.svg', 'figures/*.jpg', 'figures/*.jpeg'),
                'paper_write': ('paper/**/*.tex', 'paper/**/*.bib')}.get(node, ())
    for pattern in patterns:
        paths.update(p.relative_to(project).as_posix() for p in project.glob(pattern) if p.is_file())
    return paths


def tracked_files(project):
    return set().union(*(node_files(project, node) for node in BY_NODE))
