# Architecture

## Overview (Phase 3)

The project contains four **independent** capabilities that are deliberately
not connected yet:

Component 1 (Phase 0) — PubMed retrieval:

```text
main.py (pubmed)
   ↓
search_pubmed()                (high-level tool, app/tools/pubmed.py)
   ↓
search_pmids()
   ↓
ESearch  (esearch.fcgi, JSON)  — query → PMID list
   ↓
fetch_articles()
   ↓
EFetch   (efetch.fcgi, XML)    — PMID list → raw article XML
   ↓
parse_pubmed_xml() → Article   (app/models/schemas.py)
   ↓
JSON saved by main.py          (outputs/pubmed_results.json)
```

Component 2 (Phase 1) — gene file parsing:

```text
main.py (parse)
   ↓
parse_gene_file(path)          (app/parsers/gene_file_parser.py)
   ↓
_read_dataframe()              pandas: .csv/.tsv/.txt/.xlsx → DataFrame
   ↓
_map_columns()                 deterministic alias table → column mapping
   ↓
_clean_gene() / _parse_stat_cell()   cleaning + numeric validation
   ↓
duplicate policy (keep first)
   ↓
GeneRecord[]  + GeneParseResult      (statistics for report/audit)
   ↓
JSON saved by main.py          (outputs/gene_records.json)
```

Component 3 (Phase 2) — GLM chat client:

```text
main.py (llm)
   ↓
GLMClient                      (app/llm/glm.py, implements LLMClient protocol)
   ↓
validate_messages()            (app/llm/base.py, provider-neutral)
   ↓
ZhipuAI SDK  chat.completions.create(stream=False)
   ↓
_normalize_response()
   ↓
LLMResponse                    (app/models/schemas.py)
```

Component 4 (Phase 3) — structured output extraction:

```text
main.py (structured)
   ↓
extract_search_intent()         (app/llm/structured.py)
   ↓
SEARCH_INTENT_SYSTEM_PROMPT     (app/prompts/search_intent.py, extraction-only)
   ↓
GLMClient.chat(json_mode=True)  → response_format={"type": "json_object"}
   ↓
json.loads()                    layer 1: JSON syntax
   ↓
SearchIntent.model_validate()   layer 2: Pydantic schema (app/models/structured.py)
   ↓
StructuredLLMResult             validated SearchIntent + raw LLMResponse
   ↓
JSON saved by main.py           (outputs/search_intent.json)
```

The data contracts `Article`, `GeneRecord`, `LLMResponse` and
`SearchIntent` are the currencies that later phases (LLM planning, evidence
ranking, HTML report) will join together — but not before Phase 4.

## Directory layout

```text
literature_agent/               project root (repo root; see note below)
├── app/                        importable Python package
│   ├── config.py               shared .env loader (NCBI + GLM variables)
│   ├── llm/
│   │   ├── base.py             LLMClient protocol, error taxonomy, message validation
│   │   ├── glm.py              GLMClient over the official ZhipuAI SDK (+ json_mode)
│   │   └── structured.py       chat_structured / extract_search_intent (Phase 3)
│   ├── models/
│   │   ├── schemas.py          Article + GeneRecord + LLMResponse dataclasses
│   │   └── structured.py       SearchIntent Pydantic schema (LLM-validated data)
│   ├── prompts/
│   │   └── search_intent.py    extraction-only system prompt (Phase 3)
│   ├── parsers/
│   │   └── gene_file_parser.py gene/DEG file parser (Phase 1)
│   └── tools/
│       └── pubmed.py           PubMed tool: validation, HTTP, parsing, retries
├── docs/                       architecture / contracts / dev log / phase notes
├── examples/
│   ├── example_queries.txt     sample PubMed queries
│   └── example_deg.csv         intentionally dirty gene file (Phase 1 demo)
├── outputs/                    generated results (git-ignored except .gitkeep)
├── tests/
│   ├── test_pubmed.py          Phase 0 tests + opt-in NCBI network smoke test
│   ├── test_gene_file_parser.py Phase 1 offline tests (tmp_path fixtures)
│   ├── test_glm_client.py      Phase 2 offline tests + opt-in GLM smoke test
│   └── test_structured_output.py Phase 3 offline tests + opt-in extraction test
├── main.py                     CLI entry point (orchestration only, subcommands)
├── .env.example                template for NCBI_* and GLM_* variables
├── .gitignore                  ignores .env, outputs/*, caches, venvs
├── requirements.txt            requests + pandas + openpyxl + zhipuai + pydantic + pytest
└── pyproject.toml              metadata + pytest config (markers, pythonpath)
```

Note: the Phase 0 specification sketched the root folder as
`literature-agent/`; the repository itself is already the project root
(`literature_agent`), so files live directly at the root instead of in an
extra nested folder.

## Module responsibilities and relationships

| Module | Responsibility | Depends on |
|---|---|---|
| `app/models/schemas.py` | Define `Article`, `GeneRecord`, `LLMResponse` and their missing-field contracts | nothing (stdlib only) |
| `app/models/structured.py` | `SearchIntent` — Pydantic schema for LLM-generated (untrusted) data | `pydantic` |
| `app/config.py` | Optional stdlib `.env` loader shared by the PubMed tool and GLM client | nothing (stdlib only) |
| `app/llm/base.py` | Provider-neutral `LLMClient` protocol (incl. `json_mode`), `LLMError` taxonomy, `validate_messages` | `schemas.py` |
| `app/llm/glm.py` | `GLMClient`: SDK wiring, model/temperature/json_mode resolution, response normalization, secret redaction | `base.py`, `config.py`, `schemas.py`, `zhipuai` |
| `app/llm/structured.py` | `chat_structured` (JSON parse → Pydantic validate) and `extract_search_intent`; `StructuredOutputError` | `base.py`, `structured.py` (models), `prompts` |
| `app/prompts/search_intent.py` | The extraction-only system prompt, as a versioned Python constant | nothing |
| `app/tools/pubmed.py` | Query validation, NCBI identity, HTTP with timeout/retry, ESearch/EFetch calls, XML→`Article` parsing | `schemas.py`, `config.py`, `requests` |
| `app/parsers/gene_file_parser.py` | File-type dispatch, column alias mapping, gene cleaning, numeric validation, duplicate policy, parse statistics | `schemas.py`, `pandas`, `openpyxl` (via pandas) |
| `main.py` | argparse subcommands (`pubmed`, `parse`, `llm`, `structured`), call the components, print summaries, save JSON, map errors to exit codes | the modules above |
| `tests/` | Offline tests with fakes (SDK/HTTP monkeypatched, files in `tmp_path`) + opt-in network smoke tests | everything above |

Dependency direction is strictly
`main.py → app.llm / app.tools.pubmed / app.parsers.gene_file_parser → app.models.*`.
The models know nothing about I/O; the client/tool/parser modules know
nothing about CLI/JSON files; `main.py` knows nothing about XML, pandas, or
SDK response shapes. Upper layers depend on `app/llm/base.py` +
`LLMResponse`/`SearchIntent`, never on `ZhipuAI` types — that is what
makes a future `QwenClient`/`DeepSeekClient` a drop-in addition.

## Key design decisions

- **dataclass for trusted data, Pydantic for LLM data.** `Article` /
  `GeneRecord` / `LLMResponse` describe data our own code produced from
  trusted parsers — dataclasses suffice. `SearchIntent` describes data
  **generated by an LLM** — external, untrusted input that needs runtime
  JSON parsing, schema validation and field constraints, hence Pydantic v2
  (`app/models/structured.py`, introduced Phase 3).
- **`json_mode` is provider-neutral.** Callers say `json_mode=True`;
  `GLMClient` alone knows that this means `response_format={"type":
  "json_object"}` (support verified live; see the flag constant in
  `glm.py`). Validation never trusts the hint — `chat_structured` always
  parses + validates.
- **Extraction, not interpretation.** The `SearchIntent` prompt forbids
  inferring biology the user did not state; the schema rejects extra fields
  (`extra="forbid"`) so violations are loud, not silently dropped.
- **No repair, no retry (Phase 3 policy).** Invalid JSON and schema
  violations raise `StructuredOutputError` immediately, with distinct
  messages per layer. No regex JSON fixing, no auto-retry — model behaviour
  must stay observable before we optimize it.
- **`typing.Protocol` for `LLMClient`, not ABC.** The interface is
  structural ("has a `chat` method with this shape"): conforming needs no
  inheritance and no base-class import, which keeps test doubles and future
  providers trivially compatible.
- **`list[dict[str, str]]` messages, not a ChatMessage dataclass.** It is
  exactly the wire format every OpenAI-style SDK (including ZhipuAI)
  accepts; `validate_messages` provides the same guarantees a dataclass
  would, with one fewer translation layer.
- **Deterministic column mapping, no LLM.** Column recognition is a fixed
  lowercase/trimmed alias table (`COLUMN_ALIASES`). Ambiguous matches
  (e.g. both `gene` and `GeneSymbol`, or `Gene` + `gene`) raise instead of
  silently picking a column.
- **Keep-first duplicate policy (Phase 1).** No biological aggregation: the
  right aggregation depends on the analysis design and doing it silently
  would destroy information. See `docs/phase-01-gene-file-parser.md`.
- **Missing ≠ invalid (Phase 1).** Empty cells/NaN count as *missing*;
  unparseable strings, infinities and out-of-range p-values count as
  *invalid* and are never clamped.
- **Bounded retries.** Phase 0 HTTP: own loop, 3 attempts, transient-only.
  Phase 2 GLM: the official SDK already retries (`max_retries=2` pinned);
  no second retry layer on top — stacked retries multiply latency.
- **stdlib `.env` mini-loader (`app/config.py`) instead of python-dotenv.**
  Real environment variables always win over the file; the loader is
  shared by NCBI and GLM configuration. Avoids a dependency for a
  convenience feature.
- **Secret redaction.** `GLMClient` scrubs the API key from every error
  message it raises, so exceptions can be printed safely.

## Error-handling model

- Bad user input (blank query, `max_results` out of 1..200, invalid
  messages, temperature outside [0, 1]) → `ValueError`; `main.py` maps it
  to exit code 2 (argparse usage errors are also 2).
- Missing GLM key/model or unreadable configuration →
  `LLMConfigurationError` → exit code 1 (`[llm config error]`).
- File/parse failures → `GeneFileError`; PubMed network/response failures →
  `PubMedError`; GLM request/response failures → `LLMRequestError`; LLM
  output that is invalid JSON or fails schema validation →
  `StructuredOutputError` — all user-readable, mapped to exit code 1.
- Missing data inside otherwise valid payloads (no abstract, no DOI, blank
  gene cell, absent usage block) is **not** an error — it degrades to
  `None` per the field contracts.

## Extension points for later phases

- `search_pubmed(query, max_results)`, `parse_gene_file(path)` and
  `GLMClient.chat(messages)` are all Agent-tool-shaped entry points;
  Phase 4 can register them directly.
- `app/llm/base.py` is the seam for additional providers
  (`QwenClient`, `DeepSeekClient`) — implement `chat`, conform to the
  protocol.
- `GeneParseResult` statistics exist so the Phase 8 HTML report can state
  input rows / valid genes / dropped rows / duplicates.
- `_ncbi_params()` already threads `email`/`api_key` into every request, so
  higher-volume phases can raise the rate limit without code changes.
