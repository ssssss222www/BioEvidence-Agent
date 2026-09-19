"""Tests for the Phase 0 PubMed tool.

All tests except the final smoke test run fully offline: HTTP is faked via
``monkeypatch`` on ``app.tools.pubmed._http_get``, and the XML/JSON parsers
are exercised directly with fixture payloads that mirror real NCBI responses.
"""

from __future__ import annotations

import json
import re

import pytest

from app.models.schemas import Article
from app.tools import pubmed
from app.tools.pubmed import (
    PubMedError,
    fetch_articles,
    parse_pubmed_xml,
    search_pmids,
    search_pubmed,
)

# ---------------------------------------------------------------------------
# Fixtures: realistic (trimmed) NCBI payloads
# ---------------------------------------------------------------------------

EFETCH_XML = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE PubmedArticleSet PUBLIC "-//NLM//DTD PubMedArticle, 1st January 2024//EN" "https://dtd.nlm.nih.gov/ncbi/pubmed/out/pubmed_240101.dtd">
<PubmedArticleSet>
  <PubmedArticle>
    <MedlineCitation Status="MEDLINE" Owner="NLM">
      <PMID Version="1">11111111</PMID>
      <Article PubModel="Print">
        <Journal>
          <JournalIssue CitedMedium="Print">
            <Volume>12</Volume>
            <Issue>3</Issue>
            <PubDate>
              <Year>2021</Year>
              <Month>Jun</Month>
            </PubDate>
          </JournalIssue>
          <Title>Journal of Example Biomedicine</Title>
          <ISOAbbreviation>J Example Biomed</ISOAbbreviation>
        </Journal>
        <ArticleTitle>TP53 mutations in <i>breast cancer</i> progression and therapy resistance</ArticleTitle>
        <Abstract>
          <AbstractText Label="BACKGROUND" NlmCategory="UNLABELLED">TP53 is frequently mutated in breast cancer.</AbstractText>
          <AbstractText Label="METHODS" NlmCategory="METHODS">We analyzed 500 tumor samples.</AbstractText>
        </Abstract>
        <AuthorList>
          <Author>
            <LastName>Zhang</LastName>
            <ForeName>Li</ForeName>
          </Author>
          <Author>
            <LastName>Smith</LastName>
            <ForeName>John A</ForeName>
          </Author>
        </AuthorList>
        <Language>eng</Language>
      </Article>
      <MedlineTA>J Example Biomed</MedlineTA>
    </MedlineCitation>
    <PubmedData>
      <ArticleIdList>
        <ArticleId IdType="pubmed">11111111</ArticleId>
        <ArticleId IdType="doi">10.1000/fake.2021.001</ArticleId>
      </ArticleIdList>
    </PubmedData>
  </PubmedArticle>

  <PubmedArticle>
    <MedlineCitation Status="MEDLINE" Owner="NLM">
      <PMID Version="1">22222222</PMID>
      <Article PubModel="Print">
        <Journal>
          <JournalIssue CitedMedium="Print">
            <PubDate>
              <MedlineDate>2020 Jan-Feb</MedlineDate>
            </PubDate>
          </JournalIssue>
        </Journal>
        <ArticleTitle>A very old paper without an abstract or DOI</ArticleTitle>
        <AuthorList>
          <Author>
            <CollectiveName>International Example Consortium</CollectiveName>
          </Author>
        </AuthorList>
      </Article>
      <MedlineTA>Old Example Journal</MedlineTA>
    </MedlineCitation>
  </PubmedArticle>

  <PubmedArticle>
    <MedlineCitation Status="Publisher" Owner="NLM">
      <PMID Version="1">33333333</PMID>
      <Article PubModel="Electronic-eCollection">
        <Journal>
          <ISOAbbreviation>Epublish J</ISOAbbreviation>
        </Journal>
        <ArticleTitle>An electronic-first article with no PubDate year</ArticleTitle>
        <ArticleDate DateType="Electronic">
          <Year>2019</Year>
          <Month>Aug</Month>
          <Day>15</Day>
        </ArticleDate>
      </Article>
    </MedlineCitation>
    <PubmedData>
      <ArticleIdList>
        <ArticleId IdType="pubmed">33333333</ArticleId>
      </ArticleIdList>
    </PubmedData>
  </PubmedArticle>
</PubmedArticleSet>
"""

ESEARCH_JSON_FULL = {
    "esearchresult": {
        "count": "2",
        "retmax": "2",
        "retstart": "0",
        "idlist": ["11111111", "22222222"],
    }
}

ESEARCH_JSON_EMPTY = {
    "esearchresult": {
        "count": "0",
        "retmax": "0",
        "retstart": "0",
        "idlist": [],
    }
}


def _fake_http(payloads_by_url: dict[str, str]):
    """Build a ``_http_get`` replacement serving canned bodies per URL."""

    def fake_get(url: str, params: dict) -> str:
        return payloads_by_url[url]

    return fake_get


# ---------------------------------------------------------------------------
# 1-2. Input validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_query", ["", "   ", "\t\n"])
def test_search_pmids_rejects_empty_query(bad_query):
    with pytest.raises(ValueError, match="query"):
        search_pmids(bad_query)


def test_search_pmids_rejects_non_string_query():
    with pytest.raises(ValueError, match="query"):
        search_pmids(None)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad_max", [0, -1, 201, 1.5, "10", None])
def test_search_pmids_rejects_invalid_max_results(bad_max):
    with pytest.raises(ValueError, match="max_results"):
        search_pmids("TP53", bad_max)  # type: ignore[arg-type]


def test_fetch_articles_rejects_invalid_pmids():
    with pytest.raises(ValueError, match="pmid"):
        fetch_articles(["123", ""])  # type: ignore[list-item]
    with pytest.raises(ValueError, match="pmid"):
        fetch_articles("12345")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 3. Empty PubMed result
# ---------------------------------------------------------------------------

def test_search_pmids_returns_empty_list_when_no_hits(monkeypatch):
    monkeypatch.setattr(
        pubmed, "_http_get", _fake_http({pubmed.ESEARCH_URL: json.dumps(ESEARCH_JSON_EMPTY)})
    )
    assert search_pmids("zzzzzzzzznotagene") == []


def test_search_pubmed_returns_empty_list_when_no_hits(monkeypatch):
    payloads = {
        pubmed.ESEARCH_URL: json.dumps(ESEARCH_JSON_EMPTY),
        pubmed.EFETCH_URL: EFETCH_XML,
    }
    monkeypatch.setattr(pubmed, "_http_get", _fake_http(payloads))
    assert search_pubmed("zzzzzzzzznotagene") == []


def test_fetch_articles_with_empty_list_makes_no_http(monkeypatch):
    def unexpected_get(url, params):  # pragma: no cover - fails the test if hit
        raise AssertionError("fetch_articles([]) must not call HTTP")

    monkeypatch.setattr(pubmed, "_http_get", unexpected_get)
    assert fetch_articles([]) == []


# ---------------------------------------------------------------------------
# 4. Normal article parsing
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def articles() -> list[Article]:
    return parse_pubmed_xml(EFETCH_XML)


def test_parse_returns_one_article_per_record(articles):
    assert len(articles) == 3
    assert [a.pmid for a in articles] == ["11111111", "22222222", "33333333"]


def test_full_record_fields(articles):
    first = articles[0]
    assert first.title == "TP53 mutations in breast cancer progression and therapy resistance"
    assert first.authors == ["Li Zhang", "John A Smith"]
    assert first.journal == "Journal of Example Biomedicine"
    assert first.publication_year == "2021"
    assert first.doi == "10.1000/fake.2021.001"
    assert first.pubmed_url == "https://pubmed.ncbi.nlm.nih.gov/11111111/"


def test_structured_abstract_keeps_labels(articles):
    abstract = articles[0].abstract
    assert abstract is not None
    assert abstract.startswith("BACKGROUND: ")
    assert "METHODS: " in abstract


def test_inline_markup_is_stripped_from_title(articles):
    # <i>breast cancer</i> inside ArticleTitle must not leak tags
    assert "<i>" not in (articles[0].title or "")


# ---------------------------------------------------------------------------
# 5. Missing abstract / 6. missing DOI and other degraded records
# ---------------------------------------------------------------------------

def test_record_without_abstract_yields_none(articles):
    assert articles[1].abstract is None


def test_record_without_doi_yields_none(articles):
    assert articles[1].doi is None


def test_degraded_record_fallbacks(articles):
    old = articles[1]
    assert old.authors == ["International Example Consortium"]  # collective name
    assert old.journal == "Old Example Journal"  # MedlineTA fallback
    assert old.publication_year == "2020"  # from MedlineDate "2020 Jan-Feb"

    epub = articles[2]
    assert epub.authors == []  # no AuthorList
    assert epub.journal == "Epublish J"  # ISOAbbreviation fallback
    assert epub.publication_year == "2019"  # ArticleDate fallback
    assert epub.doi is None


def test_pubmed_url_is_always_derived_from_pmid():
    article = Article(pmid="999", title=None, abstract=None)
    assert article.pubmed_url == "https://pubmed.ncbi.nlm.nih.gov/999/"


# ---------------------------------------------------------------------------
# Malformed responses
# ---------------------------------------------------------------------------

def test_malformed_esearch_json_raises(monkeypatch):
    monkeypatch.setattr(pubmed, "_http_get", _fake_http({pubmed.ESEARCH_URL: "<html>oops"}))
    with pytest.raises(PubMedError, match="malformed JSON"):
        search_pmids("TP53")


def test_esearch_ncbi_error_raises(monkeypatch):
    error_payload = {"header": {"type": "Parameters", "error": "Invalid db name"}}
    monkeypatch.setattr(pubmed, "_http_get", _fake_http({pubmed.ESEARCH_URL: json.dumps(error_payload)}))
    with pytest.raises(PubMedError, match="Invalid db name"):
        search_pmids("TP53")


def test_esearch_missing_idlist_raises(monkeypatch):
    monkeypatch.setattr(pubmed, "_http_get", _fake_http({pubmed.ESEARCH_URL: json.dumps({"esearchresult": {}})}))
    with pytest.raises(PubMedError, match="idlist"):
        search_pmids("TP53")


def test_malformed_efetch_xml_raises():
    with pytest.raises(PubMedError, match="malformed XML"):
        parse_pubmed_xml("<PubmedArticleSet><PubmedArticle>oops")


def test_http_non_200_raises_without_retry(monkeypatch):
    """4xx client errors must fail fast instead of being retried."""

    class FakeResponse:
        status_code = 400
        text = "bad request"

    calls = []

    def fake_requests_get(url, params=None, timeout=None):
        calls.append(url)
        return FakeResponse()

    monkeypatch.setattr(pubmed.requests, "get", fake_requests_get)
    with pytest.raises(PubMedError, match="HTTP 400"):
        pubmed._http_get(pubmed.ESEARCH_URL, {"db": "pubmed"})
    assert len(calls) == 1


def test_http_retries_transient_errors(monkeypatch, monkeypatch_no_sleep):
    class FakeResponse:
        status_code = 503
        text = "service unavailable"

    calls = []

    def fake_requests_get(url, params=None, timeout=None):
        calls.append(url)
        return FakeResponse()

    monkeypatch.setattr(pubmed.requests, "get", fake_requests_get)
    with pytest.raises(PubMedError, match="attempt 3/3"):
        pubmed._http_get(pubmed.ESEARCH_URL, {"db": "pubmed"})
    assert len(calls) == pubmed.MAX_RETRIES


@pytest.fixture
def monkeypatch_no_sleep(monkeypatch):
    monkeypatch.setattr(pubmed.time, "sleep", lambda _seconds: None)


# ---------------------------------------------------------------------------
# Offline end-to-end composition
# ---------------------------------------------------------------------------

_ARTICLE_RE = re.compile(r"<PubmedArticle>.*?</PubmedArticle>", re.DOTALL)
_PMIID_RE = re.compile(r"<PMID[^>]*>(\d+)</PMID>")


def test_search_pubmed_composes_esearch_then_efetch(monkeypatch):
    requested = {}

    def fake_get(url: str, params: dict) -> str:
        requested[url] = params
        if url == pubmed.ESEARCH_URL:
            return json.dumps(ESEARCH_JSON_FULL)
        # Model NCBI behaviour: EFetch returns only the requested records.
        keep = set(params["id"].split(","))
        records = _ARTICLE_RE.findall(EFETCH_XML)
        selected = [r for r in records if _PMIID_RE.search(r).group(1) in keep]
        return "<PubmedArticleSet>" + "".join(selected) + "</PubmedArticleSet>"

    monkeypatch.setattr(pubmed, "_http_get", fake_get)
    articles = search_pubmed("TP53 AND breast cancer", max_results=2)

    assert [a.pmid for a in articles] == ["11111111", "22222222"]
    assert requested[pubmed.ESEARCH_URL]["term"] == "TP53 AND breast cancer"
    assert requested[pubmed.ESEARCH_URL]["retmax"] == "2"
    assert requested[pubmed.ESEARCH_URL]["retmode"] == "json"
    assert requested[pubmed.EFETCH_URL]["db"] == "pubmed"
    assert requested[pubmed.EFETCH_URL]["id"] == "11111111,22222222"
    assert requested[pubmed.EFETCH_URL]["retmode"] == "xml"


def test_search_pubmed_accepts_query_with_padding(monkeypatch):
    monkeypatch.setattr(
        pubmed, "_http_get", _fake_http({pubmed.ESEARCH_URL: json.dumps(ESEARCH_JSON_EMPTY)})
    )
    assert search_pubmed("  TP53  ") == []


# ---------------------------------------------------------------------------
# Real network smoke test (skipped by default)
# ---------------------------------------------------------------------------

@pytest.mark.network
def test_real_pubmed_smoke():
    """Hits the live NCBI API. Run explicitly with: pytest -m network"""
    articles = search_pubmed("TP53 AND breast cancer", max_results=3)
    assert 0 < len(articles) <= 3
    for article in articles:
        assert article.pmid.isdigit()
        assert article.pubmed_url.startswith("https://pubmed.ncbi.nlm.nih.gov/")
    first = articles[0]
    assert first.title  # real PubMed records essentially always have a title
