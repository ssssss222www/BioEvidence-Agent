"""Data models for BioEvidence Agent.

Core records shared across phases:

- :class:`Article` (Phase 0): normalized form of one PubMed article as
  returned by NCBI EFetch.
- :class:`GeneRecord` (Phase 1): one cleaned gene row from a user-supplied
  differential-analysis / gene-list file.
- :class:`LLMResponse` (Phase 2/4): provider-neutral result of one chat
  completion call — text content and/or tool calls.
- :class:`ToolCall` (Phase 4): one tool invocation requested by the model.
- :class:`GeneInfo` (Phase 5): NCBI Gene record (curated database fact).
- :class:`ReactomePathway` (Phase 5): Reactome pathway mapping for a gene.

Every later phase (LLM planning, evidence ranking, HTML report) will consume
these same shapes, so the models are kept deliberately small and
dependency-free.

Design choice: standard-library ``dataclasses`` instead of Pydantic.
Phases 0-1 perform no request-side validation and no nested model
composition, so Pydantic's only benefit (automatic JSON schema / validation)
would not justify an extra dependency. The parsers in ``app.tools.pubmed``
and ``app.parsers.gene_file_parser`` are the single places that construct
these objects and they already guarantee the "missing field -> None /
empty list" contracts documented below.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class Article:
    """One PubMed article, normalized from an EFetch XML record.

    Field availability contract (PubMed records are heterogeneous):

    ====================  ==========  =========================================
    field                 required    notes
    ====================  ==========  =========================================
    pmid                  yes         PubMed ID, always present in a valid
                                      EFetch record; primary key of a record.
    title                 no          ``None`` if the XML has no
                                      ``ArticleTitle`` (rare, but possible).
    abstract              no          ``None`` for the many records that have
                                      no abstract in PubMed (e.g. older
                                      papers, some reviews, books).
    authors               yes (list)  ``[]`` when the record has no
                                      ``AuthorList``; never ``None``.
    journal               no          ``None`` if no journal title of any
                                      kind can be found.
    publication_year      no          ``None`` when the date is missing or too
                                      unusual to extract a 4-digit year from.
    doi                   no          ``None`` for a large fraction of
                                      records (especially pre-2006 ones).
    pubmed_url            yes         Derived from ``pmid``; always a valid
                                      https link.
    ====================  ==========  =========================================

    ``publication_year`` is a string (not ``int``) on purpose: PubMed dates
    such as "2023 Jun" or Epublish-ahead-of-print years are text, and Phase 0
    only needs to display/sort them, not to do arithmetic.
    """

    pmid: str
    title: str | None
    abstract: str | None
    authors: list[str] = field(default_factory=list)
    journal: str | None = None
    publication_year: str | None = None
    doi: str | None = None
    pubmed_url: str = ""

    def __post_init__(self) -> None:
        # pubmed_url is fully determined by pmid; auto-fill so callers of the
        # dataclass can never produce an inconsistent link.
        if not self.pubmed_url:
            self.pubmed_url = f"https://pubmed.ncbi.nlm.nih.gov/{self.pmid}/"

    def to_dict(self) -> dict:
        """Plain dict for JSON serialization (``json.dumps`` friendly)."""
        return asdict(self)


@dataclass
class GeneRecord:
    """One cleaned gene row from a user-supplied gene/DEG file (Phase 1).

    Field contract:

    ====================  ==========  =========================================
    field                 required    notes
    ====================  ==========  =========================================
    gene                  yes         Cleaned gene symbol: non-empty string,
                                      surrounding whitespace stripped. The
                                      original casing is preserved on purpose
                                      (``Trp53`` is a valid mouse symbol and
                                      must not be forced to ``TRP53``). No ID
                                      conversion, no casing normalization,
                                      no external database lookups.
    logfc                 no          ``None`` when the file has no logFC
                                      column, the cell is empty/NaN, or the
                                      cell holds a non-numeric /
                                      non-finite value.
    pvalue                no          Same missing-value rules as ``logfc``;
                                      additionally a value outside [0, 1] is
                                      treated as invalid and stored as
                                      ``None`` (never clamped).
    padj                  no          Same rules as ``pvalue``.
    ====================  ==========  =========================================

    Numeric policy details (which values count as invalid vs simply missing)
    are implemented and counted in ``app.parsers.gene_file_parser``.
    """

    gene: str
    logfc: float | None = None
    pvalue: float | None = None
    padj: float | None = None

    def to_dict(self) -> dict:
        """Plain dict for JSON serialization (``json.dumps`` friendly)."""
        return asdict(self)


@dataclass
class ToolCall:
    """One tool invocation requested by the model (Phase 4).

    ``arguments`` is kept as the **raw JSON string** the provider returned
    — the provider layer does not parse business arguments. Validation and
    parsing happen in the tool-execution layer
    (``app/agent/tools.py``), the single place that knows the tool schemas.
    """

    id: str
    name: str
    arguments: str


@dataclass
class LLMResponse:
    """Result of one chat completion call, normalized across providers.

    The rest of the project must consume this type — never a provider SDK
    response object (no ``ZhipuAI`` types, no ``choices[0].message.content``
    chains). Field contract:

    ====================  ==========  =========================================
    field                 required    notes
    ====================  ==========  =========================================
    content               no          The assistant's visible text answer.
                                      ``None`` when the model answered with
                                      tool calls only. When set, guaranteed
                                      non-empty (clients enforce this).
    tool_calls            no          Tool invocations the model requested;
                                      ``[]`` for plain-text answers.
    model                 no          Model name as reported by the API;
                                      falls back to the requested model name
                                      when the API does not echo one.
    finish_reason         no          e.g. ``"stop"`` / ``"tool_calls"``;
                                      ``None`` when the API omits it.
    prompt_tokens         no          Usage counters; all ``None`` when the
    completion_tokens     no          API returns no usage block (allowed).
    total_tokens          no
    ====================  ==========  =========================================

    A valid response has **at least one** of non-empty ``content`` or a
    non-empty ``tool_calls`` list — clients raise otherwise. Hidden
    reasoning / chain-of-thought fields (``reasoning_content``) are
    deliberately not part of this contract.
    """

    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    model: str | None = None
    finish_reason: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None

    def to_dict(self) -> dict:
        """Plain dict for JSON serialization (``json.dumps`` friendly)."""
        return asdict(self)


@dataclass
class GeneInfo:
    """One NCBI Gene record (Phase 5) — curated database fact, not literature.

    Source: NCBI E-utilities ESearch (db=gene) + ESummary (db=gene).
    Field availability per real ESummary responses (verified 2026-09-20):

    ====================  ==========  =========================================
    field                 required    notes
    ====================  ==========  =========================================
    gene_id               yes         NCBI GeneID (e.g. "7157"). Distinct from
                                      the *symbol* "TP53" — never conflate.
    symbol                yes         current official symbol in NCBI's casing
                                      (lookups match case-insensitively, but
                                      this value is always the official one;
                                      ``Trp53`` mouse ≠ ``TP53`` human)
    name                  no          full descriptive name (ESummary
                                      ``description``, e.g. "tumor protein p53")
    organism              no          scientific name, e.g. "Homo sapiens"
    tax_id                no          NCBI taxonomy id as string ("9606")
    chromosome            no          e.g. "17"
    map_location          no          cytogenetic location, e.g. "17p13.1"
    aliases               yes (list)  other symbols/aliases; [] when absent
    summary               no          NCBI Gene summary text (can be long;
                                      truncated only at serialization time)
    ncbi_url              yes         derived from gene_id:
                                      https://www.ncbi.nlm.nih.gov/gene/{gene_id}
    ====================  ==========  =========================================
    """

    gene_id: str
    symbol: str
    name: str | None = None
    organism: str | None = None
    tax_id: str | None = None
    chromosome: str | None = None
    map_location: str | None = None
    aliases: list[str] = field(default_factory=list)
    summary: str | None = None
    ncbi_url: str = ""

    def __post_init__(self) -> None:
        if not self.ncbi_url:
            self.ncbi_url = f"https://www.ncbi.nlm.nih.gov/gene/{self.gene_id}"

    def to_dict(self) -> dict:
        """Plain dict for JSON serialization (``json.dumps`` friendly)."""
        return asdict(self)


@dataclass
class ReactomePathway:
    """One Reactome pathway that a gene identifier maps to (Phase 5).

    Source: Reactome Analysis Service ``POST /identifiers/``.
    Field availability per real responses (verified 2026-09-20):

    ====================  ==========  =========================================
    field                 required    notes
    ====================  ==========  =========================================
    stable_id             yes         e.g. "R-HSA-6804754"; always taken from
                                      the real API — never model-generated.
    name                  yes         pathway display name
    species               no          e.g. "Homo sapiens" (pathway species)
    is_disease            no          Reactome ``inDisease`` flag; disease
                                      pathways are included but labelled.
    is_inferred           no          always ``None``: the Analysis Service
                                      does not report an inference flag.
    url                   yes         derived from stable_id:
                                      https://reactome.org/content/detail/{id}
    ====================  ==========  =========================================

    Semantics: "gene maps to pathway" means participation/association in the
    curated Reactome model — NOT a causal claim that the gene regulates the
    pathway.
    """

    stable_id: str
    name: str
    species: str | None = None
    is_disease: bool | None = None
    is_inferred: bool | None = None
    url: str = ""

    def __post_init__(self) -> None:
        if not self.url:
            self.url = f"https://reactome.org/content/detail/{self.stable_id}"

    def to_dict(self) -> dict:
        """Plain dict for JSON serialization (``json.dumps`` friendly)."""
        return asdict(self)
