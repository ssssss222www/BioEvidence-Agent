"""PubMed retrieval tool built on the NCBI Entrez E-utilities (Phase 0).

Pipeline implemented here::

    search_pmids(query)      --ESearch-->  list of PMIDs
    fetch_articles(pmids)    --EFetch-->   list[Article]
    search_pubmed(query)     = search_pmids + fetch_articles  (Agent Tool entry)

Only the two official E-utility endpoints ``esearch.fcgi`` and ``efetch.fcgi``
are used; there is no web scraping.

NCBI identity: ``NCBI_EMAIL`` / ``NCBI_API_KEY`` are read from the process
environment (optionally from a project-root ``.env`` file, loaded by a small
stdlib parser so we do not need python-dotenv). Both are optional; without an
API key NCBI applies the anonymous rate limit (3 requests/second), which is
fine for Phase 0.
"""

from __future__ import annotations

import json
import os
import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Iterator

import requests

from app.config import load_env_file
from app.models.schemas import Article

ESEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
EFETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"

APP_NAME = "literature-agent"
APP_VERSION = "0.1.0"

REQUEST_TIMEOUT_SECONDS = 30
MAX_RETRIES = 3  # total attempts per request, not additional retries
RETRY_BACKOFF_SECONDS = 1.0  # linear backoff: 1s, 2s between attempts
RETRYABLE_HTTP_STATUS = {429, 500, 502, 503, 504}
EFETCH_BATCH_SIZE = 200  # NCBI recommends at most 200 ids per EFetch call
MAX_RESULTS_LIMIT = 200  # keeps retmax and a single EFetch batch in sync

_YEAR_RE = re.compile(r"\d{4}")


class PubMedError(RuntimeError):
    """Raised when an E-utilities request fails or returns an unusable body.

    The message is intended to be user-facing: it says what failed and why,
    e.g. "ESearch returned HTTP 503 after 3 attempts" or
    "EFetch returned malformed XML: ...".
    """


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------

def _validate_query(query: str) -> str:
    """Return the stripped query or raise ``ValueError`` for blank input."""
    if not isinstance(query, str):
        raise ValueError(f"query must be a string, got {type(query).__name__}")
    query = query.strip()
    if not query:
        raise ValueError("query must not be empty or whitespace-only")
    return query


def _validate_max_results(max_results: int) -> int:
    if isinstance(max_results, bool) or not isinstance(max_results, int):
        raise ValueError(f"max_results must be an int, got {max_results!r}")
    if not 1 <= max_results <= MAX_RESULTS_LIMIT:
        raise ValueError(
            f"max_results must be between 1 and {MAX_RESULTS_LIMIT}, got {max_results}"
        )
    return max_results


def _validate_pmids(pmids: list[str]) -> list[str]:
    if not isinstance(pmids, list):
        raise ValueError(f"pmids must be a list of strings, got {type(pmids).__name__}")
    cleaned: list[str] = []
    for pmid in pmids:
        if not isinstance(pmid, str) or not pmid.strip():
            raise ValueError(f"every pmid must be a non-empty string, got {pmid!r}")
        cleaned.append(pmid.strip())
    return cleaned


# ---------------------------------------------------------------------------
# NCBI identity / environment
# ---------------------------------------------------------------------------

def _ncbi_params() -> dict[str, str]:
    """Common query parameters identifying this tool (email/api_key optional)."""
    load_env_file()
    params = {"tool": f"{APP_NAME}/{APP_VERSION}"}
    email = os.environ.get("NCBI_EMAIL", "").strip()
    api_key = os.environ.get("NCBI_API_KEY", "").strip()
    if email:
        params["email"] = email
    if api_key:
        params["api_key"] = api_key
    return params


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------

def _http_get(url: str, params: dict[str, str]) -> str:
    """GET one E-utilities endpoint with timeout and bounded retries.

    Retries only transient failures (network errors, 429/5xx). Client errors
    (4xx other than 429) are raised immediately because retrying cannot fix
    them. Never retries more than ``MAX_RETRIES - 1`` times.
    """
    last_error: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.get(url, params=params, timeout=REQUEST_TIMEOUT_SECONDS)
        except requests.Timeout as exc:
            last_error = PubMedError(
                f"Request to {url} timed out after {REQUEST_TIMEOUT_SECONDS}s "
                f"(attempt {attempt}/{MAX_RETRIES}), caused by: {exc}"
            )
        except requests.RequestException as exc:
            last_error = PubMedError(
                f"Network error calling {url}: {exc} (attempt {attempt}/{MAX_RETRIES})"
            )
        else:
            if response.status_code == 200:
                return response.text
            reason = f"HTTP {response.status_code} from {url}"
            if response.status_code not in RETRYABLE_HTTP_STATUS:
                raise PubMedError(f"{reason}: {response.text[:200]}")
            last_error = PubMedError(
                f"{reason} (attempt {attempt}/{MAX_RETRIES}): {response.text[:200]}"
            )
        if attempt < MAX_RETRIES:
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)
    assert last_error is not None  # loop always assigns it before failing
    raise last_error


# ---------------------------------------------------------------------------
# ESearch: query -> PMID list
# ---------------------------------------------------------------------------

def search_pmids(query: str, max_results: int = 10) -> list[str]:
    """Run an ESearch against ``db=pubmed`` and return the PMID list.

    Args:
        query: PubMed query syntax, e.g. ``"TP53 AND breast cancer"``.
        max_results: maximum number of PMIDs to return (1..200).

    Returns:
        PMIDs as strings, most relevant first. Empty list when the query
        matches nothing.

    Raises:
        ValueError: blank/invalid query or invalid ``max_results``.
        PubMedError: network failure, non-200 HTTP status, or an unusable
            ESearch response body.
    """
    query = _validate_query(query)
    max_results = _validate_max_results(max_results)
    params = {
        "db": "pubmed",
        "term": query,
        "retmax": str(max_results),
        "retmode": "json",
        "sort": "relevance",  # match what a human sees on the PubMed website
        **_ncbi_params(),
    }
    body = _http_get(ESEARCH_URL, params)
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        snippet = body[:200].replace("\n", " ")
        raise PubMedError(f"ESearch returned malformed JSON: {snippet}") from exc
    return _parse_esearch_payload(payload)


def _parse_esearch_payload(payload: object) -> list[str]:
    """Extract ``esearchresult.idlist`` from an ESearch JSON payload."""
    if not isinstance(payload, dict):
        raise PubMedError(f"Unexpected ESearch payload type: {type(payload).__name__}")
    error = payload.get("error")  # some failures put the error at top level
    if not error and isinstance(payload.get("header"), dict):
        error = payload["header"].get("error")  # e.g. invalid db / bad parameter
    if error:
        raise PubMedError(f"ESearch reported an error: {error}")
    result = payload.get("esearchresult")
    if not isinstance(result, dict):
        raise PubMedError("ESearch response is missing the 'esearchresult' object")
    if "error" in result:
        raise PubMedError(f"ESearch reported an error: {result['error']}")
    id_list = result.get("idlist")
    if not isinstance(id_list, list):
        raise PubMedError("ESearch response is missing 'esearchresult.idlist'")
    return [str(pmid) for pmid in id_list]


# ---------------------------------------------------------------------------
# EFetch: PMID list -> Article list
# ---------------------------------------------------------------------------

def fetch_articles(pmids: list[str]) -> list[Article]:
    """Fetch full metadata for ``pmids`` via EFetch and parse to ``Article``.

    Args:
        pmids: PubMed IDs as strings. Empty list returns ``[]`` without any
            HTTP call. PMIDs unknown to PubMed are silently dropped by NCBI,
            so the result may be shorter than the input.

    Returns:
        Parsed articles, one per PMID that NCBI actually returned.

    Raises:
        ValueError: any element of ``pmids`` is not a non-empty string.
        PubMedError: network failure, non-200 HTTP status, or malformed XML.
    """
    pmids = _validate_pmids(pmids)
    articles: list[Article] = []
    for batch in _batched(pmids, EFETCH_BATCH_SIZE):
        params = {
            "db": "pubmed",
            "id": ",".join(batch),
            "retmode": "xml",
            "rettype": "abstract",  # includes title/abstract/authors, no MeSH
            **_ncbi_params(),
        }
        body = _http_get(EFETCH_URL, params)
        articles.extend(parse_pubmed_xml(body))
    return articles


def _batched(items: list[str], size: int) -> Iterator[list[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def parse_pubmed_xml(xml_text: str) -> list[Article]:
    """Parse an EFetch XML body into ``Article`` records.

    Kept public (no leading underscore) so tests can exercise the parser with
    fixture XML directly, without any network mocking.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        snippet = xml_text[:200].replace("\n", " ")
        raise PubMedError(f"EFetch returned malformed XML ({exc}): {snippet}") from exc
    return [
        _article_from_element(element)
        for element in root.iter("PubmedArticle")
    ]


def _article_from_element(element: ET.Element) -> Article:
    """Map one ``<PubmedArticle>`` XML element onto an ``Article``.

    Every field extraction degrades to ``None``/``[]`` when its XML node is
    missing; no exception is raised for incomplete records.
    """
    return Article(
        pmid=_text_of(element, "./MedlineCitation/PMID") or "",
        title=_text_of(element, "./MedlineCitation/Article/ArticleTitle"),
        abstract=_extract_abstract(element),
        authors=_extract_authors(element),
        journal=_extract_journal(element),
        publication_year=_extract_year(element),
        doi=_extract_doi(element),
    )


def _text_of(element: ET.Element, path: str) -> str | None:
    """First matching node's full inner text (handles <i>/<b>/<sup> markup)."""
    node = element.find(path)
    if node is None:
        return None
    text = "".join(node.itertext()).strip()
    return text or None


def _extract_abstract(element: ET.Element) -> str | None:
    """Join ``AbstractText`` parts; keeps ``Label`` of structured abstracts."""
    abstract = element.find("./MedlineCitation/Article/Abstract")
    if abstract is None:
        return None
    parts: list[str] = []
    for text_node in abstract.findall("AbstractText"):
        chunk = "".join(text_node.itertext()).strip()
        if not chunk:
            continue
        label = text_node.get("Label")
        parts.append(f"{label}: {chunk}" if label else chunk)
    return "\n".join(parts) if parts else None


def _extract_authors(element: ET.Element) -> list[str]:
    """``ForeName LastName`` per author; ``CollectiveName`` for group authors."""
    author_list = element.find("./MedlineCitation/Article/AuthorList")
    if author_list is None:
        return []
    names: list[str] = []
    for author in author_list.findall("Author"):
        collective = _text_of(author, "CollectiveName")
        if collective:
            names.append(collective)
            continue
        last = _text_of(author, "LastName")
        fore = _text_of(author, "ForeName")
        if fore and last:
            names.append(f"{fore} {last}")
        elif last:
            names.append(last)
        # authors with neither a personal nor collective name are skipped
    return names


def _extract_journal(element: ET.Element) -> str | None:
    """Prefer the full journal title, fall back to abbreviations."""
    for path in (
        "./MedlineCitation/Article/Journal/Title",
        "./MedlineCitation/Article/Journal/ISOAbbreviation",
        "./MedlineCitation/MedlineTA",
    ):
        journal = _text_of(element, path)
        if journal:
            return journal
    return None


def _extract_year(element: ET.Element) -> str | None:
    """Publication year as a 4-char string, or ``None``.

    PubMed dates are inconsistent: ``PubDate`` may carry ``<Year>`` or a prose
    ``<MedlineDate>`` like "2020 Jan-Feb"; electronic-first articles often
    only have ``ArticleDate``. We try them in that order.
    """
    pub_date = element.find("./MedlineCitation/Article/Journal/JournalIssue/PubDate")
    if pub_date is not None:
        year = _text_of(pub_date, "Year")
        if year and _YEAR_RE.fullmatch(year):
            return year
        medline_date = _text_of(pub_date, "MedlineDate")
        if medline_date:
            match = _YEAR_RE.search(medline_date)
            if match:
                return match.group(0)
    article_date_year = _text_of(
        element, "./MedlineCitation/Article/ArticleDate/Year"
    )
    if article_date_year and _YEAR_RE.fullmatch(article_date_year):
        return article_date_year
    return None


def _extract_doi(element: ET.Element) -> str | None:
    for article_id in element.findall("./PubmedData/ArticleIdList/ArticleId"):
        if article_id.get("IdType") == "doi":
            doi = (article_id.text or "").strip()
            return doi or None
    return None


# ---------------------------------------------------------------------------
# High-level tool (future Agent entry point)
# ---------------------------------------------------------------------------

def search_pubmed(query: str, max_results: int = 10) -> list[Article]:
    """Query PubMed and return up to ``max_results`` parsed articles.

    This is the function a future Literature Agent will register as its
    "search literature" tool: one call, structured output, no leaks of HTTP
    details. It simply composes :func:`search_pmids` and :func:`fetch_articles`.
    """
    pmids = search_pmids(query, max_results)
    if not pmids:
        return []
    return fetch_articles(pmids)
