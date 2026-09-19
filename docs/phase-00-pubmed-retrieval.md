# Phase 0 — PubMed Retrieval: Execution Record

## Goal

Stand up the smallest useful slice of Literature Agent: given a query string
like `"TP53 AND breast cancer"`, retrieve up to N articles from PubMed via
the official NCBI E-utilities, parse them into a structured `Article` record,
print a summary, and persist the full result as JSON. Everything later
phases build on (gene files, LLM planning, ranking, reports) is out of
scope.

## Environment

| component | value |
|---|---|
| OS | Windows (win32), Git Bash as shell |
| Python | 3.12.7 (Anaconda distribution) — satisfies the >= 3.11 requirement |
| `requests` | 2.32.3 |
| `pytest` | 7.4.4 |
| API key | none used — anonymous E-utilities access |

Environment note: on this machine the `python` shim first on `PATH` points
to the non-functional Microsoft Store stub, so every command below was run
with the real interpreter path `D:/anaconda3/python.exe` (recorded as such
for reproducibility; in a normal venv the plain `python` / `python -m pytest`
forms are equivalent).

## Commands executed

Setup / verification (main commands):

```bash
# 1. run the offline test suite (network smoke test auto-deselected)
D:/anaconda3/python.exe -m pytest -v

# 2. run the Phase 0 acceptance command against the live API
D:/anaconda3/python.exe main.py --query "TP53 AND breast cancer" --max-results 5

# 3. validate the saved JSON structure
D:/anaconda3/python.exe -c "import json; data = json.load(open('outputs/pubmed_results.json', encoding='utf-8')); print(data['retrieved_count'], len(data['articles'][0]['abstract']))"

# 4. run only the real-network smoke test
D:/anaconda3/python.exe -m pytest -m network -v

# 5. error paths via CLI
D:/anaconda3/python.exe main.py --query "   " --max-results 5   # exit 2
D:/anaconda3/python.exe main.py --query "TP53" --max-results 0  # exit 2
```

(Test runs were executed three times in total — twice while fixing the
issues documented below, once green; see Testing.)

## Files created

| file | purpose |
|---|---|
| `app/models/schemas.py` | `Article` dataclass; the pipeline's output type |
| `app/tools/pubmed.py` | PubMed tool (validation, HTTP, ESearch, EFetch, parsing) |
| `main.py` | CLI orchestration only |
| `tests/test_pubmed.py` | 30 offline tests + 1 network smoke test |
| `pyproject.toml`, `requirements.txt`, `.env.example`, `.gitignore` | config: pytest markers, deps, credential template, repo hygiene |
| `examples/example_queries.txt`, `outputs/.gitkeep` | sample queries; keep results dir |
| `README.md`, `docs/*.md` | user-facing and engineering documentation |
| `app/**/__init__.py` | package markers |

No files were modified after the initial creation except the two fixes
below (`app/tools/pubmed.py`, `tests/test_pubmed.py`).

## Implementation

### What ESearch is

ESearch (`esearch.fcgi`) is the E-utilities *search* endpoint: it runs a
query against an NCBI database (here `db=pubmed`) and returns **only the
matching document IDs** plus counts — no article content. Phase 0 calls it
with `retmode=json` and `sort=relevance`, `retmax=max_results`.

- HTTP input: `GET esearch.fcgi?db=pubmed&term=<query>&retmax=N&retmode=json&sort=relevance[&email=&api_key=&tool=]`
- HTTP output: JSON `{"esearchresult": {"count": "…", "idlist": ["36739824", …]}}`
- Program output: `list[str]` of PMIDs (`search_pmids`).

### What EFetch is

EFetch (`efetch.fcgi`) is the E-utilities *download* endpoint: given a list
of document IDs it returns the full records. For `db=pubmed` with
`retmode=xml` that is a `<PubmedArticleSet>` document containing
`<PubmedArticle>` elements (title, abstract, authors, journal, dates,
article IDs).

- HTTP input: `GET efetch.fcgi?db=pubmed&id=123,456,789&retmode=xml&rettype=abstract[&email=&api_key=&tool=]`
- HTTP output: XML text (batched at ≤200 IDs per call, NCBI's recommendation)
- Program output: `list[Article]` (`fetch_articles`).

### Why two separate functions

- It is how NCBI models the problem (search = IDs, fetch = documents), so
  the code maps 1:1 onto the upstream contract instead of hiding it.
- They fail differently: an ESearch failure means "bad query or search
  outage", an EFetch failure means "download problem" — separate functions
  give separate, honest error messages.
- The future Agent wants them separately: cheaply check how many candidates
  exist (ESearch) before spending bandwidth on details (EFetch).

### HTTP request/response handling

Every request goes through one `_http_get()` wrapper providing: a 30-second
timeout; at most 3 total attempts with 1s/2s linear backoff; retries only
for transient failures (network exceptions, 429, 5xx); immediate failure
for other 4xx (with the response snippet in the message); and the optional
`email`/`api_key`/`tool` identification parameters. Responses are validated
before parsing (HTTP 200 check, JSON decode / XML parse with clear
`PubMedError`s), and NCBI-level errors embedded in an HTTP-200 body (both
the top-level `error` and the `header.error` forms) raise too.

### Why the result becomes `Article`

Raw EFetch XML is hostile to consume (nested nodes, inline `<i>` markup,
three alternative journal fields, prose dates like "2020 Jan-Feb"). The
parser (`parse_pubmed_xml` + helpers) flattens all of that into one small,
documented dataclass where every field is either present or explicitly
`None`/`[]` (see `docs/data-contract.md` for the field-by-field table).
Downstream phases (ranking, citation verification, reports) then program
against a stable type instead of re-parsing XML.

### Why JSON output

JSON is the lingua franca for everything planned next: an LLM tool result,
a cache of fetched evidence, report input. The file is written UTF-8 with
`indent=2` and `ensure_ascii=False` so humans can read non-ASCII characters
(authors, symbols) directly. Keeping it a plain file (not a database) is
deliberate for Phase 0.

## Testing

Executed (offline suite):

```text
D:/anaconda3/python.exe -m pytest -v
→ run 1: collection error (SyntaxError in pubmed.py — see problem 1)
→ run 2: 29 passed, 1 failed (composition test — see problem 2)
→ run 3: 30 passed, 1 deselected (final, green)
```

Coverage of the required cases:

1. empty query — `test_search_pmids_rejects_empty_query` (3 whitespace variants) + `…_non_string_query`
2. invalid `max_results` — `test_search_pmids_rejects_invalid_max_results` (0, -1, 201, 1.5, "10", None)
3. empty PubMed result — `test_search_pmids_returns_empty_list_when_no_hits`, `test_search_pubmed_returns_empty_list_when_no_hits`, `test_fetch_articles_with_empty_list_makes_no_http`
4. normal article parsing — `test_parse_returns_one_article_per_record`, `test_full_record_fields`, `test_structured_abstract_keeps_labels`, `test_inline_markup_is_stripped_from_title`
5. missing abstract — `test_record_without_abstract_yields_none`
6. missing DOI — `test_record_without_doi_yields_none`
plus: collective authors / journal / date fallbacks (`test_degraded_record_fallbacks`),
derived `pubmed_url` (`test_pubmed_url_is_always_derived_from_pmid`),
malformed JSON/XML and NCBI error payloads, retry semantics (4xx fails fast,
5xx retried exactly `MAX_RETRIES` times), and an offline end-to-end
composition test asserting the exact ESearch/EFetch parameters.

Real-network smoke test: **yes, executed** —

```text
D:/anaconda3/python.exe -m pytest -m network -v
→ test_real_pubmed_smoke PASSED (1 passed, 30 deselected in 2.30s)
```

It calls the live API for "TP53 AND breast cancer" and asserts ≤3 real
articles with digit PMIDs, valid URLs, and a title on the first record.
By default it is deselected via `pyproject.toml` (`addopts = "-m 'not
network'"`) so CI/offline runs stay green.

## Actual example

Command run (exactly the Phase 0 acceptance command):

```bash
D:/anaconda3/python.exe main.py --query "TP53 AND breast cancer" --max-results 5
```

Result: **success**.

- Retrieved exactly **5 articles** (top hit: PMID 36739824, Blondeaux et al.
  2023, *Cancer Treatment Reviews*; also PMIDs 39393354, 38569880, 27815305,
  29470806).
- All 5 had title, authors, journal, year, DOI, abstract (first abstract is
  1771 characters).
- Summary printed to the terminal; full result saved to
  `outputs/pubmed_results.json` (UTF-8, pretty-printed, `retrieved_count: 5`).
- Exit code 0.

## Problems encountered

### Problem 1 — SyntaxError in `pubmed.py` on first test run

```text
问题   Collection failed: app/tools/pubmed.py line 156: ") from exc" SyntaxError
原因   `raise X from exc` 的异常链语法被误用在赋值语句上
        （`last_error = PubMedError(...) from exc`），Python 不接受
修改   删除两处赋值中的 ` from exc`；超时原因改为拼进错误消息文本
目的   保留"错误消息包含根因"的可定位性（该路径先存储错误、稍后统一 raise，
        本就无法使用异常链）
结果   测试套件可收集，进入执行阶段
```

### Problem 2 — composition test failure (29 passed, 1 failed)

```text
问题   test_search_pubmed_composes_esearch_then_efetch 断言得到 3 篇文章而不是 2 篇
原因   测试自身的 fake _http_get 对 EFetch 无视请求的 id 参数、总是返回
        完整的 3 篇 fixture XML；生产代码行为正确（真实 NCBI 只返回被请求的记录）
修改   重写该测试的 fake：按请求的 id 集合过滤 fixture 中的 <PubmedArticle>
        记录，再拼回合法的 <PubmedArticleSet>
目的   让测试准确建模 NCBI 真实行为，避免测试与实现的契约不符
结果   30 passed, 1 deselected
```

### Problem 3 (prevented) — Windows console codepage

```text
问题   PubMed 标题常含 en dash / 弯引号等字符，Windows 控制台默认 GBK
        编码下 print 可能抛 UnicodeEncodeError
原因   代码页非 UTF-8
修改   main.py 启动时调用 _force_utf8_stdout() 将 stdout 重配置为 UTF-8
        （失败则回退默认行为）
目的   保证终端摘要打印在任何 Windows 代码页下都不中断
结果   实际运行中标题含特殊字符，输出正常，无崩溃
```

## Current limitations

- Input is a hand-written query string only — no gene-list /
  differential-analysis file support yet (Phase 1).
- No LLM involvement: no query planning, no summarization (Phases 2–4).
- No NCBI Gene / Reactome enrichment (Phase 5).
- No relevance ranking of our own — result order is whatever NCBI's
  `sort=relevance` returns (Phase 6 will rank evidence).
- No citation verification (Phase 7) and no HTML report (Phase 8).
- Content depth: metadata + abstract only, no full text.
- `max_results` capped at 200 (one EFetch batch); larger pulls need
  pagination via `retstart`, deliberately not built in Phase 0.
- Without an API key, throughput is limited to 3 requests/second
  (anonymous E-utilities policy).
