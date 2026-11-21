"""Authoritative phase tool permissions; Skills describe how to use these tools."""

from __future__ import annotations

from .core import ToolSpec


PHASE_TOOL_NAMES: dict[str, frozenset[str]] = {
    "wiki": frozenset({"ls", "find", "read", "grep", "write", "edit", "skill",
                       "skill_resource_read", "search_wiki", "read_wiki", "view_image"}),
    "data_search": frozenset({"ls", "find", "read", "grep", "write", "edit", "bash",
                              "skill", "skill_resource_read",
                              "battery_resources",
                              "discover_local_papers", "read_local_paper", "read_paper_pages",
                              "search_wiki", "read_wiki"}),
    "literature": frozenset({"find", "read", "grep", "write", "skill", "skill_resource_read", "sleep",
                              "discover_local_papers", "read_local_paper", "read_paper_pages", "search_wiki", "read_wiki"}),
    "ideas": frozenset({"find", "read", "grep", "write", "edit", "skill", "skill_resource_read", "review_idea",
                         "discover_local_papers", "read_local_paper", "read_paper_pages",
                         "battery_resources", "search_wiki", "read_wiki"}),
    "research_refine": frozenset({"find", "read", "grep", "write", "edit", "bash", "skill", "skill_resource_read",
                            "discover_local_papers", "read_paper_pages", "search_wiki", "read_wiki",
                            "battery_resources"}),
    "experiment_plan": frozenset({"find", "read", "grep", "write", "edit", "skill", "skill_resource_read",
                                   "search_wiki", "read_wiki", "search_papers",
                                   "battery_resources", "inspect_compute"}),
    "experiment_bridge": frozenset({"find", "read", "grep", "write", "edit", "bash", "skill", "skill_resource_read", "sleep", "review_code",
                              "search_wiki", "read_wiki", "battery_resources", "prepare_experiment_template",
                              "prepare_template_run", "experiment_resources", "inspect_compute"}),
    "paper_plan": frozenset({"find", "read", "grep", "write", "edit", "skill", "skill_resource_read", "search_wiki", "read_wiki", "search_papers", "battery_resources"}),
    "paper_figure": frozenset({"find", "read", "grep", "write", "edit", "bash", "skill", "skill_resource_read", "search_wiki", "read_wiki", "generate_image"}),
    "paper_write": frozenset({"find", "read", "grep", "write", "edit", "bash", "skill", "skill_resource_read", "search_wiki", "read_wiki", "search_papers", "battery_resources"}),
    "paper_compile": frozenset({"find", "read", "grep", "write", "edit", "bash", "skill", "skill_resource_read", "search_wiki", "read_wiki", "search_papers"}),
}

PHASE_TOOL_NAMES["improvement"] = (PHASE_TOOL_NAMES["paper_write"] - frozenset({"battery_resources"})) | frozenset({"read_paper_pages"})

for _phase in ("experiment_plan", "experiment_bridge"):
    PHASE_TOOL_NAMES[_phase] |= frozenset({"laboratory_sources", "laboratory_snapshot"})

PHASE_TOOL_NAMES["paper_figure"] |= frozenset({
    "search_figure_references", "view_image", "battery_resources",
})

for _phase in ("paper_figure", "paper_write", "paper_compile", "improvement"):
    PHASE_TOOL_NAMES[_phase] |= frozenset({"view_image"})


def tools_for_phase(phase: str, tools: tuple[ToolSpec, ...]) -> tuple[ToolSpec, ...]:
    """Return only tools allowed for a named workflow phase."""
    try:
        allowed = PHASE_TOOL_NAMES[phase]
    except KeyError as exc:
        raise ValueError(f"unknown paper workflow phase: {phase}") from exc
    return tuple(spec for spec in tools if spec.name in allowed)
