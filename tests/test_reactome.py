"""Offline tests for the Phase 5 Reactome tool.

HTTP is faked by monkeypatching ``app.tools.reactome._post_identifiers`` /
``requests.post``; fixtures mirror the real Analysis Service response
verified on 2026-09-20 (TP53 + Homo sapiens).
"""

from __future__ import annotations

import json

import pytest
import requests

import app.tools.reactome as reactome
from app.models.schemas import ReactomePathway
from app.tools.reactome import ReactomeError, get_reactome_pathways


def analysis_pathway(st_id="R-HSA-6804754", name="Regulation of TP53 Expression",
                     species="Homo sapiens", in_disease=False):
    return {
        "stId": st_id,
        "name": name,
        "dbId": 12345,
        "species": {"dbId": 48887, "taxId": "9606", "name": species},
        "inDisease": in_disease,
        "llp": True,
        "entities": {"resource": "TOTAL", "total": 4, "found": 2, "pValue": 1e-7},
    }


def analysis_payload(pathways, found=None):
    return {
        "summary": {"identifier": "TP53"},
        "pathwaysFound": found if found is not None else len(pathways),
        "pathways": pathways,
        "identifiersNotFound": 0,
    }


@pytest.fixture
def post(monkeypatch):
    state = {"payload": None, "status": 200, "text": "", "error": None,
             "requests": []}

    class FakeResponse:
        def __init__(self, payload, status, text):
            self._payload = payload
            self.status_code = status
            self.text = text

        def json(self):
            if self._payload is None:
                raise ValueError("no json")
            return self._payload

    def fake_post(url, params=None, data=None, headers=None, timeout=None):
        state["requests"].append(
            {"url": url, "params": params, "data": data, "headers": headers}
        )
        if state["error"] is not None:
            raise state["error"]
        return FakeResponse(state["payload"], state["status"], state["text"])

    monkeypatch.setattr(reactome.requests, "post", fake_post)
    return state


# ---------------------------------------------------------------------------
# normal path
# ---------------------------------------------------------------------------

def test_normal_pathway_response(post):
    post["payload"] = analysis_payload([
        analysis_pathway(),
        analysis_pathway("R-HSA-9723907", "Loss of Function of TP53 in Cancer",
                         in_disease=True),
    ])
    pathways = get_reactome_pathways("TP53", "Homo sapiens")
    assert len(pathways) == 2
    first = pathways[0]
    assert isinstance(first, ReactomePathway)
    assert first.stable_id == "R-HSA-6804754"
    assert first.name == "Regulation of TP53 Expression"
    assert first.species == "Homo sapiens"
    assert first.is_disease is False
    assert first.is_inferred is None  # endpoint does not report inference
    assert first.url == "https://reactome.org/content/detail/R-HSA-6804754"
    assert pathways[1].is_disease is True


def test_request_shape_and_species_param(post):
    post["payload"] = analysis_payload([analysis_pathway()])
    get_reactome_pathways("TP53", "Homo sapiens", max_results=3)
    request = post["requests"][0]
    assert request["url"] == reactome.ANALYSIS_URL
    assert request["params"]["species"] == "Homo sapiens"
    assert request["params"]["pageSize"] == "3"
    assert request["params"]["page"] == "1"
    assert request["data"] == "TP53"
    assert request["headers"]["Content-Type"] == "text/plain"


def test_gene_and_species_stripped_and_required(post):
    post["payload"] = analysis_payload([analysis_pathway()])
    get_reactome_pathways("  TP53 ", " Homo sapiens ")
    assert post["requests"][0]["data"] == "TP53"
    with pytest.raises(ValueError, match="gene"):
        get_reactome_pathways("", "Homo sapiens")
    with pytest.raises(ValueError, match="species"):
        get_reactome_pathways("TP53", "   ")


@pytest.mark.parametrize("bad", [0, 11, -1, 5.5, "5", None])
def test_max_results_bounds(post, bad):
    with pytest.raises(ValueError, match="max_results"):
        get_reactome_pathways("TP53", "Homo sapiens", max_results=bad)


# ---------------------------------------------------------------------------
# ordering / dedup / limits
# ---------------------------------------------------------------------------

def test_api_order_preserved(post):
    ids = ["R-HSA-1", "R-HSA-2", "R-HSA-3"]
    post["payload"] = analysis_payload(
        [analysis_pathway(i, f"pathway {i}") for i in ids]
    )
    assert [p.stable_id for p in get_reactome_pathways("TP53", "human")] == ids


def test_duplicate_pathways_deduplicated_keep_first(post):
    post["payload"] = analysis_payload([
        analysis_pathway("R-HSA-1", "first name"),
        analysis_pathway("R-HSA-1", "duplicate name"),
        analysis_pathway("R-HSA-2", "second"),
    ])
    pathways = get_reactome_pathways("TP53", "human")
    assert [p.stable_id for p in pathways] == ["R-HSA-1", "R-HSA-2"]
    assert pathways[0].name == "first name"


def test_result_sliced_to_max_results(post):
    post["payload"] = analysis_payload(
        [analysis_pathway(f"R-HSA-{i}", f"p{i}") for i in range(5)]
    )
    assert len(get_reactome_pathways("TP53", "human", max_results=2)) == 2


# ---------------------------------------------------------------------------
# empty / error cases
# ---------------------------------------------------------------------------

def test_empty_result_returns_empty_list(post):
    post["payload"] = analysis_payload([], found=0)
    assert get_reactome_pathways("ZZZNOTAGENE", "Homo sapiens") == []


def test_missing_pathways_field_treated_as_empty(post):
    post["payload"] = {"summary": {}, "pathwaysFound": 0}
    assert get_reactome_pathways("TP53", "human") == []


def test_http_error_raises(post):
    post["status"] = 500
    post["text"] = "internal error"
    with pytest.raises(ReactomeError, match="HTTP 500"):
        get_reactome_pathways("TP53", "human")


def test_malformed_json_raises(post):
    post["payload"] = None  # json() raises ValueError
    post["text"] = "<html>gateway"
    with pytest.raises(ReactomeError, match="malformed JSON"):
        get_reactome_pathways("TP53", "human")


def test_pathways_wrong_type_raises(post):
    post["payload"] = {"pathways": "not-a-list"}
    with pytest.raises(ReactomeError, match="not a list"):
        get_reactome_pathways("TP53", "human")


def test_record_missing_stable_id_raises(post):
    record = analysis_pathway()
    del record["stId"]
    post["payload"] = analysis_payload([record])
    with pytest.raises(ReactomeError, match="missing 'stId'"):
        get_reactome_pathways("TP53", "human")


def test_record_missing_name_raises(post):
    record = analysis_pathway()
    del record["name"]
    post["payload"] = analysis_payload([record])
    with pytest.raises(ReactomeError, match="missing 'name'"):
        get_reactome_pathways("TP53", "human")


def test_network_error_raises(post):
    post["error"] = requests.ConnectionError("connection reset")
    with pytest.raises(ReactomeError, match="Network error"):
        get_reactome_pathways("TP53", "human")
