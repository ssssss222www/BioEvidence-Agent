"""Agent layer (Phase 4): bounded loop + allowlisted tools.

Public API re-exported here for convenient imports::

    from app.agent import AgentResult, run_literature_agent
"""

from app.agent.errors import AgentError, AgentLoopError, ToolExecutionError
from app.agent.loop import AgentResult, run_literature_agent
from app.agent.tools import (
    MAX_ABSTRACT_CHARS,
    MAX_ARTICLES_TO_MODEL,
    PUBMED_TOOL_DEFINITION,
    PubMedSearchArgs,
    ToolResult,
    execute_pubmed_search,
    execute_tool_call,
)

__all__ = [
    "AgentError",
    "AgentLoopError",
    "AgentResult",
    "MAX_ABSTRACT_CHARS",
    "MAX_ARTICLES_TO_MODEL",
    "PUBMED_TOOL_DEFINITION",
    "PubMedSearchArgs",
    "ToolExecutionError",
    "ToolResult",
    "execute_pubmed_search",
    "execute_tool_call",
    "run_literature_agent",
]
