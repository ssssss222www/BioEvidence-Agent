"""Bounded literature-agent loop (Phase 4).

One iteration = one LLM request. If the model requests tools, every tool
call in that turn is validated and executed sequentially, every result is
appended as a ``role="tool"`` message (IDs paired 1:1 with the assistant's
``tool_calls``), and the loop continues. The first text answer ends the
loop. A hard step budget (:data:`MAX_AGENT_STEPS`) makes runaway loops
impossible — there is no ``while True``.

Agent state in Phase 4 is exactly the ``messages`` history; nothing is
persisted between calls.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.agent.errors import AgentLoopError
from app.agent.tools import (
    PUBMED_TOOL_DEFINITION,
    TOOL_REGISTRY,
    execute_tool_call,
)
from app.llm.base import LLMClient
from app.models.schemas import ToolCall
from app.prompts.literature_agent import LITERATURE_AGENT_SYSTEM_PROMPT

#: Hard cap on LLM requests per run. A literature question needs at most a
#: couple of searches plus a final answer; 4 leaves headroom while keeping
#: worst-case cost/latency bounded.
MAX_AGENT_STEPS = 4


@dataclass
class AgentResult:
    """Final outcome of one agent run.

    ``used_pmids`` comes from the executed tool results themselves — never
    re-extracted from the model's answer text, so it is the trustworthy
    record of which evidence the agent actually saw.
    """

    answer: str
    steps: int
    tool_call_count: int
    used_pmids: list[str] = field(default_factory=list)
    model: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None


def run_literature_agent(
    client: LLMClient,
    prompt: str,
    *,
    model: str | None = None,
    temperature: float | None = None,
) -> AgentResult:
    """Run the bounded literature-agent loop for one user question.

    Raises:
        ValueError: empty prompt.
        ToolExecutionError: invalid/unknown tool call or tool failure
            (fail fast — no auto-repair, no retry, Phase 4 policy).
        AgentLoopError: step budget exhausted without a final answer.
        LLMError subclasses: configuration/request failures propagate.
    """
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt must be a non-empty string")

    messages: list[dict] = [
        {"role": "system", "content": LITERATURE_AGENT_SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]
    tool_definitions = [
        _definition_for(name) for name in TOOL_REGISTRY
    ]

    tool_call_count = 0
    used_pmids: list[str] = []
    usage_total = {"prompt": 0, "completion": 0, "total": 0}
    usage_seen = False
    resolved_model: str | None = None

    for step in range(1, MAX_AGENT_STEPS + 1):
        response = client.chat(
            messages,
            model=model,
            temperature=temperature,
            tools=tool_definitions,
            tool_choice="auto",
        )
        resolved_model = response.model or resolved_model
        for key, attr in (
            ("prompt", "prompt_tokens"),
            ("completion", "completion_tokens"),
            ("total", "total_tokens"),
        ):
            value = getattr(response, attr)
            if value is not None:
                usage_total[key] += value
                usage_seen = True

        if response.tool_calls:
            messages.append(_assistant_tool_call_message(response.tool_calls,
                                                         response.content))
            for tool_call in response.tool_calls:
                result = execute_tool_call(tool_call)  # fail fast on errors
                tool_call_count += 1
                for pmid in result.pmids:
                    if pmid not in used_pmids:
                        used_pmids.append(pmid)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": result.output,
                    }
                )
            continue

        if response.content:
            return AgentResult(
                answer=response.content,
                steps=step,
                tool_call_count=tool_call_count,
                used_pmids=used_pmids,
                model=resolved_model,
                prompt_tokens=usage_total["prompt"] if usage_seen else None,
                completion_tokens=(
                    usage_total["completion"] if usage_seen else None
                ),
                total_tokens=usage_total["total"] if usage_seen else None,
            )

        # Defensive: conforming clients raise before returning such a
        # response; if one slips through we fail loudly rather than loop.
        raise AgentLoopError(
            f"LLM returned neither content nor tool_calls at step {step}"
        )

    raise AgentLoopError(
        f"agent exceeded MAX_AGENT_STEPS ({MAX_AGENT_STEPS}) without a "
        "final answer; increase the budget or narrow the task"
    )


def _assistant_tool_call_message(
    tool_calls: list[ToolCall], content: str | None
) -> dict:
    """Neutral assistant turn preserving every tool call id verbatim."""
    return {
        "role": "assistant",
        "content": content,
        "tool_calls": [
            {"id": call.id, "name": call.name, "arguments": call.arguments}
            for call in tool_calls
        ],
    }


def _definition_for(name: str) -> dict:
    """Tool definition for a registered executor (Phase 4: one tool)."""
    if name != "search_pubmed":  # pragma: no cover - registry has one entry
        raise AgentLoopError(f"no tool definition available for '{name}'")
    return PUBMED_TOOL_DEFINITION
