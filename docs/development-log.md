# Development Log

## Phase 5 final review patch (2026-09-20)

Three-point correctness review, no new features:

1. **Reactome wording** — replaced "significance-ranked / ranked pathway
   list" phrasing with "API analysis-result order (its statistical
   ordering; not a claim of biological/causal/regulatory importance)" in
   `app/tools/reactome.py`, `docs/data-contract.md`, and the Phase 5
   record. Endpoint unchanged.
2. **MAX_AGENT_STEPS 4 → 6** (`app/agent/loop.py`) — a real Phase 5 run
   already used 3 tool calls / 4 steps; 6 leaves room for synthesis plus
   extra retrieval while staying a hard cap (still no `while True`).
   Tests updated (exhaustion now expects exactly 6 LLM calls) and a new
   regression asserts a 3-turn multi-tool sequence finishes in 4 ≤ 6
   steps. README/architecture/data-contract updated.
3. **NCBI Gene symbol casing** — resolution now matches the requested
   symbol case-insensitively (`casefold()`), still exact (no
   substring/fuzzy matching), still species-scoped; the returned
   `GeneInfo` always carries NCBI's official ESummary casing. Verified
   live that `[sym]` is case-insensitive server-side (tp53+human → 7157;
   trp53+Mus musculus → 22059). New offline tests cover
   TP53/tp53/Trp53/trp53 combinations plus a casing-only ambiguity case;
   a live `ncbi_network` test asserts lowercase requests return official
   casing.

---

## Phase 5 — Multi-tool Biomedical Agent (2026-09-20, branch phase-05-multi-tool)

One agent, one loop, three tools: NCBI Gene + Reactome + PubMed, with
per-source provenance and facts-vs-evidence semantics. Nothing committed
or pushed in this phase (per instruction).

### Files added

| file | purpose |
|---|---|
| `app/tools/ncbi_gene.py` | ESearch(db=gene)+ESummary lookup, exact-symbol resolution policy, `NCBIGeneError` |
| `app/tools/reactome.py` | Analysis Service mapping, deterministic order/dedup/slice, `ReactomeError` |
| `tests/test_ncbi_gene.py` | 14 offline tests (fixtures mirror the verified real responses) |
| `tests/test_reactome.py` | 16 offline tests |
| `tests/test_agent_multi_tool.py` | registry/selection/composition/provenance offline tests + opt-in `ncbi_network` / `reactome_network` / multi-tool `agent_network` tests |
| `docs/phase-05-multi-tool-agent.md` | teaching-style execution record |

### Files modified

| file | change |
|---|---|
| `app/models/schemas.py` | +`GeneInfo`, `ReactomePathway` dataclasses (fields per verified API responses) |
| `app/agent/tools.py` | +`GeneInfoArgs`/`ReactomePathwayArgs`; tool definitions renamed `SEARCH_PUBMED_TOOL` + new `GET_GENE_INFO_TOOL`/`GET_REACTOME_PATHWAYS_TOOL`; `TOOL_DEFINITIONS` keyed by registry name; registry → 3; `ToolResult` gained `gene_ids`/`reactome_ids`; all payloads tagged `"source"` (incl. PubMed); shared `_parse_tool_arguments` helper; gene-summary truncation |
| `app/agent/loop.py` | catalogue from `TOOL_DEFINITIONS[registry]`; collects `used_gene_ids`/`used_reactome_ids` — no structural change, no per-tool branching |
| `app/prompts/literature_agent.py` | three-tool selection guide, species-assumption rule, facts-vs-evidence semantics, per-namespace citation rules |
| `main.py` | agent output: Retrieved Gene IDs / Reactome IDs / PMIDs |
| `pyproject.toml` | `ncbi_network` / `reactome_network` markers, excluded by default |
| `tests/test_agent_loop.py` | renamed definition imports; tools-catalogue assertion (3 definitions); PubMed payload `source` tag |
| `README.md`, `docs/architecture.md`, `docs/data-contract.md` | Phase 5 updates |

### Why it was designed this way

- **Registry-driven, loop-agnostic.** The Phase 4 loop only needed its
  catalogue source widened; that the abstraction held without per-tool
  branching is the validation of the Phase 4 design.
- **ESummary over EFetch (NCBI)** — verified to already provide every
  field used; **Analysis Service over Content Service (Reactome)** —
  direct symbol→ordered-pathways mapping in one call. Both choices
  recorded with probe evidence; one implementation each.
- **Deterministic resolution** — exact case-insensitive symbol + organism
  field tags; ambiguity is an explicit error, never a silent pick.
- **Provenance from tool outputs only** — `source` tags on results and
  `used_*` id lists; identifiers are never re-parsed from model text.
- **HTTP helper duplicated, not extracted** — avoiding a DRY refactor of
  Phase 0's pubmed module (and its test monkeypatch seams), per phase
  instruction.

### Verification summary

- Baseline before work: 186 passed, clean tree on `phase-05-multi-tool`.
- Offline after implementation (2 test-authoring fixes): **233 passed,
  7 deselected**.
- Live: `ncbi_network` PASSED, `reactome_network` PASSED, multi-tool
  `agent_network` PASSED (glm-4-flash; both tools really called; cited
  GeneIDs/R-HSA IDs ⊆ tool results), Phase 4 PubMed `agent_network`
  regression PASSED. CLI live runs verified tool selection (Case A) and
  three-tool composition (Case D: 3 tool calls, 3 provenance lists).
- One batch network run was killed externally (exit 137, no results
  printed); individual re-runs all passed.

---

## Phase 4 — Agent Loop (2026-09-19)

First real composition: a bounded agent loop where GLM decides when to call
`search_pubmed`, arguments are re-validated in Python, results return to
the model, and the final answer cites only actually-retrieved PMIDs.
Nothing committed or pushed in this phase (per instruction); suggested
commit split at the end of the phase report.

### Files added

| file | purpose |
|---|---|
| `app/agent/__init__.py` | public re-exports |
| `app/agent/errors.py` | `AgentError` / `ToolExecutionError` / `AgentLoopError` |
| `app/agent/tools.py` | `PubMedSearchArgs`, tool definition (schema derived from the model), allowlist `TOOL_REGISTRY`, execution + bounded serialization |
| `app/agent/loop.py` | `run_literature_agent` (MAX_AGENT_STEPS=6), `AgentResult` |
| `app/prompts/literature_agent.py` | agent system prompt (evidence-honesty rules) |
| `tests/test_agent_loop.py` | 37 offline tests + 1 opt-in `agent_network` integration test |
| `docs/phase-04-agent-loop.md` | teaching-style execution record |

### Files modified

| file | change |
|---|---|
| `app/models/schemas.py` | +`ToolCall`; `LLMResponse.content` now `str \| None` and gained `tool_calls` (content-or-tool_calls invariant) |
| `app/llm/base.py` | message contract v2 (4 shapes incl. assistant tool-call + tool result); protocol gains `tools` / `tool_choice`; `LLMRequestError` docstring updated |
| `app/llm/glm.py` | `tools`/`tool_choice` params (auto/none only), neutral↔provider message & tool-call conversion, tool-call response parsing, content-None tolerance |
| `app/llm/structured.py` | `content=None` guard → clean `StructuredOutputError` (never `json.loads(None)`) |
| `main.py` | `agent` subcommand; description/epilog updated |
| `pyproject.toml` | `agent_network` marker, excluded by default |
| `tests/test_glm_client.py`, `tests/test_structured_output.py` | role-contract update ("tool" now valid with `tool_call_id`), +content-None regression test |
| `README.md`, `docs/architecture.md`, `docs/data-contract.md` | Phase 4 updates |

### Why it was designed this way

- **Provider adapter converts, never executes** — `glm.py` only translates
  neutral tools/messages/tool-calls to ZhipuAI wire format; arguments stay
  raw JSON strings until the tool layer parses them once, with Pydantic.
- **Allowlist registry** — the only bridge from model requests to Python;
  unknown tools fail loudly. The security boundary is structural, not
  conventional.
- **Bounded everything** — 6 LLM steps max (raised from 4 in the Phase 5 review); ≤5 articles and 1500-char
  (explicitly marked) abstracts per tool result; `used_pmids` taken from
  tool results, never re-parsed from model text.
- **Fail fast** — malformed arguments / unknown tools / PubMed errors abort
  with distinguishable exceptions (no auto-repair, no retry) so behaviour
  stays observable.

### Verification summary

- Baseline before work: 149 passed, tree clean.
- Offline after implementation (3 test-bug fixes): **186 passed,
  4 deselected**.
- Live probe (ad hoc): SDK v2.1.5 + glm-4-flash accept `tools` and
  `tool_choice="auto"`; `tool_calls` = `[{id, type, function:{name,
  arguments}}]`; forced tool_choice dict accepted but **ineffective** →
  not used, documented.
- Live integration (`pytest -m agent_network`, glm-4-flash): **PASSED** —
  1 real tool call, 5 real PMIDs, answer cites only retrieved PMIDs.
- CLI acceptance: 2 steps, 1 tool call, 5 PMIDs, exit 0 (glm-5.3 still out
  of balance; glm-4-flash used, as in Phase 3).

---

## Phase 3 consistency patch (2026-09-19, pre-Phase 4)

- `main.py`: `structured --temperature` argparse default changed from
  `None` to `DEFAULT_EXTRACTION_TEMPERATURE` (imported from
  `app/llm/structured.py` — no zhipuai in its import chain). Previously an
  omitted flag sent `None`, silently overriding the service's 0.1 default
  while the help text claimed 0.1. The constant is now the single source
  of truth; `%(default)s` renders the real value in help.
- `tests/test_structured_output.py`: two CLI-level regression tests —
  omitted `--temperature` reaches the LLM call as
  `DEFAULT_EXTRACTION_TEMPERATURE`; explicit `--temperature 0.3` passes
  0.3.
- Offline suite after the patch: 149 passed, 3 deselected.
- Docs updated: `docs/phase-03-structured-output.md` (post-phase patch
  section).

---

## Phase 3 — Structured Output (2026-09-19)

### Phase 2 cleanups done first (per plan §0)

- `app/llm/base.py`: `LLMRequestError` docstring aligned with the actual
  implementation (authentication failures — missing key at construction and
  401 at request time — are both `LLMConfigurationError`).
- `main.py`: CLI description/epilog now list all four subcommands with
  examples.

### Files added

| file | purpose |
|---|---|
| `app/models/structured.py` | `SearchIntent` Pydantic schema (strip/None/dedup/limit validators, `extra="forbid"`) |
| `app/llm/structured.py` | `chat_structured` (JSON parse → Pydantic validate), `extract_search_intent`, `StructuredOutputError`, `StructuredLLMResult` wrapper |
| `app/prompts/__init__.py`, `app/prompts/search_intent.py` | extraction-only system prompt as a Python constant |
| `tests/test_structured_output.py` | 36 offline tests + 1 opt-in `llm_network` extraction test |
| `docs/phase-03-structured-output.md` | execution record |

### Files modified

| file | change |
|---|---|
| `app/llm/base.py` | `LLMClient.chat` protocol gains provider-neutral `json_mode: bool = False` |
| `app/llm/glm.py` | `chat(json_mode=True)` sends `response_format={"type":"json_object"}` behind the `GLM_JSON_MODE_SUPPORTED` flag (verified live); rate-limit message now says "rate limit or quota exceeded" after a real 429/1113 (balance) incident showed `APIReachLimitError` covers quota too |
| `main.py` | new `structured` subcommand (`--prompt`, `--model`, `--temperature`, `--output`) |
| `requirements.txt`, `pyproject.toml` | +`pydantic>=2.7` |
| `README.md`, `docs/architecture.md`, `docs/data-contract.md` | Phase 3 updates |

### Why it was designed this way

- **Pydantic enters exactly now** — LLM output is the project's first
  untrusted external data source; dataclasses remain for trusted records.
- **Two distinct validation layers** — "not valid JSON" vs "schema
  validation failed" — both surfaced as `StructuredOutputError` with
  previews/offending fields; no repair, no auto-retry (observable first).
- **`json_mode` stays provider-neutral** — the `response_format` dict
  never leaves `glm.py`; validation never trusts the hint.
- **`StructuredLLMResult` wrapper** — keeps model/finish_reason/usage next
  to the validated data; nothing hidden (reasoning) is persisted.
- **Prompt as a Python module** — versioned, importable by tests, no
  runtime file loading.

### Verification summary

- Offline: 147 passed, 3 deselected (after fixing a fixture scoping bug —
  see phase doc).
- JSON-mode capability verified with a real glm-5.3 request (accepted,
  valid JSON). Later live runs hit the account's balance limit (429/1113)
  on glm-5.3; verified end-to-end on the free-tier `glm-4-flash` instead:
  live extraction test PASSED and the CLI acceptance command produced the
  exact expected SearchIntent (`TP53` / breast cancer / DNA damage +
  apoptosis), saved to `outputs/search_intent.json`.

---

## Phase 2 — GLM Client (2026-09-19)

### Files added

| file | purpose |
|---|---|
| `app/llm/__init__.py` | package marker |
| `app/llm/base.py` | `LLMClient` protocol (`typing.Protocol`), `LLMError` / `LLMConfigurationError` / `LLMRequestError`, `validate_messages` |
| `app/llm/glm.py` | `GLMClient` over the official ZhipuAI SDK (v2 chat completions): config resolution, temperature validation, response normalization, SDK error mapping, API-key redaction |
| `app/config.py` | shared stdlib `.env` loader (moved out of `app/tools/pubmed.py` so GLM vars use the same mechanism) |
| `tests/test_glm_client.py` | 34 offline tests (fake SDK, isolated env) + 1 opt-in `llm_network` smoke test |
| `docs/phase-02-glm-client.md` | execution record |

### Files modified

| file | change |
|---|---|
| `app/models/schemas.py` | added `LLMResponse` dataclass |
| `app/tools/pubmed.py` | uses shared `app.config.load_env_file()`; unreadable `.env` now warns instead of raising `PubMedError` (documented behaviour change in an untested edge path) |
| `main.py` | new `llm` subcommand (`--prompt`, `--system`, `--model`, `--temperature`); GLM import is lazy so `pubmed`/`parse` work without the SDK installed |
| `.env.example` | added `GLM_API_KEY` / `GLM_MODEL` templates |
| `requirements.txt`, `pyproject.toml` | added `zhipuai>=2.1`; new `llm_network` pytest marker excluded by default |
| `README.md`, `docs/architecture.md`, `docs/data-contract.md` | Phase 2 updates (three independent components) |

### Why it was designed this way

- **`typing.Protocol` interface** — structural conformance; no inheritance,
  no base import; test doubles and future `QwenClient`/`DeepSeekClient`
  conform automatically.
- **Messages as `list[dict[str, str]]`** — the exact wire format
  OpenAI-style SDKs accept; `validate_messages` enforces the same rules a
  dataclass would, with one fewer translation layer.
- **Rely on SDK retries (`max_retries=2`), no custom loop** — stacking
  retries multiplies latency and worsens rate limits; auth failures are
  never retried by us.
- **Secret redaction in every error path** — the key is replaced with
  `***REDACTED***` before any exception message is built.
- **`reasoning_content` ignored** — hidden chain-of-thought is not business
  output and must not be persisted or depended on.

### Verification summary

- Offline suite: 111 passed, 2 deselected (both opt-in network tests).
- Real GLM API smoke test: passed (`pytest -m llm_network`).
- Real CLI run: `python main.py llm --prompt "Explain the main biological
  function of TP53 in one sentence."` → model glm-5.3, one-sentence answer,
  usage 34/197/231 tokens, exit 0, no key leaked.

---

## Phase 1 correctness patch (2026-09-19, pre-Phase 2)

- `app/parsers/gene_file_parser.py::_map_columns` — replaced the
  `{normalized: original}` dict with a list of pairs so distinct columns
  that normalize identically (`Gene` + `gene`, `FDR` + `fdr`) both count
  as matches and raise the ambiguity error instead of silently keeping one.
  No behaviour change for all other inputs.
- `invalid_numeric_count` semantics documented explicitly (counts invalid
  cells in retained unique-gene rows only; duplicate/missing rows are
  skipped before numeric parsing): updated `GeneParseResult` docstring,
  `docs/data-contract.md`, `docs/phase-01-gene-file-parser.md`. Code
  behaviour intentionally unchanged.
- `tests/test_gene_file_parser.py` — 3 new parametrized ambiguity
  regression cases (`Gene`+`gene`, `gene`+`GeneSymbol`, `gene`+`FDR`+`fdr`).

---

## Phase 1 — Gene File Parser (2026-09-19)

### Files added

| file | purpose |
|---|---|
| `app/parsers/__init__.py` | package marker for the new parsers layer |
| `app/parsers/gene_file_parser.py` | `parse_gene_file`, `GeneParseResult`, `GeneFileError`, `COLUMN_ALIASES`, cleaning/validation/duplicate policies |
| `examples/example_deg.csv` | intentionally dirty demo file (whitespace, duplicate, missing gene, missing logfc/padj, non-numeric cell, out-of-range p-value) |
| `tests/test_gene_file_parser.py` | 44 offline tests covering file types, aliases, cleaning, numerics, statistics |

### Files modified

| file | change |
|---|---|
| `app/models/schemas.py` | added `GeneRecord` dataclass (+ docstring field contract) |
| `main.py` | **breaking change**: argparse subcommands `pubmed` / `parse`; old `python main.py --query ...` form removed. PubMed behaviour unchanged (`run_pubmed` wraps the same `search_pubmed` call). New parse flow prints summary and writes `outputs/gene_records.json` |
| `requirements.txt`, `pyproject.toml` | added `pandas>=2.0`, `openpyxl>=3.1` |
| `README.md`, `docs/architecture.md`, `docs/data-contract.md` | updated for Phase 1 (two independent workflows, new contracts, breaking-change note) |
| `docs/phase-01-gene-file-parser.md` | added (detailed execution record) |

### Why it was designed this way

- **New `app/parsers/` layer.** Parsing user files is a different concern
  from calling web APIs; a separate package keeps `app/tools/` for
  retrievable knowledge and lets both grow into Agent tools independently.
- **`GeneParseResult` instead of bare `list[GeneRecord]`.** The HTML report
  (Phase 8) must be able to state input rows / valid genes / dropped rows /
  duplicates; losing those statistics now would mean re-parsing later.
- **Deterministic alias table, ambiguity = error.** Column choice determines
  everything downstream, so the parser refuses to guess between e.g.
  `gene` and `GeneSymbol` — it raises with the candidate list.
- **Keep-first duplicates, no aggregation.** Aggregating (mean logFC, min
  padj) silently encodes biological assumptions about probe-vs-gene level
  data that Phase 1 cannot verify; deferring keeps the data honest.
- **Missing vs invalid numerics distinguished.** Empty cell = "not
  measured"; `not_available` / `inf` / p>1 = "measured but broken". Only
  the latter increments `invalid_numeric_count`, and neither aborts.

### Verification summary

- All Phase 0 offline tests still pass unchanged (no Phase 0 code modified
  except `main.py`'s argument layer, which is not unit-tested against the
  old flag form; the `pubmed` subcommand was re-run against the live API).
- `python main.py parse --input examples/example_deg.csv` run for real:
  9 rows → 8 valid → 7 unique genes, 1 duplicate (EGFR), 1 missing gene,
  2 invalid numeric cells; JSON saved to `outputs/gene_records.json`.

### Known limitations (see also phase doc)

- No species detection, symbol validation, or Ensembl ID conversion.
- `.txt` is always tab-separated; other text layouts are not sniffed.
- Text files must be UTF-8 (BOM-tolerant); GBK-only exports are rejected
  with a re-save hint rather than transcoded.

### Next steps (Phase 2, not started)

- GLM client wrapper (config, timeout, error taxonomy) — no agent logic yet.

---

## Phase 0 — PubMed Retrieval (2026-09-19)

### Files added

| file | purpose |
|---|---|
| `app/__init__.py`, `app/models/__init__.py`, `app/tools/__init__.py` | turn `app` into an importable package |
| `app/models/schemas.py` | `Article` dataclass + field-availability contract |
| `app/tools/pubmed.py` | the whole PubMed tool: input validation, NCBI identity, HTTP (timeout + bounded retry), ESearch, EFetch, XML→Article parsing, `search_pubmed()` composition |
| `main.py` | CLI orchestration: argparse, summary printing, JSON saving, exit codes |
| `tests/test_pubmed.py` | 30 offline tests + 1 opt-in network smoke test |
| `pyproject.toml` | project metadata + pytest config (`network` marker, `pythonpath`, default `-m 'not network'`) |
| `requirements.txt` | `requests`, `pytest` |
| `.env.example` | template for optional `NCBI_EMAIL` / `NCBI_API_KEY` |
| `.gitignore` | excludes `.env`, `outputs/*`, caches, virtualenvs |
| `examples/example_queries.txt` | sample queries |
| `outputs/.gitkeep` | keep the (git-ignored) results directory in the repo |
| `README.md` | install/usage/roadmap entry point |
| `docs/architecture.md`, `docs/data-contract.md`, `docs/phase-00-pubmed-retrieval.md`, this file | documentation set |

### Files modified

None (fresh project — no pre-existing work was overwritten; the directory
only contained `prompt/`).

### Why it was designed this way

- **Layering `main.py → tools → models`.** `main.py` contains zero PubMed
  logic so Phase 4 can register `search_pubmed()` as an Agent tool unchanged;
  `schemas.py` contains zero behaviour so every later phase can reuse
  `Article` as its evidence currency.
- **Two functions, two responsibilities.** `search_pmids` answers "which
  documents?" cheaply; `fetch_articles` answers "what is in them?" An Agent
  will need them separately (e.g. count candidates before spending EFetch
  calls), and the split mirrors NCBI's own API.
- **dataclass over Pydantic / no python-dotenv / no frameworks.** Every
  dependency was judged against "does Phase 0 need it?". Answer was no for
  Pydantic (single construction site, no deserialization), dotenv (12-line
  stdlib loader suffices), and LangChain/FastAPI/DB entirely.
- **Degrade, don't crash, on missing fields.** Old PubMed records lack
  abstracts, DOIs, even author lists; the parser maps every missing node to
  `None`/`[]` so one weird record cannot kill a 200-article batch.
- **Bounded, targeted retries.** 3 total attempts, linear backoff, only for
  transient failures; 4xx fails fast with the response snippet in the error.

### Known limitations (see also phase doc)

- Text input only — no gene-list file parsing yet.
- No LLM anywhere in the loop.
- No relevance ranking of our own — order is NCBI's `sort=relevance`.
- Metadata + abstract only — no full text.
- Single-request concurrency; throughput fine for ≤200 articles.

### Next steps (Phase 1, not started)

- Gene-list / differential-analysis file parser producing the same kind of
  validated inputs that `search_pmids` already expects.
