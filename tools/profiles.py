"""Phase-specific tool visibility for the paper workflow."""

from __future__ import annotations

from .core import ToolSpec


PHASE_TOOL_NAMES: dict[str, frozenset[str]] = {
    "literature": frozenset({"find", "read", "grep", "write", "skill", "skill_resource_read", "sleep",
                              "discover_local_papers", "read_local_paper", "read_paper_pages", "search_papers", "download_arxiv_paper", "download_openalex_paper", "search_wiki", "read_wiki"}),
    "ideas": frozenset({"find", "read", "grep", "write", "edit", "skill", "skill_resource_read",
                         "discover_local_papers", "read_local_paper", "search_papers",
                         "search_battery_resources", "read_battery_resource", "search_wiki", "read_wiki"}),
    "novelty": frozenset({"find", "read", "grep", "write", "skill", "skill_resource_read",
                           "discover_local_papers", "read_local_paper", "search_papers",
                           "search_battery_resources", "read_battery_resource", "search_wiki", "read_wiki"}),
    "research_refine": frozenset({"find", "read", "grep", "write", "edit", "bash", "skill", "skill_resource_read", "skill_script_run",
                            "discover_local_papers", "search_papers", "search_wiki", "read_wiki",
                            "search_battery_resources", "read_battery_resource"}),
    "experiment_plan": frozenset({"find", "read", "grep", "write", "edit", "skill", "skill_resource_read",
                                   "search_papers", "search_wiki", "read_wiki",
                                   "search_battery_resources", "read_battery_resource"}),
    "experiment_bridge": frozenset({"find", "read", "grep", "write", "edit", "bash", "skill", "skill_resource_read", "skill_script_run", "sleep",
                              "search_wiki", "read_wiki"}),
    "writing": frozenset({"find", "read", "grep", "write", "edit", "bash", "skill", "skill_resource_read", "skill_script_run", "search_wiki", "read_wiki"}),
    "paper_plan": frozenset({"find", "read", "grep", "write", "edit", "skill", "skill_resource_read", "search_papers", "search_wiki", "read_wiki"}),
    "paper_figure": frozenset({"find", "read", "grep", "write", "edit", "bash", "skill", "skill_resource_read", "search_wiki", "read_wiki", "generate_image"}),
    "paper_write": frozenset({"find", "read", "grep", "write", "edit", "bash", "skill", "skill_resource_read", "search_papers", "search_wiki", "read_wiki"}),
    "paper_compile": frozenset({"find", "read", "grep", "edit", "bash", "skill", "skill_resource_read", "search_wiki", "read_wiki"}),
}

# Runner-owned image generation uses read-only preparation/review subphases.
for _phase in ("paper_figure_design", "paper_figure_review"):
    PHASE_TOOL_NAMES[_phase] = frozenset({"find", "read", "grep"})

for _phase in ("paper_figure", "paper_write", "paper_compile"):
    PHASE_TOOL_NAMES[_phase] |= frozenset({"view_image"})


for _phase in ("ideas", "novelty", "research_refine",
               "experiment_plan", "experiment_bridge"):
    PHASE_TOOL_NAMES[_phase] |= frozenset({
        "search_battery_resources", "read_battery_resource",
        "list_battery_data_sources", "inspect_battery_data",
    })


def tools_for_phase(phase: str, tools: tuple[ToolSpec, ...]) -> tuple[ToolSpec, ...]:
    """Return only tools allowed for a named workflow phase."""
    phase = {"proposal": "refine", "experiment": "experiment_bridge"}.get(phase, phase)
    phase = {"refine": "research_refine"}.get(phase, phase)
    try:
        allowed = PHASE_TOOL_NAMES[phase]
    except KeyError as exc:
        raise ValueError(f"unknown paper workflow phase: {phase}") from exc
    return tuple(spec for spec in tools if spec.name in allowed)
