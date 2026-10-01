"""Offline checks for the Traditional Chinese console: usage maths, translation contract, start targets."""

import json

import pytest

from jev_ultrafast import demo, translate, usage


def test_task_usage_sums_jev_tokens_and_helper_cost():
    state = {
        "decisions": [{"usage": {"input_tokens": 1000, "output_tokens": 10}}, {"usage": {"input_tokens": 500}}, {}],
        "text_calls": [{"usage": {"cost": 0.00002, "prompt_tokens": 300, "completion_tokens": 20}}],
        "history": [1, 2],
        "elapsed_ms": 1234,
        "status": "done",
    }
    summary = usage.task_usage(state)
    assert summary["jev_requests"] == 3
    assert summary["jev_input_tokens"] == 1500 and summary["jev_output_tokens"] == 10
    assert summary["jev_cost_usd"] == pytest.approx(1500 / 1e6 * usage.JEV_USD_PER_MTOK)
    assert summary["helper_calls"] == 1 and summary["helper_cost_usd"] == pytest.approx(0.00002)
    assert summary["helper_input_tokens"] == 300 and summary["actions"] == 2


def test_ledger_upserts_tasks_and_persists(tmp_path):
    path = tmp_path / "ledger.json"
    ledger = usage.Ledger(path)
    meta = {"goal": "Search", "goal_zh": "搜尋", "started_at_epoch": 1.0}
    one = usage.task_usage({"decisions": [{"usage": {"input_tokens": 100}}], "text_calls": [], "history": []})
    decisions = [{"usage": {"input_tokens": 100}}, {"usage": {"input_tokens": 100}}]
    two = usage.task_usage({"decisions": decisions, "text_calls": [], "history": []})
    ledger.record_task("t1", meta, one)
    ledger.record_task("t1", meta, two)
    ledger.record_translate({"cost": 0.001, "prompt_tokens": 50, "completion_tokens": 5})
    reloaded = usage.Ledger(path)
    totals = reloaded.totals()
    assert totals["tasks"] == 1 and totals["jev_requests"] == 2 and totals["jev_input_tokens"] == 200
    assert totals["translate_calls"] == 1 and totals["openrouter_cost_usd"] == pytest.approx(0.001)
    assert reloaded.recent()[0]["goal_zh"] == "搜尋"


def test_translate_keeps_keywords_and_reports_usage(monkeypatch):
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "test")
    monkeypatch.setenv("TEXT_MODEL", "helper-model")
    monkeypatch.delenv("TRANSLATE_MODEL", raising=False)
    calls = []

    def post(_url, _key, body):
        calls.append(body)
        goal = 'Search for "水凝尿布" on momo. Stop when the search results are visible.'
        return {"choices": [{"message": {"content": json.dumps({"goal_en": goal})}}], "usage": {"cost": 0.0001}}

    monkeypatch.setattr(translate, "post_json", post)
    out = translate.translate_goal("在 momo 搜尋水凝尿布")
    assert "水凝尿布" in out["goal_en"] and out["model"] == "helper-model"
    assert out["usage"]["cost"] == 0.0001
    assert calls[0]["response_format"] == {"type": "json_object"}
    assert "水凝尿布" in calls[0]["messages"][1]["content"]


@pytest.mark.parametrize("content", ['{"goal_en": ""}', '{"goal_en": "x", "extra": 1}', "not json", '{"text": "x"}'])
def test_translate_rejects_invalid_output(monkeypatch, content):
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "test")
    monkeypatch.setattr(translate, "post_json", lambda *_: {"choices": [{"message": {"content": content}}]})
    with pytest.raises(ValueError):
        translate.translate_goal("任務")


def test_translate_surfaces_provider_error_and_needs_credential(monkeypatch):
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "test")
    monkeypatch.setattr(translate, "post_json", lambda *_: {"error": {"message": "provider down"}})
    with pytest.raises(ValueError, match="provider down"):
        translate.translate_goal("任務")
    monkeypatch.delenv("TEXT_MODEL_API_KEY", raising=False)
    with pytest.raises(ValueError):
        translate.translate_goal("任務")


class FakeAgent:
    def __init__(self, url, goal, **_kwargs):
        self.url, self.goal = url, goal
        self.state = {"goal": goal, "decisions": [], "text_calls": [], "history": [], "status": "ready"}
        self.state["elapsed_ms"] = 0

    def snapshot(self):
        return dict(self.state)

    def close(self):
        pass


def test_reset_accepts_site_presets_and_custom_urls(monkeypatch):
    monkeypatch.setattr(demo, "Agent", FakeAgent)
    monkeypatch.setattr(demo, "LEDGER", None)
    state = demo.command("reset", {"scenario": "momo", "goal": "Search", "goal_zh": "搜尋"})
    assert demo.AGENT.url == demo.SITES["momo"]
    assert state["goal_zh"] == "搜尋" and state["usage"]["jev_requests"] == 0 and state["task_id"]
    demo.command("reset", {"scenario": "url", "url": "https://example.test/x", "goal": "Open"})
    assert demo.AGENT.url == "https://example.test/x"
    demo.AGENT = None


@pytest.mark.parametrize(
    "body",
    [
        {"scenario": "url", "url": "ftp://example.test", "goal": "g"},
        {"scenario": "url", "url": "example.test", "goal": "g"},
        {"scenario": "nope", "goal": "g"},
    ],
)
def test_reset_rejects_bad_start_targets(monkeypatch, body):
    monkeypatch.setattr(demo, "Agent", FakeAgent)
    monkeypatch.setattr(demo, "LEDGER", None)
    with pytest.raises(ValueError):
        demo.command("reset", body)
    demo.AGENT = None


class LostTabAgent(FakeAgent):
    def __init__(self, url, goal, **kwargs):
        super().__init__(url, goal, **kwargs)
        self.browser = type("B", (), {"target": "t"})()

    def command(self, _name, _body):
        raise RuntimeError({"code": -32602, "message": "No target with given id found"})


def test_closed_tab_stops_the_run_with_a_chinese_message(monkeypatch):
    monkeypatch.setattr(demo, "Agent", LostTabAgent)
    monkeypatch.setattr(demo, "LEDGER", None)
    demo.command("reset", {"scenario": "momo", "goal": "Search"})
    with pytest.raises(ValueError, match="分頁已被關閉"):
        demo.command("tick", {})
    assert demo.AGENT.state["status"] == "blocked" and demo.AGENT.browser.target is None
    demo.AGENT = None


def test_browser_close_tolerates_an_already_closed_tab(monkeypatch):
    from jev_ultrafast import browser as browser_module

    def cdp(method, **_params):
        raise RuntimeError({"code": -32602, "message": "No target with given id found"})

    monkeypatch.setattr(browser_module, "cdp", cdp)
    tab = browser_module.Browser.__new__(browser_module.Browser)
    tab.target = "gone"
    tab.close()
    assert tab.target is None
