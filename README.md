# BioEvidence Agent

## Project

BioEvidence Agent — a multi-source biomedical evidence agent for gene
annotation, pathway mapping, literature retrieval, and provenance-aware
synthesis.

## Goal

Given a gene list (or a differential-expression result file), the finished
agent will retrieve PubMed literature and gene/pathway knowledge, plan
retrieval tasks with an LLM, rank and verify the evidence, and produce an
HTML biology report with PMID-backed citations.

## Current status

```text
Phase 5 — Multi-tool Biomedical Agent
```

(Phases 0-4 are complete; `pubmed` / `parse` / `llm` / `structured`
subcommands unchanged.)

## Current workflows

```text
Phase 0:  PubMed query → PubMed Tool → Article[]
Phase 1:  Input file   → File Parser → GeneRecord[]
Phase 2:  Messages     → GLM Client  → LLMResponse
Phase 3:  User intent  → GLM + JSON + Pydantic → SearchIntent
Phase 4:  Question → Agent Loop (GLM ⇄ search_pubmed tool, bounded) → cited answer
Phase 5:  One agent, one loop, three tools: search_pubmed + get_gene_info
          (NCBI Gene) + get_reactome_pathways (Reactome), with per-source
          provenance (PMIDs / GeneIDs / Reactome stable IDs)
```

## Installation

Requires Python >= 3.11.

```bash
python -m venv .venv
```

Activate the virtual environment:

- Windows (PowerShell): `.\.venv\Scripts\Activate.ps1`
- Windows (cmd): `.venv\Scripts\activate.bat`
- Linux / macOS: `source .venv/bin/activate`

Then install dependencies:

```bash
pip install -r requirements.txt
```

Optional but required for Phase 2: copy `.env.example` to `.env` and set
`GLM_API_KEY` / `GLM_MODEL` (ZhipuAI GLM credentials — see
https://open.bigmodel.cn/). `NCBI_EMAIL` / `NCBI_API_KEY` stay optional
for the PubMed tool. `.env` is git-ignored; never commit real keys.

## Usage

CLI uses subcommands as of Phase 1.

Search PubMed (Phase 0):

```bash
python main.py pubmed --query "TP53 AND breast cancer" --max-results 5
```

> **Breaking change (Phase 1):** the Phase 0 form
> `python main.py --query "..."` no longer works; add the `pubmed`
> subcommand as shown above.

Parse a gene / differential-analysis file (Phase 1):

```bash
python main.py parse --input examples/example_deg.csv
```

Supports `.csv`, `.tsv`, `.txt` (tab-separated) and `.xlsx` (first sheet
by default, `--sheet-name` to select another). The parser auto-detects the
gene column and optional `logFC` / `pvalue` / `padj` columns via a
deterministic alias table, cleans gene symbols (whitespace only — casing is
preserved), keeps the first occurrence of duplicate genes, and reports
missing/invalid cells as statistics. Output:
`outputs/gene_records.json`.

Example files: `examples/example_deg.csv` (intentionally dirty),
`examples/example_queries.txt`.

Call the GLM chat API (Phase 2; needs `GLM_API_KEY` + `GLM_MODEL`):

```bash
python main.py llm --prompt "Explain the main biological function of TP53 in one sentence."
```

Optional flags: `--system "..."` (default: a simple biomedical-assistant
persona), `--model ...` (overrides `GLM_MODEL`), `--temperature 0.1`
(omitted → API-side default). Prints provider, model, response text and
token usage; never prints the API key.

Extract structured data from free text (Phase 3):

```bash
python main.py structured --prompt "I want to investigate TP53 in breast cancer, focusing on DNA damage and apoptosis."
```

Run the literature agent (Phases 4-5; needs `GLM_API_KEY` + `GLM_MODEL`):

```bash
python main.py agent --prompt "Find recent PubMed evidence about the role of TP53 in breast cancer."
python main.py agent --prompt "What is human TP53? Cite the GeneID."
python main.py agent --prompt "Which Reactome pathways involve human TP53?"
python main.py agent --prompt "Explain human TP53, its pathways, and breast cancer evidence."
```

The agent has three tools behind one allowlist registry — `search_pubmed`
(PubMed literature), `get_gene_info` (NCBI Gene database facts),
`get_reactome_pathways` (Reactome pathway mappings) — and chooses/composes
them per question. Every tool call is argument-validated with Pydantic;
the loop is bounded (6 LLM steps); tool results are size-bounded with
explicit truncation markers. Output includes retrieved Gene IDs, Reactome
stable IDs and PMIDs (real provenance, never parsed from the answer), and
the answer distinguishes curated database facts from literature evidence.
`--model` / `--temperature` flags available.

Sends the text to GLM with a strict extraction prompt and JSON mode,
parses the reply as JSON, validates it against the `SearchIntent` Pydantic
schema, prints the result and saves it (plus model/usage metadata) to
`outputs/search_intent.json`. Invalid JSON or schema violations fail
loudly (`[structured error]`) — no silent repair. `--model` /
`--temperature` / `--output` flags available.

Run the tests (offline by default; live-API smoke tests are excluded):

```bash
python -m pytest
```

Run only the real-network smoke tests:

```bash
python -m pytest -m network            # NCBI E-utilities (PubMed)
python -m pytest -m llm_network        # ZhipuAI GLM (needs GLM_API_KEY/GLM_MODEL)
python -m pytest -m agent_network      # full agent: real GLM + real tools
python -m pytest -m ncbi_network       # real NCBI Gene
python -m pytest -m reactome_network   # real Reactome Analysis Service
```

## Roadmap

```text
Phase 5 Multi-tool agent         ← current
Phase 6 Evidence ranking
Phase 7 Citation verification
Phase 8 HTML report
Phase 9 UI
```

## Documentation

- `docs/architecture.md` — project layout and data flow
- `docs/data-contract.md` — inputs/outputs of every public function
- `docs/development-log.md` — change log and rationale
- `docs/phase-00-pubmed-retrieval.md` — Phase 0 execution record
- `docs/phase-01-gene-file-parser.md` — Phase 1 execution record
- `docs/phase-02-glm-client.md` — Phase 2 execution record
- `docs/phase-03-structured-output.md` — Phase 3 execution record
- `docs/phase-04-agent-loop.md` — Phase 4 execution record
- `docs/phase-05-multi-tool-agent.md` — Phase 5 execution record
