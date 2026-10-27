"""Keep workflow unit tests independent of the live literature service."""
import pytest

from paper_agent.tools import ToolResult


@pytest.fixture(autouse=True)
def offline_workflow_search(monkeypatch):
    # Retrieval tests replace this with their own deterministic responses.
    monkeypatch.setattr('paper_agent.agent.workflow.search_papers',
                        lambda *args, **kwargs: ToolResult('Offline test environment', is_error=True))
