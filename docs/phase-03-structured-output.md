# Phase 3 — Structured Output: Execution Record

## Goal

Turn natural-language user intent into a **validated** structured object:

```text
"I want to investigate TP53 in breast cancer, focusing on DNA damage and apoptosis."
→ GLM → JSON → schema validation → SearchIntent
```

This is still not an Agent — no tools are called, no loop runs, nothing is
retrieved. It is the extraction capability a future agent will use to
understand what the user wants.

## Natural language vs JSON

Humans and LLMs converse in natural language; programs need structure.
"TP53 in breast cancer, focusing on DNA damage" is easy for a person but
useless to code: which part is the gene? the disease? free-text programs
would need fragile string surgery. An agreed **JSON** shape
(`{"gene": ..., "disease": ..., "topics": [...]}`) gives every downstream
component an exact handle. Phase 3's job is to cross that boundary safely.

## JSON ≠ Schema (two validation layers)

Being valid JSON is **not** the same as being a valid `SearchIntent`:

```text
"{gene: TP53}"                          → not even valid JSON (layer 1 fails)
'{"genes": "TP53"}'                     → valid JSON, wrong schema (layer 2 fails)
'{"gene": "TP53", "pathway": "p53"}'    → valid JSON, forbidden extra field (layer 2 fails)
'{"gene": "", "topics": ["a","a"]}'     → valid JSON, constraint violation (layer 2 fails)
```

Layer 1 is `json.loads` (syntax). Layer 2 is Pydantic (field types,
constraints, unknown fields). Both failures raise the same exception type —
`StructuredOutputError` — but the message always names the layer, and
Phase 3 deliberately does not repair or retry: broken model output must be
observable before we decide what to fix. There is **no** regex "JSON
repair" (fence stripping, quote replacement, brace completion) — see §8 of
the plan.

## Pydantic — LLM → JSON → Pydantic → trusted Python object

Pydantic v2 turns a parsed JSON object into a checked Python object:

```text
LLM output (text, untrusted)
    ↓ json.loads            — is it JSON at all?
    ↓ SearchIntent.model_validate — right fields? right types? constraints met?
    ↓
SearchIntent instance — code can rely on .gene being a non-empty stripped str,
                       .disease being str | None, .topics being a clean list
```

Without this step, a typo like `"genes"` or `"TP53 "` (with spaces) would
travel silently into query construction and poison every later phase.

## Why Pydantic starts now (and not in Phase 0)

Phases 0-2 used plain dataclasses because **we constructed every record
ourselves** from trusted, deterministic parsers (NCBI XML, our CSV reader,
our GLM adapter) — a dataclass documents the shape, and the single
construction site guarantees it. Phase 3 introduces the project's first
**externally generated** data: model text we did not produce and cannot
trust. Runtime validation (parse + schema + constraints) is exactly what
dataclasses don't do and Pydantic does — that is the reason for the new
`pydantic>=2.7` dependency, kept in a separate module
(`app/models/structured.py`) so the trusted-data modules stay
dependency-light.

## SearchIntent — fields and boundaries

| field | type | rules |
|---|---|---|
| `gene` | `str` | required; stripped; non-empty; **casing preserved** (`Trp53` stays `Trp53`); not validated against any gene database |
| `disease` | `str \| None` | optional; stripped; blank → `None`; no disease-ontology lookup |
| `topics` | `list[str]` | default `[]`; items stripped; empties dropped; de-duplicated keeping first-seen order; **≤ 10 after cleaning**; flat strings only |
| extra fields | — | **rejected** (`extra="forbid"`) |

The boundaries are the point: this is **extraction, not biological
interpretation**. The system prompt (below) forbids the model from adding
diseases, pathways, PMIDs, gene functions or evidence the user never
mentioned — and the schema enforces it by rejecting unknown fields loudly.
No pathway/PMID/function fields exist because the user's text cannot
honestly supply them.

## The prompt

`app/prompts/search_intent.py` (chosen over a `prompts/*.txt` folder:
prompts are code — versioned, imported by tests, no runtime file I/O or
path resolution; the CLI stays orchestration-only). It states: extraction
only; do not infer missing biology; output one JSON object; expected
fields with null/[] policy; no markdown or extra text; plus one worked
example.

## JSON mode — verified capability (not guessed)

| item | value |
|---|---|
| SDK | `zhipuai` v2.1.5.20250725 (`chat.completions.create` has a `response_format` parameter) |
| verification | real minimal request on 2026-09-19 with `response_format={"type": "json_object"}` |
| model | `glm-5.3` (`GLM_MODEL` in local `.env`) |
| accepted? | **yes** — HTTP 200, no error |
| valid JSON? | **yes** — content `{"gene":"TP53","disease":"breast cancer"}` parsed cleanly (usage 99/85/184 tokens) |
| enabled in code? | **yes** — `GLM_JSON_MODE_SUPPORTED = True` in `app/llm/glm.py`; `json_mode=True` sends the parameter; `json_mode=False` (default) sends nothing |

Unverified parameters (`json_schema`, `strict`) were not used. Later in
the session the account's glm-5.3 balance ran out (see Problems), and the
same `json_object` mode was exercised end-to-end on the free-tier
`glm-4-flash` (see Tests) — accepted there as well.

## Current architecture

```text
User text
    ↓ main.py structured --prompt ...
GLMClient (json_mode=True → response_format={"type":"json_object"})
    ↓
LLMResponse.content
    ↓ json.loads()                 layer 1: JSON syntax
    ↓ SearchIntent.model_validate  layer 2: schema
    ↓
StructuredLLMResult (data + model/usage metadata)
    ↓
outputs/search_intent.json (validated data + metadata only)
```

## Still not an Agent

```text
no tool call        — the model cannot invoke search_pubmed/parse_gene_file
no action loop      — one request, one response, done
no state machine    — each call is stateless; nothing is remembered
```

All four capabilities (gene file → `GeneRecord[]`; PubMed query →
`Article[]`; messages → `LLMResponse`; user intent → `SearchIntent`)
remain separate by design until Phase 4.

## Commands executed

```bash
# 0. baseline
D:/anaconda3/python.exe -m pytest                                    # 111 passed

# 1. JSON-mode capability check (real request, glm-5.3)  → accepted, valid JSON

# 2. offline suite during development
D:/anaconda3/python.exe -m pytest                                    # run 1: 143+4 failed (fixture bug)
                                                                     # run 2: 147 passed, 3 deselected

# 3. live tests — first attempt hit the account balance limit
D:/anaconda3/python.exe -m pytest -m llm_network                     # 2 failed: 429 code 1113 余额不足 (glm-5.3)

# 4. live tests on the free-tier model (env override, .env untouched)
GLM_MODEL=glm-4-flash D:/anaconda3/python.exe -m pytest -m llm_network tests/test_structured_output.py   # PASSED
GLM_MODEL=glm-4-flash D:/anaconda3/python.exe -m pytest -m llm_network tests/test_glm_client.py          # PASSED

# 5. CLI acceptance (real API, glm-4-flash)
D:/anaconda3/python.exe main.py structured --model glm-4-flash \
  --prompt "I want to investigate TP53 in breast cancer, focusing on DNA damage and apoptosis."
# → exit 0; outputs/search_intent.json saved
```

## Tests

Offline (deterministic, no network):

```text
D:/anaconda3/python.exe -m pytest
→ 147 passed, 3 deselected in 2.22s   (36 new Phase 3 tests)
```

Coverage: SearchIntent normalization (strip / blank→None / dedup+order /
limit-after-cleaning / type errors / nested rejection / extra-field
rejection), `chat_structured` layer separation (malformed JSON, natural
language, JSON array, wrong field names, error messages naming offending
fields), `json_mode` forwarding, GLMClient `response_format` wiring
(on/off/unsupported-flag), CLI happy path (output + saved JSON + no
reasoning keys), CLI failure path (exit 1, no file written), empty-topics
rendering, and metadata preservation through `StructuredLLMResult`.

Network (opt-in, marker `llm_network`):

- `test_real_structured_extraction` — **PASSED** on `glm-4-flash` with
  `json_mode=True`: `gene == "TP53"`, disease non-empty, "apoptosis" in
  topics; Pydantic validation passed; usage 366/34/400.
- `test_real_glm_smoke` (Phase 2) — also re-run PASSED on `glm-4-flash`.
- On the `.env` default `glm-5.3` both fail with 429 / code 1113
  (account balance), correctly mapped to `LLMRequestError`.

## Actual example result

CLI acceptance command (above): success, exit 0.

```text
Provider: GLM
Model: glm-4-flash
Structured result:
  gene: TP53
  disease: breast cancer
  topics:
    - DNA damage
    - apoptosis
Usage: prompt tokens=366 | completion tokens=34 | total tokens=400
```

`outputs/search_intent.json` (validated data + model + usage only; no raw
text, no hidden reasoning).

## Problems encountered

### Problem 1 — offline sdk-fixture NameError

```text
problem   4 tests failed with LLMRequestError("… NameError …") on first run
cause     the fake create() was defined outside the fake ZhipuAI factory,
          referencing an `instance` variable that was not in its scope
fix       moved create() inside the factory closure (same pattern as Phase 2)
reason    closures must capture variables from their defining scope
result    147 passed offline
```

### Problem 2 — live tests blocked by account balance (external)

```text
problem   live llm_network tests failed with 429, error code 1113
          "余额不足或无可用资源包,请充值" on the .env model glm-5.3
cause     the GLM account ran out of paid balance mid-session (external
          billing issue, not a code defect — our error taxonomy mapped it
          correctly to LLMRequestError)
fix       verified end-to-end on the free-tier glm-4-flash via
          GLM_MODEL=glm-4-flash (env override; .env untouched) and
          --model glm-4-flash for the CLI
reason    still prove the real network path without waiting for a top-up
result    live extraction + CLI acceptance PASSED on glm-4-flash;
          glm-5.3 remains configured but requires a balance top-up
```

### Problem 3 — misleading rate-limit wording

```text
problem   429/1113 (quota/balance) surfaced as "GLM rate limit exceeded",
          which suggested waiting would help — it would not
cause     SDK class APIReachLimitError covers both rate limits and quota
fix       message changed to "GLM rate limit or quota exceeded" (+ comment)
reason    error text must point at the right remedy
result    clearer diagnosis; no behaviour change
```

## Security check

Real `GLM_API_KEY` exists only in `.env` (git-ignored; project is not yet
a git repository, so nothing is tracked at all); a full-file scan found the
key in no other file; no Authorization headers or keys appear in any CLI
output, test output, or saved JSON. `reasoning_content` / hidden
chain-of-thought is never read, exposed, or persisted —
`outputs/search_intent.json` contains only validated fields + model +
usage.
