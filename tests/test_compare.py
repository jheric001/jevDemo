"""Offline checks for the three-way comparison demo. No paid APIs, no browser."""

import json
import threading

import pytest

from jev_ultrafast import compare, llm, model
from jev_ultrafast.browser import StalePage, fingerprint


def page():
    state = {
        "url": "https://shop.test/",
        "title": "Shop",
        "text": "Search",
        "scroll": {"y": 0},
        "actions": [
            {"id": "e1", "kind": "fill", "label": "Search", "role": "textbox", "value": "", "node": 10},
            {"id": "e3", "kind": "click", "label": "Go", "role": "button", "value": "", "node": 20},
            {"id": "wait", "kind": "wait", "label": "Wait"},
        ],
    }
    state["fingerprint"] = fingerprint(state)
    return state


@pytest.mark.parametrize(
    "text, price",
    [
        ("Parasol 水凝尿布 M $850", 850),
        ("市售價 $990 特價 $1,280", 990),
        ("滿$1000折$100 本檔 $850", 850),
        ("NT$599 送 44 片", 599),
        ("1,200 元", 1200),
        ("64 片 3 號", None),
    ],
)
def test_parse_price_takes_lowest_real_amount(text, price):
    assert compare.parse_price(text) == price


def test_product_cards_keep_priced_listings_once():
    data = {
        "cards": [
            {
                "title": "水凝尿布 M 64片",
                "href": "https://shop.test/g?i_code=1&ref=a",
                "context": "總銷量>3,000",
                "price": 850,
            },
            {"title": "水凝尿布 M 64片", "href": "https://shop.test/g?i_code=1&ref=b", "context": "", "price": 850},
            {"title": "幫寶適 L 52片", "href": "https://shop.test/g?i_code=2", "context": "特價 $699", "price": None},
            {"title": "沒有價格的商品", "href": "https://shop.test/g?i_code=3", "context": "熱銷", "price": None},
            {"title": "價格欄位怪怪的", "href": "https://shop.test/g?i_code=4", "context": "", "price": 3},
        ]
    }
    cards = compare.product_cards(data)
    assert [c["title"] for c in cards] == ["水凝尿布 M 64片", "幫寶適 L 52片"]
    assert [c["i"] for c in cards] == [0, 1] and [c["price_twd"] for c in cards] == [850, 699]


def test_read_cards_waits_until_the_grid_stops_growing(monkeypatch):
    monkeypatch.setattr(compare.time, "sleep", lambda _s: None)

    def card(n):
        return {"title": f"商品 {n}", "href": f"https://s/g?i_code={n}", "context": "", "price": 100 + n}

    reads = [[], [card(1)], [card(1), card(2)], [card(1), card(2)], [card(1), card(2)], [card(9)]]

    class Grid:
        calls = 0

        def evaluate(self, expression):
            if expression is compare.CARD_READ_JS:
                Grid.calls += 1
                return {"title": "結果", "links": 90, "cards": reads[Grid.calls - 1]}
            return None

    data, cards, _waited = compare.read_cards(Grid(), timeout=60)
    assert Grid.calls == 5 and [c["title"] for c in cards] == ["商品 1", "商品 2"] and data["links"] == 90


def test_llm_decision_answers_the_same_questions_as_jev(monkeypatch):
    seen = {}

    def chat_json(kind, system, user, max_tokens=0):
        seen.update(kind=kind, questions=set(user["questions"]))
        answer = {"operation": "TYPE_TEXT", "type_text_target": 1, "click_target": "999"}
        meta = {"model": "m", "latency_ms": 1200, "cost_usd": 0.0004, "input_tokens": 900, "output_tokens": 20}
        return answer, meta

    monkeypatch.setattr(llm, "chat_json", chat_json)
    decision = compare.choose_llm(page(), "Find a book", [])
    body, *_ = model.decision_request(page(), "Find a book", [])
    assert seen == {"kind": "actor", "questions": set(body["questions"])}
    assert decision["choice"] == "e1" and decision["latency_ms"] == 1200 and decision["cost_usd"] == 0.0004


def test_llm_decision_outside_the_options_is_not_executed(monkeypatch):
    meta = {"model": "m", "latency_ms": 1, "cost_usd": 0, "input_tokens": 1, "output_tokens": 1}
    monkeypatch.setattr(llm, "chat_json", lambda *a, **k: ({"operation": "DELETE_ACCOUNT"}, meta))
    with pytest.raises(ValueError, match="不在清單"):
        compare.choose_llm(page(), "Find a book", [])


def test_jev_pick_fans_out_nouls_and_keeps_likely_matches(monkeypatch):
    cards = [
        {"i": i, "title": f"商品{i}", "context": "", "url": f"https://s/{i}", "price_twd": 100 + i} for i in range(5)
    ]
    bodies = []

    def post(_url, _key, body):
        bodies.append(body)
        noul = {"c0": 0.2, "c1": 0.9, "c2": 0.6, "c3": 0.95, "c4": "bad"}
        return {
            "answers": {q: {"type": "noul", "noul": noul[q]} for q in body["questions"]},
            "usage": {"input_tokens": 1000},
        }

    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", post)
    items, meta = compare.jev_pick("只要 M 號", cards, limit=2, chunk=3)
    assert len(bodies) == 2 and all(q["type"] == "noul" for b in bodies for q in b["questions"].values())
    assert bodies[0]["state"] == {"shopper_request": "只要 M 號"}
    assert [i["title"] for i in items] == ["商品3", "商品1"]
    assert meta["calls"] == 2 and meta["input_tokens"] == 2000
    assert meta["scores"] == {0: 0.2, 1: 0.9, 2: 0.6, 3: 0.95}


def test_price_table_sorts_and_names_the_cheapest():
    table = compare.price_table(
        [
            {"title": "B | 箱購", "url": "https://s/b", "price_twd": 3200},
            {"title": "A", "url": "https://s/a", "price_twd": 850},
        ]
    )
    lines = table.splitlines()
    assert lines[2].startswith("| 1 | [A](https://s/a) | 850 |")
    assert "B ／ 箱購" in lines[3] and "**最便宜**：A，850 元。" in table


class FakeBrowser:
    def __init__(self, url):
        self.url = url
        self.closed = False

    def call(self, method, **params):
        return {"data": "/9j/4AAQ"} if method == "Page.captureScreenshot" else {}

    def evaluate(self, _expression):
        return None

    def observe(self, screenshot=True):
        return {"url": self.url, "actions": [{"id": "e1", "kind": "fill", "label": "搜尋", "node": 1}]}

    def close(self):
        self.closed = True


class FakeAgent:
    created = []

    def __init__(self, browser, goal, decide, type_text):
        self.goal, self.decide, self.type_text = goal, decide, type_text
        self.state = {"status": "ready", "history": [], "decisions": [], "text_calls": []}
        FakeAgent.created.append(self)

    @classmethod
    def attach(cls, browser, goal, *, screenshots=True, decide=None, type_text=None):
        return cls(browser, goal, decide, type_text)

    def command(self, _name):
        self.state["decisions"].append({"latency_ms": 300, "usage": {"input_tokens": 4000}})
        if self.type_text:
            text, helper = self.type_text({})
            self.state["text_calls"].append({**helper, "value": text})
        self.state["history"].append({"action": "search"})
        self.state["status"] = "done"

    def close(self):
        pass


def fake_site(monkeypatch):
    FakeAgent.created.clear()
    monkeypatch.setattr(compare, "connect_browser", lambda: None)
    monkeypatch.setattr(compare, "Browser", FakeBrowser)
    monkeypatch.setattr(compare, "Agent", FakeAgent)
    data = {
        "title": "結果",
        "links": 90,
        "product_links": 2,
        "cards": [
            {"title": "Parasol 水凝尿布 M 64片", "href": "https://s/1", "context": "", "price": 850},
            {"title": "Parasol 水凝尿布 L 52片", "href": "https://s/2", "context": "", "price": 799},
        ],
    }
    monkeypatch.setattr(compare, "read_cards", lambda browser: (data, compare.product_cards(data), 1.6))


def test_pure_jev_run_uses_no_llm_at_all(monkeypatch):
    fake_site(monkeypatch)
    monkeypatch.setattr(llm, "chat_json", lambda *a, **k: pytest.fail("pure Jev must not call an LLM"))
    monkeypatch.setattr(
        compare,
        "jev_pick",
        lambda criteria, cards, limit: ([{**cards[0], "why": "0.97"}], {"calls": 1, "ms": 250, "input_tokens": 600}),
    )
    run = compare.Run("jev", "在 momo 找「水凝尿布M」只要 M 號", "momo", 5, threading.Event())
    run.run()
    s = run.state
    assert s["status"] == "done" and s["keyword"] == "水凝尿布M"
    assert FakeAgent.created[0].decide is None and 'Search for "水凝尿布M"' in FakeAgent.created[0].goal
    assert FakeAgent.created[0].state["text_calls"][0]["value"] == "水凝尿布M"
    assert s["llm_calls"] == 0 and s["llm_ms"] == 0 and s["jev_calls"] == 2 and s["jev_ms"] == 550
    assert [st["actor"] for st in s["steps"]] == ["程式", "Jev", "程式", "Jev", "程式"]
    assert "**最便宜**" in s["report_md"] and s["screenshot"] == "/9j/4AAQ"


def test_pure_jev_needs_a_quoted_keyword(monkeypatch):
    fake_site(monkeypatch)
    run = compare.Run("jev", "幫我找便宜的尿布", "momo", 5, threading.Event())
    run.run()
    assert run.state["status"] == "failed" and "「」" in run.state["error"]
    assert run.state["steps"][0]["status"] == "failed" and FakeAgent.created == []


def test_llm_modes_split_time_between_llm_and_jev(monkeypatch):
    fake_site(monkeypatch)
    meta = {"model": "m", "latency_ms": 2000, "cost_usd": 0.01, "input_tokens": 10, "output_tokens": 10}
    monkeypatch.setattr(
        llm, "chat_json", lambda kind, *a, **k: ({"keyword": "水凝尿布M", "criteria_zh": "Parasol M 號"}, meta)
    )
    monkeypatch.setattr(
        compare.capture,
        "filter_candidates",
        lambda criteria, page, n: ([{"url": "https://s/1", "reason_zh": "M 號"}], {**meta, "latency_ms": 3000}),
    )
    monkeypatch.setattr(
        compare,
        "jev_pick",
        lambda criteria, cards, limit: (
            [{**cards[0], "why": "0.9"}],
            {"calls": 1, "ms": 250, "input_tokens": 600, "scores": {0: 0.9, 1: 0.04}},
        ),
    )
    monkeypatch.setattr(llm, "chat_text", lambda *a, **k: ("# 報告", {**meta, "latency_ms": 5000}))

    hybrid = compare.Run("hybrid", "找水凝尿布 M", "momo", 5, threading.Event())
    hybrid.run()
    h = hybrid.state
    assert h["status"] == "done" and FakeAgent.created[-1].decide is None
    assert h["llm_calls"] == 2 and h["llm_ms"] == 7000 and h["jev_calls"] == 2 and h["jev_ms"] == 550
    assert h["items"][0]["url"] == "https://s/1" and h["report_md"] == "# 報告"
    assert [(c["score"], c["picked"]) for c in h["candidates"]] == [(0.9, True), (0.04, False)]
    assert FakeAgent.created[-1].state["text_calls"][0]["value"] == "水凝尿布M"

    pure = compare.Run("llm", "找水凝尿布 M", "momo", 5, threading.Event())
    pure.run()
    p = pure.state
    assert p["status"] == "done" and FakeAgent.created[-1].decide is compare.choose_llm
    assert p["jev_calls"] == 0 and p["llm_calls"] == 4 and p["llm_ms"] == 2000 + 300 + 3000 + 5000
    assert [st["actor"] for st in p["steps"]] == ["LLM", "LLM", "程式", "LLM", "LLM"]
    typed = FakeAgent.created[-1].state["text_calls"][0]
    assert typed["value"] == "水凝尿布M" and typed["model"] == "直接輸入關鍵字"


TABLE_MD = '# 報告\n| 商品 | 價格 |\n| --- | --- |\n| "A" | 850 |'


@pytest.mark.parametrize(
    "content, expected",
    [(TABLE_MD, TABLE_MD), ("```markdown\n# 報告\n最便宜：A\n```", "# 報告\n最便宜：A")],
)
def test_chat_text_returns_markdown_without_a_json_wrapper(monkeypatch, content, expected):
    seen = {}

    def post(_url, _key, body, kind="judge"):
        seen.update(body)
        return {"choices": [{"message": {"content": content}}], "usage": {"cost": 0.002, "prompt_tokens": 900}}

    monkeypatch.setenv("TEXT_MODEL_API_KEY", "test")
    monkeypatch.setattr(llm, "post_json", post)
    text, meta = llm.chat_text("judge", "rules", {"listings": []})
    assert text == expected and "response_format" not in seen and meta["cost_usd"] == 0.002


def test_chat_text_rejects_an_empty_reply(monkeypatch):
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "test")
    monkeypatch.setattr(llm, "post_json", lambda *a, **k: {"choices": [{"message": {"content": "  "}}]})
    with pytest.raises(ValueError, match="判斷模型沒有回傳內容"):
        llm.chat_text("judge", "rules", "x")


class FailingAgent(FakeAgent):
    """The browser loop gives up on its first tick with one of its English errors."""

    def command(self, _name):
        self.state["decisions"].append({"operation": "TYPE_TEXT", "latency_ms": 300, "usage": {"input_tokens": 900}})
        raise ValueError("Text helper returned no valid field value; nothing typed.")


def test_a_stopped_search_explains_why_in_chinese(monkeypatch):
    fake_site(monkeypatch)
    monkeypatch.setattr(compare, "Agent", FailingAgent)
    understood = {"keyword": "華碩電競螢幕 27吋", "criteria_zh": "華碩 27 吋"}
    monkeypatch.setattr(llm, "chat_json", lambda *a, **k: (understood, dict(LLM_META)))
    run = compare.Run("hybrid", "在 momo 找華碩電競螢幕 27吋", "momo", 5, threading.Event())
    run.run()
    s = run.state
    assert s["status"] == "failed" and "原因：打字模型沒有回傳可用的文字" in s["error"]
    assert "操作中止：打字模型沒有回傳可用的文字" in s["steps"][1]["detail"]
    assert "Text helper" not in s["error"] + s["steps"][1]["detail"]
    assert compare.zh_agent_error(ValueError("Stopped at the 60-action demo budget")) == "達到動作次數上限"


def test_session_runs_modes_in_order_and_records_each(monkeypatch, tmp_path):
    fake_site(monkeypatch)
    monkeypatch.setattr(
        compare,
        "jev_pick",
        lambda criteria, cards, limit: ([{**cards[0], "why": "0.9"}], {"calls": 1, "ms": 250, "input_tokens": 600}),
    )
    recorded = []

    class Ledger:
        def record_task(self, task_id, meta, usage):
            recorded.append((task_id, meta["kind"], usage["status"]))

    session = compare.Session("找「水凝尿布M」", "momo", ["jev", "hybrid"], 3, ledger=Ledger(), log_root=tmp_path)
    monkeypatch.setattr(
        llm, "chat_json", lambda *a, **k: ({"keyword": "水凝尿布M", "criteria_zh": "M 號"}, {**LLM_META})
    )
    monkeypatch.setattr(llm, "chat_text", lambda *a, **k: ("# 報告", dict(LLM_META)))
    session.run()
    snap = session.snapshot()
    assert snap["phase"] == "done" and [r["status"] for r in snap["runs"]] == ["done", "done"]
    assert [r[1:] for r in recorded] == [("compare", "done"), ("compare", "done")]
    logs = sorted(tmp_path.glob("*.json"))
    assert [p.name.split("-")[-1] for p in logs] == ["hybrid.json", "jev.json"]
    saved = json.loads(logs[1].read_text(encoding="utf-8"))
    assert saved["status"] == "done" and len(saved["decisions"]) == 1 and snap["runs"][0]["log_file"] == str(logs[1])


LLM_META = {"model": "m", "latency_ms": 2000, "cost_usd": 0.01, "input_tokens": 10, "output_tokens": 10}


def test_chrome_connection_happens_before_any_mode_is_timed(monkeypatch, tmp_path):
    fake_site(monkeypatch)

    def refuse():
        raise RuntimeError("daemon default didn't come up -- check bu-default.log")

    monkeypatch.setattr(compare, "connect_browser", refuse)
    session = compare.Session("找「水凝尿布M」", "momo", ["llm", "jev"], 3, log_root=tmp_path)
    session.run()
    snap = session.snapshot()
    assert snap["phase"] == "done" and FakeAgent.created == []
    assert all(r["status"] == "failed" and "daemon" in r["error"] and r["total_ms"] is None for r in snap["runs"])


class BlockedAgent(FakeAgent):
    """Answers BLOCKED on the very first look and never acts."""

    def command(self, _name):
        body, *_ = model.decision_request(page(), "Search", [])
        self.state["decisions"].append({"operation": "BLOCKED", "latency_ms": 800, "request": body, "backend": "llm"})
        self.state["status"] = "blocked"


def test_search_that_never_acts_stops_the_mode_instead_of_reading_the_home_page(monkeypatch):
    fake_site(monkeypatch)
    monkeypatch.setattr(compare, "Agent", BlockedAgent)
    monkeypatch.setattr(compare, "read_cards", lambda browser: pytest.fail("must not read the start page"))
    monkeypatch.setattr(
        llm, "chat_json", lambda *a, **k: ({"keyword": "水凝尿布M", "criteria_zh": "M 號"}, dict(LLM_META))
    )
    run = compare.Run("llm", "找水凝尿布 M", "momo", 5, threading.Event())
    run.run()
    s = run.state
    assert s["status"] == "failed" and "搜尋沒有完成" in s["error"] and "BLOCKED" in s["error"]
    assert [st["status"] for st in s["steps"]] == ["done", "failed", "pending", "pending", "pending"]
    assert s["decisions"][0]["operation"] == "BLOCKED" and s["decisions"][0]["confidence"] is None
    assert s["decisions"][0]["search_box"] is True and s["decisions"][0]["elements"] == 2
    assert "LLM 沒有完成搜尋" in s["steps"][1]["detail"]


def test_wait_for_search_box_waits_for_a_fillable_field(monkeypatch):
    monkeypatch.setattr(compare.time, "sleep", lambda _s: None)
    pages = [
        {"actions": [{"kind": "click"}]},
        StalePage("navigating"),
        {"actions": [{"kind": "click"}, {"kind": "fill"}]},
    ]

    class Loading:
        calls = 0

        def observe(self, screenshot=True):
            Loading.calls += 1
            item = pages[Loading.calls - 1]
            if isinstance(item, Exception):
                raise item
            return item

    assert compare.wait_for_search_box(Loading(), timeout=60)[0] is True and Loading.calls == 3

    class Empty:
        def observe(self, screenshot=True):
            return {"actions": []}

    assert compare.wait_for_search_box(Empty(), timeout=0)[0] is False


def test_decision_record_names_the_target_and_keeps_jev_probabilities():
    body, operations, targets, controls = model.decision_request(page(), "Find a book", [])
    answers = {
        "operation": {"choice": "TYPE_TEXT", "confidence": 0.93, "probabilities": {k: 0.0 for k in operations}},
        "type_text_target": {"choice": "1", "confidence": 0.9, "probabilities": {"1": 1.0}},
    }
    answers["operation"]["probabilities"]["TYPE_TEXT"] = 1.0
    decision = {**model.read_decision(answers, operations, targets, controls), "request": body, "latency_ms": 280}
    record = compare.decision_record(1, decision)
    assert record["operation"] == "TYPE_TEXT" and record["target"] == "Search" and record["confidence"] == 0.93
    assert record["top"][0] == ["TYPE_TEXT", 1.0] and record["search_box"] is True
    assert compare.same_page("https://a.test/x/#top", "https://a.test/x") and not compare.same_page(None, "https://a")
