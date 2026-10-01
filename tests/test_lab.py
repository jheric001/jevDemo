"""Offline checks for the question-type lab at /lab. No paid APIs."""

import json
from pathlib import Path

import pytest

from jev_ultrafast import demo, model

ITEMS = json.loads((Path(demo.ROOT) / "static" / "lab-items.json").read_text())
NOUL = {"urgent": {"type": "noul", "instructions": "這封信帶有急迫性"}}


@pytest.fixture
def jev(monkeypatch):
    calls = []

    def fake_post(url, key, body):
        calls.append((url, key, body))
        return {"model": "jev-test", "answers": {"urgent": {"type": "noul", "noul": 0.9}}, "usage": {"input_tokens": 500}}

    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    monkeypatch.setenv("TYPESAFE_MODEL", "jev-latest")
    monkeypatch.setattr(model, "post_json", fake_post)
    return calls


@pytest.mark.parametrize(
    "body",
    [
        {"state": "", "questions": NOUL},
        {"state": "x" * 4001, "questions": NOUL},
        {"state": "hi", "questions": []},
        {"state": "hi", "questions": {}},
        {"state": "hi", "questions": {f"q{i}": NOUL["urgent"] for i in range(21)}},
        {"state": "hi", "questions": {"q": {"type": "text", "instructions": "寫一段話"}}},
        {"state": "hi", "questions": {"q": {"type": "noul"}}},
    ],
)
def test_lab_rejects_bad_requests(jev, body):
    with pytest.raises(ValueError):
        demo.lab_ask(body)
    assert jev == []


def test_lab_forwards_state_and_questions_unchanged(jev):
    result = demo.lab_ask({"state": "  系統壞了三天  ", "questions": NOUL})
    url, key, body = jev[0]
    assert url == "https://api.typesafe.ai/v1/systemone"
    assert key == "test-key"
    assert body == {"state": "系統壞了三天", "model": "jev-latest", "questions": NOUL}
    assert result["answers"]["urgent"]["noul"] == 0.9
    assert result["cost_usd"] == pytest.approx(500 * demo.JEV_USD_PER_MTOK / 1_000_000)


def test_lab_requires_api_key(jev, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY")
    with pytest.raises(ValueError, match="TYPESAFE_API_KEY"):
        demo.lab_ask({"state": "hi", "questions": NOUL})


@pytest.mark.parametrize("item", ITEMS, ids=[i["id"] for i in ITEMS])
def test_every_bank_item_passes_validation(jev, item):
    for state in [item["state"], *(v["state"] for v in item["variants"])]:
        demo.lab_ask({"state": state, "questions": item["questions"]})
