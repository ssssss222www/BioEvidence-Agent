"""Error taxonomy for the agent layer (Phase 4).

Distinct, catchable failure classes so callers can tell apart:

- configuration problems      → ``LLMConfigurationError`` (app.llm.base)
- LLM request problems        → ``LLMRequestError``       (app.llm.base)
- tool validation/execution   → :class:`ToolExecutionError`
- loop exhaustion             → :class:`AgentLoopError`

No ``except Exception: return "failed"`` anywhere: failures are loud and
carry the layer that produced them.
"""

from __future__ import annotations


class AgentError(RuntimeError):
    """Base class for agent-layer failures (loop, tools)."""


class ToolExecutionError(AgentError):
    """A tool call could not be executed.

    Covers, distinguishable by message prefix:
    - ``invalid JSON arguments`` — the model's arguments were not JSON;
    - ``schema validation failed`` — JSON was valid but violated the tool's
      argument schema;
    - ``unknown tool`` — the requested name is not in the allowlist
      registry (the agent's security boundary);
    - ``PubMed execution failed`` — the underlying tool raised.

    Phase 4 policy: no argument repair, no auto-retry — the failure is
    surfaced to the caller (fail fast).
    """


class AgentLoopError(AgentError):
    """The agent could not produce a final answer within its step budget."""
