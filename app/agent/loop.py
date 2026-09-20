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
    TOOL_DEFINITIONS,
    TOOL_REGISTRY,
    execute_tool_call,
)
from app.llm.base import LLMClient
from app.models.schemas import ToolCall
from app.prompts.literature_agent import LITERATURE_AGENT_SYSTEM_PROMPT

#: Hard cap on LLM requests per run. Since Phase 5 the agent composes up to
#: three tools (NCBI Gene + Reactome + PubMed) sequentially, and a real
#: run already used 3 tool calls / 4 steps; 6 leaves room for final
#: synthesis plus a little extra retrieval while keeping worst-case
#: cost/latency bounded.
MAX_AGENT_STEPS = 6


@dataclass
class AgentResult:
    """Final outcome of one agent run.

    ``used_pmids`` / ``used_gene_ids`` / ``used_reactome_ids`` come from
    the executed tool results themselves — never re-extracted from the
    model's answer text, so they are the trustworthy provenance record of
    which identifiers the agent actually saw.
    """

    answer: str
    steps: int
    tool_call_count: int
    used_pmids: list[str] = field(default_factory=list)
    used_gene_ids: list[str] = field(default_factory=list)
    used_reactome_ids: list[str] = field(default_factory=list)
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
        TOOL_DEFINITIONS[name] for name in TOOL_REGISTRY
    ]

    tool_call_count = 0
    used_pmids: list[str] = []
    used_gene_ids: list[str] = []
    used_reactome_ids: list[str] = []
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
                for result_attr, collected in (
                    ("pmids", used_pmids),
                    ("gene_ids", used_gene_ids),
                    ("reactome_ids", used_reactome_ids),
                ):
                    for identifier in getattr(result, result_attr):
                        if identifier not in collected:
                            collected.append(identifier)
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
                used_gene_ids=used_gene_ids,
                used_reactome_ids=used_reactome_ids,
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
