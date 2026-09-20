"""Agent layer (Phases 4-5): bounded loop + allowlisted tools.

Public API re-exported here for convenient imports::

    from app.agent import AgentResult, run_literature_agent
"""

from app.agent.errors import AgentError, AgentLoopError, ToolExecutionError
from app.agent.loop import AgentResult, run_literature_agent
from app.agent.tools import (
    MAX_ABSTRACT_CHARS,
    MAX_ARTICLES_TO_MODEL,
    MAX_SUMMARY_CHARS,
    GET_GENE_INFO_TOOL,
    GET_REACTOME_PATHWAYS_TOOL,
    SEARCH_PUBMED_TOOL,
    TOOL_DEFINITIONS,
    TOOL_REGISTRY,
    GeneInfoArgs,
    PubMedSearchArgs,
    ReactomePathwayArgs,
    ToolResult,
    execute_get_gene_info,
    execute_get_reactome_pathways,
    execute_pubmed_search,
    execute_tool_call,
)

__all__ = [
    "AgentError",
    "AgentLoopError",
    "AgentResult",
    "MAX_ABSTRACT_CHARS",
    "MAX_ARTICLES_TO_MODEL",
    "MAX_SUMMARY_CHARS",
    "GET_GENE_INFO_TOOL",
    "GET_REACTOME_PATHWAYS_TOOL",
    "SEARCH_PUBMED_TOOL",
    "TOOL_DEFINITIONS",
    "TOOL_REGISTRY",
    "GeneInfoArgs",
    "PubMedSearchArgs",
    "ReactomePathwayArgs",
    "ToolExecutionError",
    "ToolResult",
    "execute_get_gene_info",
    "execute_get_reactome_pathways",
    "execute_pubmed_search",
    "execute_tool_call",
    "run_literature_agent",
]
