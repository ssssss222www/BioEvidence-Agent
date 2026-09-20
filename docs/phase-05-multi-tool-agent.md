# Phase 5 — Multi-tool Biomedical Agent: Execution Record

## Goal

Grow the Phase 4 single-tool agent into a genuine **multi-tool** agent:
`get_gene_info` (NCBI Gene) + `get_reactome_pathways` (Reactome) +
`search_pubmed` (PubMed) behind the same allowlist registry, the same one
agent loop, with per-source provenance and honest evidence semantics. Still
no gene-list pipeline, no enrichment, no ranking — those are later phases.

## 1. Why a single-tool agent is not a real tool-selection challenge

With exactly one tool the model has no choice: any tool-shaped answer is
"call search_pubmed". Tool *selection* — matching each part of a question
to the right source — only exists when the catalogue has ≥2 meaningful
alternatives. Phase 5's real test is a question like "explain TP53, its
pathways, and breast-cancer evidence", where the correct behaviour is
three different tools (and *not* calling all three for "what is TP53?").

## 2-4. The three tools' roles

| tool | source | answers | nature |
|---|---|---|---|
| `get_gene_info` | NCBI Gene (E-utilities) | identity: official symbol, GeneID, full name, organism, chromosome, map location, aliases, summary | **curated database fact** |
| `get_reactome_pathways` | Reactome (Analysis Service) | which curated pathways a gene maps to; stable IDs, names, disease flags | **curated database annotation** |
| `search_pubmed` | NCBI PubMed (E-utilities) | published evidence: disease associations, mechanisms, experimental findings | **literature evidence** |

## 5. Database facts vs literature evidence

These are different epistemic categories and the agent must not blur them:

- NCBI Gene / Reactome entries are *curations* — maintained annotations,
  periodically re-reviewed. "Reactome maps TP53 to R-HSA-6804754" is a
  statement about the database's content.
- A PubMed abstract is *one paper's claim*. Evidence strength varies;
  abstract-level reading is shallow by definition.

The system prompt forbids describing database facts as "studies show" and
forbids "the literature indicates" claims without a same-conversation
`search_pubmed` result. The final answer is required to keep sources
distinguishable (GeneID vs R-HSA vs PMID citations).

## 6. Tool selection

Implemented purely via tool descriptions + the system prompt's selection
guide (§13 of the plan). Verified with live runs:

```text
"What is human TP53? Cite the GeneID."          → get_gene_info only
"Which Reactome pathways involve human TP53?"   → get_reactome_pathways only
"Find published evidence linking TP53 to …"     → search_pubmed only
"Explain TP53, its pathways, and evidence…"     → all three (composition)
```

No routing code exists — and none was added: selection is the model's
decision over a well-described catalogue.

## 7. Tool composition

Phase 4's "execute every tool call in the turn, sequentially, append all
results, then re-request" already implements composition; Phase 5 only
widened the registry. The live Case-D run: 4 steps, 3 tool calls
(gene → pathways → pubmed), all three `role="tool"` messages present in the
next request, `tool_call_id` pairing 1:1 throughout.

## 8. Provenance

Every tool result JSON carries a top-level `"source"` tag
(`"PubMed"` / `"NCBI Gene"` / `"Reactome"` — the PubMed tag was added in
this phase as a small compatible change). `ToolResult` carries the
identifier lists the agent actually saw, and `AgentResult` exposes them:

```text
used_pmids        e.g. ["36739824", …]          from real tool results
used_gene_ids     e.g. ["7157"]                 — never regex-parsed from
used_reactome_ids e.g. ["R-HSA-6804754", …]       the model's answer text
```

The CLI prints all three ("Retrieved Gene IDs / Reactome IDs / PMIDs").

## 9. Identifier semantics

Three identifier namespaces are deliberately never conflated:

- gene **symbol** `TP53` — what humans type; case matters (`Trp53` = mouse);
- NCBI **GeneID** `7157` — numeric gene identity within a species;
- Reactome **stable ID** `R-HSA-6804754` — a pathway, not a gene.

All IDs in answers must originate from tool results; the live tests assert
cited IDs ⊆ retrieved IDs per namespace.

## 10. Species ambiguity

`get_gene_info` and `get_reactome_pathways` take an explicit `species`
argument with no implicit default. The tool layer forbids blank species;
the system prompt tells the model to state an assumption explicitly when
the user omitted species, rather than silently assuming human. NCBI
queries use precise field tags — `TP53[sym] AND human[orgn]` — so a bare
"TP53" never hits unrelated records (verified: exactly one result).

## 11. Registry 1 → 3

```python
TOOL_REGISTRY = {"search_pubmed": ..., "get_gene_info": ..., "get_reactome_pathways": ...}
TOOL_DEFINITIONS = {name: definition}   # keyed by registry name
```

Definitions derive their `parameters` from the args models'
`model_json_schema()` (`PubMedSearchArgs`, `GeneInfoArgs`,
`ReactomePathwayArgs`; all `extra="forbid"` → `additionalProperties:
false`). Unknown tools still raise `ToolExecutionError`; still no
eval/globals/dynamic imports.

## 12. Why the agent loop barely changed

Phase 4's loop was written against the *registry*, not against PubMed:
"definitions from registry keys" + "execute every call" +
"collect result ids". Phase 5 changed exactly three things in
`loop.py` — build the catalogue from `TOOL_DEFINITIONS` (replacing the
hard-coded single definition), collect `gene_ids`/`reactome_ids` next to
`pmids`, and expose them on `AgentResult`. No per-tool if/elif anywhere;
that the abstraction held is the point.

## 13. Multi-tool sequence (real Case-D run)

```text
User: "Explain human TP53, its Reactome pathways, and literature evidence
       for its role in breast cancer."
step 1  LLM → tool_calls: get_gene_info(TP53, human)
        registry → GeneInfoArgs ✓ → NCBI ESearch[gene] → ESummary
        tool result {source: "NCBI Gene", gene: {GeneID 7157, …}}
step 2  LLM → tool_calls: get_reactome_pathways(TP53, human)
        registry → ReactomePathwayArgs ✓ → AnalysisService POST "TP53"
        tool result {source: "Reactome", pathways: [R-HSA-…, …]}
step 3  LLM → tool_calls: search_pubmed(TP53 AND breast cancer)
        tool result {source: "PubMed", articles: [PMID …, …]}
step 4  LLM → final answer (facts + pathway IDs + PMIDs, sources distinct)
AgentResult(steps=4, tool_calls=3, used_gene_ids=[7157],
            used_reactome_ids=[R-HSA-6804754, …], used_pmids=[36739824, …])
```

## API choices (verified live, 2026-09-20)

### NCBI Gene — E-utilities ESearch(db=gene) + ESummary(db=gene)

Probe: `TP53[sym] AND human[orgn]` → exactly `["7157"]`; ESummary JSON
provides `name` (symbol), `description`, `organism{scientificname, taxid}`,
`chromosome`, `maplocation`, `summary`, `otheraliases`. ESummary suffices
for every field we use — EFetch's large XML was deliberately not used.
Resolution policy (deterministic): field-tagged search → filter docsums by
exact symbol matched **case-insensitively** (`casefold()`; no substring or
fuzzy matching; species separation comes from the `[orgn]` tag) → exactly
one wins and the returned symbol is NCBI's **official casing** ("tp53" →
"TP53", "trp53"+mouse → "Trp53"); zero → "no record" error; >1 → explicit
ambiguity error listing candidates (never a silent pick).

### Reactome — Analysis Service `POST /AnalysisService/identifiers/`

Chosen over the Content Service: it maps a free-text symbol to an ordered
pathway list in one call (stable IDs, names, species, disease flags,
pageSize), while Content Service endpoints expect already-resolved
Reactome/external IDs and offer no direct symbol→pathway ordered lookup.
Only one implementation is kept. Response: `{"pathwaysFound": <int>,
"pathways": [{stId, name, species{name}, inDisease, entities{pValue…}}]}`
(real TP53 run: 129 pathways found, `pageSize` returned). Order = the
API's analysis-result order, verified stable across identical calls —
preserved as-is. That ordering is simply how the Analysis Service returns
results (its statistical ordering); it is **not** a claim of biological
importance, causal relevance, or regulatory importance. Results are
de-duplicated by `stId` (keep first) and sliced to
`max_results` (1..10). `is_inferred` is not provided by this endpoint and
is honestly `None`. Disease pathways are included but flagged
(`is_disease`).

## Engineering notes

- HTTP style mirrors the PubMed tool (timeout, User-Agent header, bounded
  transient retries, explicit status/parse errors, `NCBIGeneError` /
  `ReactomeError`). The small helper is duplicated on purpose — extracting
  it would have refactored Phase 0's module and broken its test monkeypatch
  seams, which Phase 5 explicitly must not do.
- NCBI identity (tool/email/api_key) stays optional via `app/config.py`;
  no key required.
- Size control: gene summary ≤1200 chars, abstracts ≤1500 chars, pathways
  ≤ `max_results` (≤10), articles ≤5 — every truncation appends an
  explicit `[… truncated to N characters]` marker. Never silent.
- Semantics guardrail: the Reactome tool description, the args-model
  description and the system prompt all state that mapping =
  participation/association, never causal regulation.

## Tests

Offline (no network; fake LLM responses + monkeypatched domain tools):

```text
→ 233 passed, 7 deselected
   (new: 14 NCBI Gene, 16 Reactome, 14 multi-tool/registry/agent;
    updated: Phase 4 agent tests for the renamed definition + source tag)
```

Covers: NCBI exact parse / request-correctness (`TP53[sym] AND human[orgn]`)
/ organism & optional-field tolerance / no-result / exact-symbol filtering /
ambiguity / malformed ESearch & ESummary / network errors; Reactome normal
parse / request shape (pageSize, species, text/plain) / order preservation /
dedup / slicing / bounds / empty / HTTP & JSON errors / missing fields;
registry contents & dispatch / unknown tool / schema-derived definitions /
args validation / summary truncation marker; agent selection (gene /
pathway / literature / mixed with three same-turn calls), provenance lists,
message pairing, catalogue of 3, step-budget regression.

Network (opt-in markers):

- `ncbi_network` — real TP53+human: **PASSED** (symbol `TP53`, digit
  GeneID, organism *Homo sapiens*, summary present).
- `reactome_network` — real TP53+*Homo sapiens*: **PASSED** (≥1 pathway,
  `R-` stable IDs, names, species, ≤3 results).
- `agent_network` multi-tool (live GLM glm-4-flash driving real NCBI +
  Reactome, no PubMed in this test): **PASSED** — model called both tools,
  all arguments passed the Pydantic models, cited `GeneID:` /
  `R-HSA-…` identifiers ⊆ real tool results.
- `agent_network` Phase 4 regression (live GLM + PubMed): **PASSED**.

CLI live examples (glm-4-flash): Case A ("What is human TP53? Cite the
GeneID.") → 1 tool call, GeneID 7157 only; Case D (composition) → 3 tool
calls, 5 Reactome IDs + 5 PMIDs + GeneID, answer separates database facts
from literature evidence.

## Problems encountered

### Problem 1 — leftover half-written helper in a test file

```text
problem   first offline run failed importing test_agent_multi_tool (missing
          export) and a NCBI test contained an unfinished helper function
cause     drafting mistakes (TOOL_REGISTRY not re-exported; test helper left
          mid-refactor)
fix       added the export; deleted the stray helper and wrote the intended
          per-URL fake; also fixed one test whose fake served the same body
          to both endpoints (failure fired at the wrong layer)
result    233 passed offline
```

### Problem 2 — first live multi-tool run was killed externally

```text
problem   the agent_network batch run exited 137 (SIGKILL) before printing
          results
cause     external interruption during the long real-API run, not a test
          failure
fix       re-ran each network test individually
result    all passed (NCBI, Reactome, multi-tool agent, Phase 4 regression)
```

### Problem 3 — glm-5.3 still out of balance (carried from Phase 3)

```text
problem   configured GLM_MODEL has no quota; forced tool_choice also
          unreliable (Phase 4 finding)
fix       live runs used GLM_MODEL=glm-4-flash / --model glm-4-flash;
          multi-tool prompt explicitly names both required tools
result    stable live behaviour; limitations documented
```

## Current limitations

- One gene per gene-tool call; no batch/gene-list workflows, and strictly
  **no enrichment statistics** (Phase 5 boundary).
- Reactome results depend on the Analysis Service's ranking; no local
  relevance re-ranking (that is Phase 6 territory).
- `is_inferred` unavailable from the chosen endpoint (always `None`).
- Species must come from the model/user; a wrong-but-plausible species is
  passed through (validation would need a taxonomy lookup — later phase).
- MAX_AGENT_STEPS=6 (raised from 4 in the Phase 5 review) caps composition depth; a 3-tool question plus one
  refinement already spends most of the budget.
- Live GLM runs still on glm-4-flash (glm-5.3 balance); tool selection
  under `tool_choice="auto"` is probabilistic, so prompts in tests name
  the tools they require.
