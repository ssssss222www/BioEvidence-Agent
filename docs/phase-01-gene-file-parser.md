# Phase 1 — Gene File Parser: Execution Record

## Goal

Turn a user-supplied gene list or differential-analysis file
(CSV / TSV / TXT / XLSX) into clean, validated `GeneRecord` objects plus an
audit trail (`GeneParseResult`), so later phases have a trustworthy gene
input instead of raw spreadsheet data. No LLM, no databases, no species
knowledge — just deterministic schema recognition, cleaning, and counting.

## Why a parser is needed (and not just `pd.read_csv()`)

`pd.read_csv()` alone gives you a DataFrame with whatever columns and
whatever dirt the file happens to contain. The pipeline needs guarantees:

- **Schema recognition** — the gene column may be called `gene`, `Symbol`,
  `GeneSymbol`, `gene_symbol`, …; the statistics may be `log2FoldChange`
  (DESeq2), `avg_log2FC` (Seurat), `P.Value`/`adj.P.Val` (limma),
  `FDR`/`qvalue`. Someone must decide which column is which; doing it
  deterministically (alias table) makes runs reproducible and auditable,
  which an LLM guess would not be.
- **Validation** — p-values outside [0, 1], `inf`, `not_available` strings:
  these must be detected and counted, not silently propagated into
  ranking math later.
- **Normalization** — whitespace-only dirt (`" TP53 "`) must be cleaned,
  while genuine biological information (casing: `Trp53` vs `TP53`) must be
  preserved. `read_csv` does neither.
- **Data contract** — downstream phases (and the Agent) need one stable
  type (`GeneRecord`) with documented missing-value semantics, not "a
  DataFrame with hopefully these columns".

## GeneRecord

Defined in `app/models/schemas.py`:

| field | type | missing allowed | meaning |
|---|---|---|---|
| `gene` | `str` | no | cleaned symbol: stringified, stripped, non-empty; **casing preserved**; never uppercased, never ID-converted |
| `logfc` | `float \| None` | yes | log fold change; `None` when column/cell missing or cell invalid |
| `pvalue` | `float \| None` | yes | raw p-value; additionally invalid outside `[0, 1]` |
| `padj` | `float \| None` | yes | adjusted p-value / FDR / q-value; same rule as `pvalue` |

Plus `to_dict()` for JSON serialization. No species/disease/pathway fields
were added — those belong to future phases and would be speculation now.

## Column aliases

Headers are matched after `strip().lower()` (case-insensitive,
whitespace-trimmed — nothing fuzzier):

| canonical | aliases (lowercase) |
|---|---|
| `gene` | gene, symbol, gene_symbol, genesymbol |
| `logfc` | logfc, log2fc, log2foldchange, avg_logfc, avg_log2fc |
| `pvalue` | pvalue, p_value, pval, p.value |
| `padj` | padj, fdr, qvalue, q_value, adj.p.val, adjusted_pvalue |

Multiple candidates for the same field (e.g. both `gene` and `GeneSymbol`,
or case variants that collide after normalization such as `Gene` + `gene`
or `FDR` + `fdr`) → `GeneFileError` listing the candidates; **never** a
silent choice. A missing gene column → `GeneFileError` listing the actual
columns and the supported aliases (plus a hint for comma-separated `.txt`
files). Optional statistics columns are simply absent from `column_mapping`
when not found.

## Duplicate policy

`keep first` occurrence of each gene (after cleaning). Later rows are
dropped and counted (`duplicate_gene_count`, `duplicate_gene_names`).

Why no biological aggregation in Phase 1: choosing mean logFC / min padj /
max |logFC| / transcript-merging requires knowing whether rows are probes,
transcripts, or per-condition measurements — information the file does not
carry and Phase 1 has no annotation source to resolve. A wrong silent
aggregation would corrupt every downstream conclusion, so we keep the first
row (fully auditable) and defer aggregation to a phase that can justify it.

## Numeric policy

| situation | handling |
|---|---|
| NaN / empty cell / absent column | value → `None`; counted as missing (no invalid counter) |
| valid finite number (incl. `1e-05`) | `float` |
| non-numeric non-empty string (e.g. `not_available`) | `None`, `invalid_numeric_count++`, row survives — file never silently fails |
| `inf` / non-finite | `None`, `invalid_numeric_count++` |
| `pvalue` / `padj` outside `[0, 1]` | `None`, `invalid_numeric_count++` — **never clamped** to 0/1 |

`logfc` has no range constraint (any finite float is biologically legal).

Counting scope: `invalid_numeric_count` covers invalid cells found in
**retained unique-gene rows only**. Duplicate rows are skipped before
numeric parsing (keep-first policy), so a bad cell inside a dropped
duplicate row is not counted — that row is already reported via
`duplicate_gene_count` / `duplicate_gene_names`.

## Files

Added:

- `app/parsers/__init__.py` — package marker
- `app/parsers/gene_file_parser.py` — parser, policies, `GeneFileError`,
  `GeneParseResult`, `COLUMN_ALIASES`
- `examples/example_deg.csv` — dirty demo data (9 rows: normal, padded
  symbol, duplicate EGFR, missing gene, missing logFC, missing padj,
  `not_available` logFC, p-value > 1)
- `tests/test_gene_file_parser.py` — 44 offline tests
- `docs/phase-01-gene-file-parser.md` — this record

Modified:

- `app/models/schemas.py` — added `GeneRecord`
- `main.py` — subcommands `pubmed` / `parse` (**breaking change**; migration:
  `python main.py --query X` → `python main.py pubmed --query X`);
  PubMed logic untouched, still in `app/tools/pubmed.py`
- `requirements.txt`, `pyproject.toml` — +`pandas>=2.0`, +`openpyxl>=3.1`
- `README.md`, `docs/architecture.md`, `docs/data-contract.md`,
  `docs/development-log.md` — Phase 1 updates

## Commands executed

```bash
# 0. confirm Phase 0 baseline before touching anything
D:/anaconda3/python.exe -m pytest                 # 30 passed, 1 deselected

# 1. full suite after implementation
D:/anaconda3/python.exe -m pytest -v               # 74 passed, 1 deselected

# 2. Phase 1 acceptance command (real run)
D:/anaconda3/python.exe main.py parse --input examples/example_deg.csv

# 3. inspect JSON output
D:/anaconda3/python.exe -c "import json; json.load(open('outputs/gene_records.json', encoding='utf-8')); ..."   # structure/records check

# 4. CLI migration & error paths
D:/anaconda3/python.exe main.py                                    # usage error (exit 2)
D:/anaconda3/python.exe main.py parse --input examples/missing_file.csv   # exit 1
D:/anaconda3/python.exe main.py pubmed --query "BRCA1 AND ovarian cancer" --max-results 2 --output outputs/pubmed_smoke_phase1.json
                                                                   # exit 0; smoke file deleted afterwards

# 5. exit-code checks
D:/anaconda3/python.exe main.py parse --input examples/missing_file.csv ; echo $?   # 1
D:/anaconda3/python.exe main.py parse --input examples/example_deg.csv  ; echo $?   # 0
```

(Interpreter path is the Anaconda one because the `python` shim on this
machine points to a non-functional Microsoft Store stub.)

## Tests

Real results, first run after implementation:

```text
D:/anaconda3/python.exe -m pytest -v
→ 74 passed, 1 deselected (network) in 2.00s
```

- 44 new tests in `tests/test_gene_file_parser.py`: all four file types +
  specific Excel sheet, unsupported suffix, missing file, empty file,
  directory input; 8 gene aliases + header padding; no-gene-column error
  content; ambiguous gene/stat columns; whitespace trim, NaN/blank gene,
  duplicate keep-first (+ unique duplicate names), case preservation;
  normal/missing/invalid numerics, inf, p-value/padj out of range (6
  variants), boundary 0/1 accepted, stat aliases, summary statistics,
  alias-table regression guard.
- 30 Phase 0 tests unchanged and passing.
- Zero network use in the offline suite (network marker still deselected).

## Actual example

`D:/anaconda3/python.exe main.py parse --input examples/example_deg.csv`
→ success (exit 0):

```text
total rows:        9
valid gene rows:   8
unique genes:      7
duplicates:        1 (EGFR; first occurrence logfc=3.4 kept)
missing genes:     1 (empty gene cell)
invalid numeric:   2 (KRAS logfc="not_available", PTEN P.Value=1.2 > 1)
column mapping:    gene<-GeneSymbol, logfc<-log2FoldChange, pvalue<-P.Value, padj<-FDR
output path:       outputs/gene_records.json
```

Verified in the JSON: `" TP53 "`-style rows cleaned, `Trp53` casing kept,
`MYC` logfc `null` (missing), `KRAS` logfc `null` (invalid), `PTEN`
pvalue `null` (out of range), `EGFR` single record.

## Post-phase correctness patch (2026-09-19, before Phase 2)

### Problem A — normalized column names silently overwrote each other

```text
problem   _map_columns() built {normalized_key: original_column} as a dict,
          so two distinct columns that normalize identically (e.g. "Gene"
          and "gene", or "FDR" and "fdr") collapsed into one dict entry and
          bypassed the ambiguity protection — one column was silently kept
cause     dict comprehension keyed by the normalized name loses duplicates
fix       keep a list of (normalized_key, original_column) pairs and collect
          matches across all columns for each canonical field
reason    the documented contract says the parser must never silently
          choose between candidate columns; case-variant duplicates are
          just as ambiguous as different aliases
result    "Gene"+"gene", "gene"+"GeneSymbol", "FDR"+"fdr" all raise
          GeneFileError("Ambiguous columns for …"); covered by 3 new
          parametrized regression tests
```

### Problem B — `invalid_numeric_count` scope was implicit

```text
problem   duplicate rows are skipped before numeric parsing, so the counter
          only sees retained unique-gene rows — but no doc stated this,
          inviting the misreading that it covers all input rows
cause     the keep-first shortcut predates the numeric pass by design (no
          behaviour bug), but the semantics were undocumented
fix       documented explicitly in the GeneParseResult docstring,
          docs/data-contract.md (field table + numeric policy), and here;
          code behaviour intentionally unchanged
reason    an audit statistic whose denominator is ambiguous is worse than
          none — the definition must be explicit
result    semantics now stated in three places; no code or tests changed
          for this item
```

## Problems encountered

### Problem 1 — misleading exit-code echo during manual verification

```text
problem   "echo exit=$?" printed an empty value when the command was run
          with ">NUL" redirection
cause     Git Bash on Windows treats the CMD-style ">NUL" redirect oddly
fix       re-ran verification with ">/dev/null" / plain commands
reason    verify the real exit codes (1 for GeneFileError, 0 for success)
result    exit=1 (missing file) and exit=0 (success) confirmed
```

No code defects were found during Phase 1 — the test suite passed on its
first full run (74/74 offline). The two deliberate design guardrails
(ambiguity errors, missing-vs-invalid counting) were covered by tests
written before the first run.

## Current limitations

- No species identification — human `TP53` and mouse `Trp53` are both
  accepted as-is, with no way to tell them apart.
- No gene-symbol validation against any database — typos pass through as
  valid records.
- No Ensembl / Entrez ID input or conversion — symbols only.
- No gene prioritization or DEG significance filtering (Phase 6 scope).
- The parser does not call PubMed and does not use any LLM; the two
  workflows remain separate until Phase 4.
- `.txt` is assumed tab-separated; comma-separated text must be renamed
  `.csv` (the error message says so).
- Text input must be UTF-8 (BOM-tolerant); other encodings are rejected
  with a hint instead of being transcoded.
- `--sheet-name` accepts one sheet; multi-sheet workbooks need separate
  runs.
