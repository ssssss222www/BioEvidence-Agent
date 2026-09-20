"""PubMed tool for the agent: schema, definition, registry, execution.

Flow (Phase 4)::

    ToolCall.arguments (raw JSON string from the provider)
    → json.loads()                     malformed → ToolExecutionError
    → PubMedSearchArgs.model_validate  schema violation → ToolExecutionError
    → search_pubmed(query, max_results)  PubMed/network error → ToolExecutionError
    → Article[]
    → safe serialization (limited fields, bounded sizes)
    → JSON string for the role="tool" message

The allowlist registry (:data:`TOOL_REGISTRY`) is the agent's security
boundary: only registered Python functions can ever run. No ``eval``, no
``globals()``, no dynamic imports, no model-chosen code.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.agent.errors import ToolExecutionError
from app.models.schemas import Article, ToolCall
from app.tools.pubmed import PubMedError, search_pubmed

#: Agent policy: at most this many articles are returned to the model.
#: The underlying search_pubmed() stays general (up to 200) — the cap
#: lives here, in the agent's tool layer.
MAX_ARTICLES_TO_MODEL = 5

#: Abstracts longer than this are truncated *with an explicit marker*
#: appended to the text (never silently). Rationale: keep tool messages
#: small enough for multi-step conversations while preserving the fact
#: that content was cut.
MAX_ABSTRACT_CHARS = 1500

_ARGUMENTS_PREVIEW_CHARS = 120


class PubMedSearchArgs(BaseModel):
    """Validated arguments for the ``search_pubmed`` tool.

    ``max_results`` is capped at 5 by agent policy (not by the underlying
    PubMed tool, which allows up to 200 for direct CLI/library use).
    """

    model_config = ConfigDict(extra="forbid")

    query: str
    max_results: int = Field(default=5, ge=1, le=5)

    @field_validator("query")
    @classmethod
    def _clean_query(cls, value: str) -> str:
        query = value.strip()
        if not query:
            raise ValueError("query must be a non-empty string after stripping")
        return query


#: OpenAI-style tool definition shown to the model. The JSON-schema part is
#: derived from PubMedSearchArgs so there is exactly one source of truth
#: (Pydantic v2 emits ``additionalProperties: false`` for
#: ``extra="forbid"`` models).
PUBMED_TOOL_DEFINITION: dict = {
    "type": "function",
    "function": {
        "name": "search_pubmed",
        "description": (
            "Search PubMed for biomedical literature. Use this whenever "
            "literature evidence is needed. 'query' is a PubMed query "
            "string, e.g. \"TP53 AND breast cancer\". 'max_results' is the "
            "number of articles to return (1-5, default 5)."
        ),
        "parameters": PubMedSearchArgs.model_json_schema(),
    },
}


@dataclass
class ToolResult:
    """Outcome of one executed tool call.

    ``output`` is the JSON string placed verbatim into the role="tool"
    message; ``pmids`` are the article PMIDs the agent actually saw, used
    to build the trustworthy ``AgentResult.used_pmids`` list (never
    re-extracted from model text).
    """

    output: str
    pmids: list[str] = field(default_factory=list)


def execute_pubmed_search(tool_call: ToolCall) -> ToolResult:
    """Validate arguments, run the PubMed tool, serialize the result."""
    try:
        raw_arguments = json.loads(tool_call.arguments)
    except json.JSONDecodeError as exc:
        raise ToolExecutionError(
            f"invalid JSON arguments for '{tool_call.name}' "
            f"({exc.msg} at line {exc.lineno} column {exc.colno}); raw: "
            f"{tool_call.arguments[:_ARGUMENTS_PREVIEW_CHARS]!r}"
        ) from exc
    try:
        args = PubMedSearchArgs.model_validate(raw_arguments)
    except ValidationError as exc:
        raise ToolExecutionError(
            f"schema validation failed for '{tool_call.name}' arguments: "
            f"{_format_validation_error(exc)}"
        ) from exc
    try:
        articles = search_pubmed(args.query, args.max_results)
    except (PubMedError, ValueError) as exc:
        raise ToolExecutionError(
            f"PubMed execution failed for '{tool_call.name}': {exc}"
        ) from exc
    payload = {
        "query": args.query,
        "retrieved_count": len(articles),
        "articles": [
            _article_payload(article)
            for article in articles[:MAX_ARTICLES_TO_MODEL]
        ],
    }
    return ToolResult(
        output=json.dumps(payload, ensure_ascii=False),
        pmids=[article.pmid for article in articles[:MAX_ARTICLES_TO_MODEL]],
    )


#: Allowlist registry — the only functions an LLM tool call can ever reach.
TOOL_REGISTRY: dict[str, Callable[[ToolCall], ToolResult]] = {
    "search_pubmed": execute_pubmed_search,
}


def execute_tool_call(tool_call: ToolCall) -> ToolResult:
    """Dispatch a tool call through the allowlist registry.

    Raises:
        ToolExecutionError: ``unknown tool`` when the name is not
            registered — this is the security boundary; nothing outside
            :data:`TOOL_REGISTRY` can be executed by the model.
    """
    executor = TOOL_REGISTRY.get(tool_call.name)
    if executor is None:
        raise ToolExecutionError(
            f"unknown tool {tool_call.name!r}; available tools: "
            f"{sorted(TOOL_REGISTRY)}"
        )
    return executor(tool_call)


def _article_payload(article: Article) -> dict:
    """Model-facing projection of an Article: necessary fields only.

    Deliberately excluded: authors (bulk), DOI, pubmed_url, and any HTTP /
    debug internals — the model needs evidence, not transport details.
    """
    abstract = article.abstract
    if abstract and len(abstract) > MAX_ABSTRACT_CHARS:
        abstract = (
            abstract[:MAX_ABSTRACT_CHARS]
            + f"\n[abstract truncated to {MAX_ABSTRACT_CHARS} characters]"
        )
    return {
        "pmid": article.pmid,
        "title": article.title,
        "abstract": abstract,
        "journal": article.journal,
        "publication_year": article.publication_year,
    }


def _format_validation_error(exc: ValidationError) -> str:
    details = []
    for error in exc.errors()[:5]:
        location = ".".join(str(part) for part in error["loc"]) or "(root)"
        details.append(f"{location}: {error['msg']}")
    return "; ".join(details)
