# Data Contract

This document defines the inputs and outputs of every public function in
the project. Anything not listed here is private and may change.

---

# Phase 0 — PubMed Tool

## `search_pmids`

`app/tools/pubmed.py`

### Input

| name | type | constraints |
|---|---|---|
| `query` | `str` | non-empty after stripping; PubMed query syntax (e.g. `"TP53 AND breast cancer"`) |
| `max_results` | `int` | `1 <= max_results <= 200` (default `10`) |

### Output

`list[str]` — PMIDs as strings, most-relevant first (ESearch is called with
`sort=relevance`). Empty list when the query matches nothing. The list never
contains duplicates for distinct PMIDs and never exceeds `max_results`.

### Errors

- `ValueError` — blank/non-string query, or invalid `max_results`.
- `PubMedError` — network failure, HTTP non-200 (after bounded retries),
  malformed JSON, NCBI-level error payload, or missing `esearchresult.idlist`.

---

## `fetch_articles`

`app/tools/pubmed.py`

### Input

| name | type | constraints |
|---|---|---|
| `pmids` | `list[str]` | every element a non-empty string; `[]` allowed |

PMIDs are sent to EFetch in batches of 200. An empty list short-circuits and
performs no HTTP call.

### Output

`list[Article]` — one `Article` per PMID that NCBI actually returned.
Unknown PMIDs are dropped silently by NCBI, so the output can be shorter
than the input. Order follows the input order within each batch.

### Errors

- `ValueError` — `pmids` is not a list, or contains a non-string / empty
  string element.
- `PubMedError` — network failure, HTTP non-200 (after bounded retries), or
  malformed XML.

---

## `search_pubmed` (Agent-tool entry point)

### Input / Output

Identical signature to `search_pmids`; returns `list[Article]`. Defined as
`search_pmids(query, max_results)` followed by `fetch_articles(pmids)`; an
empty search returns `[]` without calling EFetch.

### Errors

Union of the two functions above.

---

## `parse_pubmed_xml`

`app/tools/pubmed.py`

### Input / Output

`str` (raw EFetch XML body) → `list[Article]`. Public so tests can exercise
the parser with fixtures; production code calls it only via
`fetch_articles`. Raises `PubMedError` on unparsable XML; missing nodes
inside an otherwise valid document degrade per the table below.

---

## `Article`

`app/models/schemas.py` (stdlib `dataclass`)

| field | type | required | meaning | source in EFetch XML |
|---|---|---|---|---|
| `pmid` | `str` | yes | PubMed unique identifier, primary key of the record | `MedlineCitation/PMID` |
| `title` | `str \| None` | optional | article title, inline markup (`<i>` etc.) stripped | `MedlineCitation/Article/ArticleTitle` |
| `abstract` | `str \| None` | optional | full abstract; structured abstracts keep their section labels (`BACKGROUND: …\nMETHODS: …`) | `…/Abstract/AbstractText` (all parts joined with `\n`) |
| `authors` | `list[str]` | yes (list) | `"ForeName LastName"` per author, or the group name for collective authors; `[]` when there is no author list | `…/AuthorList/Author` (`ForeName`, `LastName`, `CollectiveName`) |
| `journal` | `str \| None` | optional | journal name; full `Title` preferred, then `ISOAbbreviation`, then `MedlineTA` | `…/Journal/Title` → `…/Journal/ISOAbbreviation` → `MedlineCitation/MedlineTA` |
| `publication_year` | `str \| None` | optional | 4-digit year as a string; tries `PubDate/Year`, then the year inside `MedlineDate` (e.g. `"2020 Jan-Feb"` → `"2020"`), then `ArticleDate/Year` (epub-ahead records) | `…/JournalIssue/PubDate`, `…/ArticleDate` |
| `doi` | `str \| None` | optional | DOI without a `https://doi.org/` prefix | `PubmedData/ArticleIdList/ArticleId[@IdType="doi"]` |
| `pubmed_url` | `str` | yes | canonical PubMed page URL, always `https://pubmed.ncbi.nlm.nih.gov/{pmid}/`; derived in `__post_init__`, never read from XML | derived |

Why `str` and not `int` for years: PubMed dates are text ("2023 Jun",
"2020 Jan-Feb"); Phase 0 only displays them, so arithmetic types would add
parsing risk with no benefit.

---

## PubMed JSON output file (`outputs/pubmed_results.json`)

Written by `main.py pubmed`, UTF-8, `indent=2`, `ensure_ascii=False`.

```json
{
  "query": "TP53 AND breast cancer",
  "retrieved_count": 5,
  "articles": [
    {
      "pmid": "36739824",
      "title": "…",
      "abstract": "…",
      "authors": ["Eva Blondeaux", "…"],
      "journal": "Cancer treatment reviews",
      "publication_year": "2023",
      "doi": "10.1016/j.ctrv.2023.102522",
      "pubmed_url": "https://pubmed.ncbi.nlm.nih.gov/36739824/"
    }
  ]
}
```

`retrieved_count == len(articles)`. Optional fields serialize as `null` /
`[]`; `query` is echoed exactly as the caller passed it (unstripped).

---

# Phase 1 — Gene File Parser

## `parse_gene_file`

`app/parsers/gene_file_parser.py`

### Input

| name | type | constraints |
|---|---|---|
| `path` | `str \| Path` | must exist and be one of `.csv`, `.tsv`, `.txt`, `.xlsx` |
| `sheet_name` | `int \| str` | Excel sheet index or name; only used for `.xlsx` (default `0` = first sheet) |

File-format behaviour: `.csv` → comma separator; `.tsv`/`.txt` → tab
separator (a comma-separated `.txt` therefore fails gene-column detection
with an actionable error); `.xlsx` → first sheet via openpyxl. All text
files are read as UTF-8 (BOM-tolerant).

### Output

`GeneParseResult` (see below). `records` holds deduplicated `GeneRecord`s
in first-appearance order.

### Errors

`GeneFileError` (subclass of `RuntimeError`) with a self-diagnosing message
for: missing file, directory input, unsupported suffix, non-UTF-8 text,
unparseable table/Excel file, no recognizable gene column, or an ambiguous
column mapping (more than one column matching the same canonical field).

---

## `GeneRecord`

`app/models/schemas.py` (stdlib `dataclass`)

| field | type | required | meaning | source |
|---|---|---|---|---|
| `gene` | `str` | yes | cleaned gene symbol: stringified, whitespace-stripped, non-empty. **Casing preserved** (mouse `Trp53` must survive); no uppercasing, no ID conversion, no external lookups | gene column cell |
| `logfc` | `float \| None` | optional | log fold change; `None` if the file has no logFC column, the cell is empty/NaN, or the cell is non-numeric / non-finite | logfc column cell |
| `pvalue` | `float \| None` | optional | raw p-value; same missing rules as `logfc`; a value outside `[0, 1]` is invalid → `None` (never clamped) | pvalue column cell |
| `padj` | `float \| None` | optional | adjusted p-value / FDR / q-value; same rules as `pvalue` | padj column cell |

## `GeneParseResult`

`app/parsers/gene_file_parser.py` (stdlib `dataclass`)

| field | type | meaning |
|---|---|---|
| `records` | `list[GeneRecord]` | cleaned, deduplicated records (first occurrence kept) |
| `source_file` | `str` | input path as given |
| `total_rows` | `int` | data rows in the file (excluding header) |
| `valid_gene_rows` | `int` | rows whose gene cell was usable (includes later duplicates) |
| `unique_genes` | `int` | `len(records)` — genes after deduplication |
| `duplicate_gene_count` | `int` | rows dropped because the gene was already seen |
| `missing_gene_count` | `int` | rows with a NaN **or** blank gene cell |
| `invalid_numeric_count` | `int` | statistic cells (in **retained unique-gene rows only**) that were non-numeric strings, infinities, or out-of-range p-values. Rows dropped before numeric parsing — missing gene or duplicate gene — are not inspected, so their cells are never counted |
| `column_mapping` | `dict[str, str]` | canonical field → actual column header (e.g. `{"gene": "GeneSymbol", …}`); only fields that were found |
| `duplicate_gene_names` | `list[str]` | unique names of duplicated genes, in first-duplicate order |

## Column aliases

Column headers are matched after `strip().lower()` — case-insensitive,
whitespace-trimmed, nothing fuzzier. Canonical field → accepted headers
(lowercase form shown; any casing of these works):

| canonical | aliases |
|---|---|
| `gene` | `gene`, `symbol`, `gene_symbol`, `genesymbol` |
| `logfc` | `logfc`, `log2fc`, `log2foldchange`, `avg_logfc`, `avg_log2fc` |
| `pvalue` | `pvalue`, `p_value`, `pval`, `p.value` |
| `padj` | `padj`, `fdr`, `qvalue`, `q_value`, `adj.p.val`, `adjusted_pvalue` |

Ambiguity policy: if two or more columns match the same canonical field
(e.g. both `gene` and `GeneSymbol`, or the case variants `Gene` + `gene` /
`FDR` + `fdr` which collide after normalization), parsing fails with a
`GeneFileError` listing the candidates — the parser never silently chooses.
All original columns are examined individually; nothing is collapsed by
normalized name before matching. Only the gene column is mandatory;
statistics columns are optional.

## Numeric policy

| cell content | result | counted as |
|---|---|---|
| missing column entirely | `None` | — |
| empty / whitespace-only string, NaN | `None` | missing (no counter) |
| valid finite number (incl. scientific notation) | `float(value)` | — |
| non-numeric non-empty string (e.g. `not_available`) | `None` | `invalid_numeric_count++` |
| `inf` / non-finite | `None` | `invalid_numeric_count++` |
| `pvalue` / `padj` outside `[0, 1]` | `None` | `invalid_numeric_count++` |

`logfc` has no range constraint (any finite float is legal). Invalid cells
never abort the parse — the row survives with `None` statistics and the
count is reported in the CLI summary and JSON.

Counting scope: `invalid_numeric_count` applies to **retained unique-gene
rows only**. Duplicate-gene rows (and missing-gene rows) are skipped before
numeric parsing, so invalid cells inside dropped rows are not counted — the
row they belong to is already accounted for in `duplicate_gene_count` /
`missing_gene_count`.

## Duplicate policy

Same gene (after cleaning) → **keep the first occurrence**, drop later rows,
increment `duplicate_gene_count`, record the name once in
`duplicate_gene_names`. No aggregation (no mean logFC / min padj / max
|logFC| / transcript merging): the biologically correct aggregation depends
on how the file was produced (probe-level vs gene-level summaries), so
Phase 1 refuses to guess — a later phase with species/annotation knowledge
can do it explicitly.

## Gene JSON output file (`outputs/gene_records.json`)

Written by `main.py parse`, UTF-8, `indent=2`, `ensure_ascii=False`.

```json
{
  "source_file": "examples/example_deg.csv",
  "summary": {
    "total_rows": 9,
    "valid_gene_rows": 8,
    "unique_genes": 7,
    "duplicate_gene_count": 1,
    "missing_gene_count": 1,
    "invalid_numeric_count": 2
  },
  "column_mapping": {
    "gene": "GeneSymbol",
    "logfc": "log2FoldChange",
    "pvalue": "P.Value",
    "padj": "FDR"
  },
  "duplicate_gene_names": ["EGFR"],
  "genes": [
    {"gene": "TP53", "logfc": 2.1, "pvalue": 0.0005, "padj": 0.001},
    {"gene": "Trp53", "logfc": 1.2, "pvalue": 0.05, "padj": null}
  ]
}
```

`unique_genes == len(genes)`; missing statistics serialize as `null`.

---

# Phase 2 — GLM Client

## `validate_messages`

`app/llm/base.py`

### Input / Output

`object` → `list[dict[str, str]]` (normalized copies: only `role` and
`content` keys, unknown keys dropped).

### Rules

- must be a non-empty `list` of `dict`s;
- `role` must be one of `system` / `user` / `assistant` (no tool role in
  Phase 2);
- `content` must be a non-empty string after stripping;
- violations raise `ValueError` naming the offending index.

## `GLMClient`

`app/llm/glm.py` — implements the `LLMClient` protocol (structural,
`typing.Protocol`; no inheritance).

### Construction

`GLMClient(api_key=None, model=None, timeout=60.0)`. Resolution per
argument: explicit value → environment (`GLM_API_KEY` / `GLM_MODEL`,
optionally from `.env`) → `LLMConfigurationError` (at construction for a
missing key, at `chat()` time for a missing model).

### `chat(messages, *, model=None, temperature=None) -> LLMResponse`

| parameter | rules |
|---|---|
| `messages` | validated by `validate_messages` first |
| `model` | per-call override → client default → `LLMConfigurationError` |
| `temperature` | `None` = omit, API-side default; otherwise a number in `[0, 1]` (out of range / non-numeric → `ValueError`) |

Always non-streaming (`stream=False`). Retries: the ZhipuAI SDK's built-in
retry is pinned to `max_retries=2`; no additional retry loop (rationale in
`docs/phase-02-glm-client.md`).

### Errors

- `LLMConfigurationError` — missing `GLM_API_KEY`, missing model,
  SDK-side authentication rejection (401).
- `LLMRequestError` — network/timeout, rate limit, other SDK failures,
  responses without `choices`, empty assistant `content`, unexpected
  transport exceptions.
- All messages are scrubbed of the API key (`***REDACTED***`).
- Hidden `reasoning_content` is never read or exposed.

## `LLMResponse`

`app/models/schemas.py` (stdlib `dataclass`; extended in Phase 4)

| field | type | required | meaning |
|---|---|---|---|
| `content` | `str \| None` | one of content/tool_calls | the assistant's visible text answer; `None` for tool-call-only answers; non-empty when set (clients enforce) |
| `tool_calls` | `list[ToolCall]` | one of content/tool_calls | tool invocations requested by the model; `[]` for text answers |
| `model` | `str \| None` | optional | model name echoed by the API; falls back to the requested model name |
| `finish_reason` | `str \| None` | optional | e.g. `stop` / `tool_calls`; `None` when omitted |
| `prompt_tokens` | `int \| None` | optional | usage counters; all `None` when the API returns no usage block |
| `completion_tokens` | `int \| None` | optional | |
| `total_tokens` | `int \| None` | optional | |

A valid response has at least one of non-empty `content` or a non-empty
`tool_calls` list — clients raise `LLMRequestError` otherwise. Plus
`to_dict()`. Upper layers consume only this type — never the raw SDK
response object.

---

# Phase 3 — Structured Output

## `SearchIntent`

`app/models/structured.py` (Pydantic v2 `BaseModel`)

| field | type | required | rules |
|---|---|---|---|
| `gene` | `str` | yes | stripped, non-empty; casing preserved; NOT validated against any gene database |
| `disease` | `str \| None` | no | stripped; `""`/blank → `None`; NOT looked up in any ontology |
| `topics` | `list[str]` | no (default `[]`) | each stripped; empties dropped; de-duplicated in first-seen order; ≤ 10 entries **after cleaning**; flat strings only (nested structures rejected) |

Extra/unknown fields are **rejected** (`extra="forbid"`) — the system
prompt forbids the model from adding fields (pathway, pmid, …), and a
validation error keeps that violation observable.

Why Pydantic here (and dataclasses in Phases 0-2): LLM output is external,
untrusted input that must be parsed and validated at runtime before the
project may rely on it; dataclasses for trusted, self-produced records
remain sufficient.

## `chat_structured`

`app/llm/structured.py`

### Input

| name | type | default | meaning |
|---|---|---|---|
| `client` | `LLMClient` | — | any provider client |
| `messages` | `list[dict[str, str]]` | — | validated by the client |
| `schema` | `type[BaseModel]` | — | Pydantic model used for validation |
| `model` | `str \| None` | `None` | forwarded to `client.chat` |
| `temperature` | `float \| None` | `None` | forwarded to `client.chat` |
| `json_mode` | `bool` | `True` | ask the provider for a JSON object where supported (never trusted for validation) |

### Output

`StructuredLLMResult[schema]` — a wrapper dataclass with:

- `data`: the validated Pydantic instance (the trusted object);
- `raw_response`: the full `LLMResponse` (model, finish_reason, token
  usage) for CLI display and the saved JSON. Hidden reasoning content is
  never part of it.

### Errors

`StructuredOutputError` (subclass of `LLMError`), always naming the failed
layer:

- `"Model output is not valid JSON (…); content preview: …"` — layer 1,
  `json.loads` failed (malformed JSON, natural-language reply);
- `"schema validation failed for SearchIntent: <field>: <reason>; …"` —
  layer 2, Pydantic rejected a syntactically valid JSON object.

No repair, no auto-retry (Phase 3 policy).

## `extract_search_intent`

`app/llm/structured.py`

### Input / Output

`(client, text, *, model=None, temperature=0.1, json_mode=True)` →
`StructuredLLMResult[SearchIntent]`. Builds `[system: extraction prompt,
user: text]` using `app/prompts/search_intent.py`. `text` must be a
non-empty string (`ValueError`). The temperature default 0.1 is the single
documented extraction default (deterministic extraction); pass `None` for
the API default.

## `json_mode` semantics

`LLMClient.chat(..., json_mode=True)` is a **request hint**: the provider
is asked to constrain its reply to a JSON object (GLM:
`response_format={"type": "json_object"}`, support verified live — see
`GLM_JSON_MODE_SUPPORTED` in `app/llm/glm.py`). It never validates the
reply; callers needing trustworthy data must use `chat_structured`.

## Search-intent JSON output file (`outputs/search_intent.json`)

Written by `main.py structured`, UTF-8, `indent=2`, `ensure_ascii=False`.

```json
{
  "gene": "TP53",
  "disease": "breast cancer",
  "topics": ["DNA damage", "apoptosis"],
  "model": "glm-4-flash",
  "usage": {"prompt_tokens": 366, "completion_tokens": 34, "total_tokens": 400}
}
```

Validated structured data plus model/usage metadata only — no raw model
output, no hidden reasoning.

---

# Phase 4 — Agent Loop

## `ToolCall`

`app/models/schemas.py` (stdlib `dataclass`)

| field | type | meaning |
|---|---|---|
| `id` | `str` | provider-issued call id; must be echoed verbatim in the matching `role="tool"` message |
| `name` | `str` | requested tool name (must exist in the registry to execute) |
| `arguments` | `str` | **raw JSON string** exactly as the provider returned it — parsing/validation is the tool layer's job, never the provider adapter's |

## Message shapes (`validate_messages`, contract v2)

Four legal dict shapes (unknown keys dropped; `ValueError` with index on
violation):

```text
{"role": "system"|"user", "content": str}                 # content non-empty
{"role": "assistant", "content": str}                     # plain answer
{"role": "assistant", "content": None|str,                # tool-call turn
 "tool_calls": [{"id": str, "name": str, "arguments": str}]}
{"role": "tool", "tool_call_id": str, "content": str}     # tool result
```

Assistant turns need non-empty content **or** a non-empty `tool_calls`
list; each tool call needs non-empty `id`/`name` and string `arguments`;
tool messages need non-empty `tool_call_id` and content. Phase 2/3 callers
(system/user/assistant text) are unaffected.

## `chat()` new parameters

`tools: list[dict] | None = None`, `tool_choice: str | None = None`
(neutral values: `"auto"`, `"none"`). `tools=None` reproduces the exact
Phase 2/3 behaviour — no tool parameters are sent. `GLMClient` converts
neutral tool definitions / messages / tool-calls to ZhipuAI wire format in
both directions and never executes anything.

## `PubMedSearchArgs` / `PUBMED_TOOL_DEFINITION`

`app/agent/tools.py`

| field | type | rules |
|---|---|---|
| `query` | `str` | stripped, non-empty; PubMed query syntax |
| `max_results` | `int` | default 5; `1 <= max_results <= 5` (**agent policy** — the underlying `search_pubmed` stays general up to 200) |

`extra="forbid"`. `PUBMED_TOOL_DEFINITION["function"]["parameters"]` is
generated from `PubMedSearchArgs.model_json_schema()` (single source of
truth; emits `additionalProperties: false`).

## `execute_tool_call` / `ToolResult`

`ToolCall → ToolResult` through the allowlist `TOOL_REGISTRY`
(`{"search_pubmed": execute_pubmed_search}`). Error messages distinguish:
`invalid JSON arguments` → `schema validation failed` → `unknown tool` →
`PubMed execution failed`, all as `ToolExecutionError` (fail fast, no
repair).

`ToolResult.output` is a JSON string:

```json
{"query": "…", "retrieved_count": 2,
 "articles": [{"pmid": "…", "title": "…", "abstract": "…",
               "journal": "…", "publication_year": "…"}]}
```

Projection rules: at most 5 articles (`MAX_ARTICLES_TO_MODEL`); per-article
fields pmid/title/abstract/journal/publication_year only (no authors bulk,
DOI, pubmed_url, HTTP/debug internals); abstracts longer than
`MAX_ABSTRACT_CHARS` (1500) are truncated **with an appended
`[abstract truncated to 1500 characters]` marker** — never silently.
`ToolResult.pmids` carries the actually-retrieved PMIDs (the basis of
`AgentResult.used_pmids`).

## `run_literature_agent` / `AgentResult`

`app/agent/loop.py` — `(client, prompt, *, model=None, temperature=None) →
AgentResult`. Loop: request → if tool_calls, execute every call in the
turn sequentially and append paired messages, continue → first text answer
returns. Hard cap `MAX_AGENT_STEPS = 4` → `AgentLoopError`.

| field | type | meaning |
|---|---|---|
| `answer` | `str` | final model answer |
| `steps` | `int` | LLM requests made (≤ 4) |
| `tool_call_count` | `int` | tool calls executed |
| `used_pmids` | `list[str]` | PMIDs from real tool results (never re-parsed from answer text), deduplicated in retrieval order |
| `model` | `str \| None` | model name from the responses |
| `prompt_tokens` / `completion_tokens` / `total_tokens` | `int \| None` | summed usage across steps (`None` if no step reported usage) |

Errors: `ValueError` (empty prompt), `ToolExecutionError`,
`AgentLoopError`, plus propagated `LLMError` subclasses. Nothing is
persisted between runs.

---

# Environment variables

| name | phase | required | effect |
|---|---|---|---|
| `GLM_API_KEY` | 2 | for `llm` | ZhipuAI API key; checked at `GLMClient` construction |
| `GLM_MODEL` | 2 | for `llm` | default model name; overridable per call / `--model` |
| `NCBI_EMAIL` | 0 | no | passed as `email` to every E-utility call |
| `NCBI_API_KEY` | 0 | no | raises the NCBI rate limit from 3 to 10 requests/second |

Resolution order (all variables): real process environment first; a
project-root `.env` file is read once by `app/config.py` and only fills
variables that are not already set. No credentials are ever hard-coded or
printed.
