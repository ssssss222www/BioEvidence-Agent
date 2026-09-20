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
from app.models.schemas import Article, GeneInfo, ReactomePathway, ToolCall
from app.tools.ncbi_gene import NCBIGeneError, get_gene_info
from app.tools.pubmed import PubMedError, search_pubmed
from app.tools.reactome import ReactomeError, get_reactome_pathways

#: Agent policy: at most this many articles are returned to the model.
#: The underlying search_pubmed() stays general (up to 200) — the cap
#: lives here, in the agent's tool layer.
MAX_ARTICLES_TO_MODEL = 5

#: Abstracts longer than this are truncated *with an explicit marker*
#: appended to the text (never silently). Rationale: keep tool messages
#: small enough for multi-step conversations while preserving the fact
#: that content was cut.
MAX_ABSTRACT_CHARS = 1500

#: NCBI Gene summaries longer than this are truncated with the same
#: explicit-marker policy as abstracts.
MAX_SUMMARY_CHARS = 1200

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


class GeneInfoArgs(BaseModel):
    """Validated arguments for the ``get_gene_info`` tool.

    ``species`` is required — the agent passes the user's explicit species
    context; no silent cross-species default.
    """

    model_config = ConfigDict(extra="forbid")

    gene: str
    species: str

    @field_validator("gene", "species")
    @classmethod
    def _clean_non_empty(cls, value: str, info) -> str:
        text = value.strip()
        if not text:
            raise ValueError(f"{info.field_name} must be non-empty after stripping")
        return text


class ReactomePathwayArgs(BaseModel):
    """Validated arguments for the ``get_reactome_pathways`` tool."""

    model_config = ConfigDict(extra="forbid")

    gene: str
    species: str
    max_results: int = Field(default=5, ge=1, le=10)

    @field_validator("gene", "species")
    @classmethod
    def _clean_non_empty(cls, value: str, info) -> str:
        text = value.strip()
        if not text:
            raise ValueError(f"{info.field_name} must be non-empty after stripping")
        return text


#: OpenAI-style tool definitions shown to the model. Each JSON-schema part
#: is derived from the matching Pydantic args model so there is exactly one
#: source of truth (Pydantic v2 emits ``additionalProperties: false`` for
#: ``extra="forbid"`` models).
SEARCH_PUBMED_TOOL: dict = {
    "type": "function",
    "function": {
        "name": "search_pubmed",
        "description": (
            "Search PubMed for biomedical literature. Use this for published "
            "evidence: disease associations, mechanisms, experimental "
            "findings, or any claim that needs literature support. 'query' "
            "is a PubMed query string, e.g. \"TP53 AND breast cancer\". "
            "'max_results' is the number of articles to return (1-5, "
            "default 5)."
        ),
        "parameters": PubMedSearchArgs.model_json_schema(),
    },
}

GET_GENE_INFO_TOOL: dict = {
    "type": "function",
    "function": {
        "name": "get_gene_info",
        "description": (
            "Get curated NCBI Gene database facts for one gene: official "
            "symbol, GeneID, full name, organism, chromosome, map location, "
            "aliases, and the NCBI summary. Use for gene identity and "
            "annotation questions, not for published evidence. 'species' "
            "must state the organism (e.g. 'human' or 'Homo sapiens'); do "
            "not assume a species the user did not state."
        ),
        "parameters": GeneInfoArgs.model_json_schema(),
    },
}

GET_REACTOME_PATHWAYS_TOOL: dict = {
    "type": "function",
    "function": {
        "name": "get_reactome_pathways",
        "description": (
            "Get the Reactome pathways a gene maps to (participates in / is "
            "associated with) in the curated Reactome model. Returns pathway "
            "stable IDs (R-…), names, species, and disease flags. Mapping "
            "means participation, NOT causal regulation. 'species' must "
            "state the organism. 'max_results' limits pathways returned "
            "(1-10, default 5)."
        ),
        "parameters": ReactomePathwayArgs.model_json_schema(),
    },
}

#: Tool definitions keyed by registry name. The agent loop derives the
#: catalogue from TOOL_REGISTRY keys, so registry and definitions can never
#: drift apart.
TOOL_DEFINITIONS: dict[str, dict] = {
    "search_pubmed": SEARCH_PUBMED_TOOL,
    "get_gene_info": GET_GENE_INFO_TOOL,
    "get_reactome_pathways": GET_REACTOME_PATHWAYS_TOOL,
}


@dataclass
class ToolResult:
    """Outcome of one executed tool call.

    ``output`` is the JSON string placed verbatim into the role="tool"
    message (its top level always carries a ``"source"`` provenance tag).
    The id lists collect what the agent actually saw — PMIDs, NCBI
    GeneIDs, Reactome stable IDs — and feed the trustworthy
    ``AgentResult.used_*`` lists (never re-extracted from model text).
    """

    output: str
    pmids: list[str] = field(default_factory=list)
    gene_ids: list[str] = field(default_factory=list)
    reactome_ids: list[str] = field(default_factory=list)


def _parse_tool_arguments(tool_call: ToolCall, args_model: type[BaseModel]):
    """Shared argument pipeline: raw JSON string → validated model."""
    try:
        raw_arguments = json.loads(tool_call.arguments)
    except json.JSONDecodeError as exc:
        raise ToolExecutionError(
            f"invalid JSON arguments for '{tool_call.name}' "
            f"({exc.msg} at line {exc.lineno} column {exc.colno}); raw: "
            f"{tool_call.arguments[:_ARGUMENTS_PREVIEW_CHARS]!r}"
        ) from exc
    try:
        return args_model.model_validate(raw_arguments)
    except ValidationError as exc:
        raise ToolExecutionError(
            f"schema validation failed for '{tool_call.name}' arguments: "
            f"{_format_validation_error(exc)}"
        ) from exc


def execute_pubmed_search(tool_call: ToolCall) -> ToolResult:
    """Validate arguments, run the PubMed tool, serialize the result."""
    args = _parse_tool_arguments(tool_call, PubMedSearchArgs)
    try:
        articles = search_pubmed(args.query, args.max_results)
    except (PubMedError, ValueError) as exc:
        raise ToolExecutionError(
            f"PubMed execution failed for '{tool_call.name}': {exc}"
        ) from exc
    payload = {
        "source": "PubMed",
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


def execute_get_gene_info(tool_call: ToolCall) -> ToolResult:
    """Validate arguments, run the NCBI Gene tool, serialize the result."""
    args = _parse_tool_arguments(tool_call, GeneInfoArgs)
    try:
        info = get_gene_info(args.gene, args.species)
    except (NCBIGeneError, ValueError) as exc:
        raise ToolExecutionError(
            f"NCBI Gene execution failed for '{tool_call.name}': {exc}"
        ) from exc
    payload = {
        "source": "NCBI Gene",
        "query": {"gene": args.gene, "species": args.species},
        "gene": _gene_payload(info),
    }
    return ToolResult(
        output=json.dumps(payload, ensure_ascii=False),
        gene_ids=[info.gene_id],
    )


def execute_get_reactome_pathways(tool_call: ToolCall) -> ToolResult:
    """Validate arguments, run the Reactome tool, serialize the result."""
    args = _parse_tool_arguments(tool_call, ReactomePathwayArgs)
    try:
        pathways = get_reactome_pathways(
            args.gene, args.species, max_results=args.max_results
        )
    except (ReactomeError, ValueError) as exc:
        raise ToolExecutionError(
            f"Reactome execution failed for '{tool_call.name}': {exc}"
        ) from exc
    payload = {
        "source": "Reactome",
        "query": {
            "gene": args.gene,
            "species": args.species,
            "max_results": args.max_results,
        },
        "pathways": [
            {
                "stable_id": pathway.stable_id,
                "name": pathway.name,
                "species": pathway.species,
                "is_disease": pathway.is_disease,
                "is_inferred": pathway.is_inferred,
                "url": pathway.url,
            }
            for pathway in pathways
        ],
        "retrieved_count": len(pathways),
    }
    return ToolResult(
        output=json.dumps(payload, ensure_ascii=False),
        reactome_ids=[pathway.stable_id for pathway in pathways],
    )


#: Allowlist registry — the only functions an LLM tool call can ever reach.
TOOL_REGISTRY: dict[str, Callable[[ToolCall], ToolResult]] = {
    "search_pubmed": execute_pubmed_search,
    "get_gene_info": execute_get_gene_info,
    "get_reactome_pathways": execute_get_reactome_pathways,
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


def _gene_payload(info: GeneInfo) -> dict:
    """Model-facing projection of a GeneInfo (with bounded summary)."""
    summary = info.summary
    if summary and len(summary) > MAX_SUMMARY_CHARS:
        summary = (
            summary[:MAX_SUMMARY_CHARS]
            + f"\n[summary truncated to {MAX_SUMMARY_CHARS} characters]"
        )
    return {
        "gene_id": info.gene_id,
        "symbol": info.symbol,
        "name": info.name,
        "organism": info.organism,
        "tax_id": info.tax_id,
        "chromosome": info.chromosome,
        "map_location": info.map_location,
        "aliases": info.aliases,
        "summary": summary,
        "ncbi_url": info.ncbi_url,
    }


def _format_validation_error(exc: ValidationError) -> str:
    details = []
    for error in exc.errors()[:5]:
        location = ".".join(str(part) for part in error["loc"]) or "(root)"
        details.append(f"{location}: {error['msg']}")
    return "; ".join(details)
