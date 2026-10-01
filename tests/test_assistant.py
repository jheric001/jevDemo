"""Offline checks for the assistant: plan validation, candidate mapping, collections and the step runner."""

import json

import pytest

from jev_ultrafast import assistant, capture, planner


def sample_plan(max_items=20):
    return {
        "intent": "product_search",
        "summary_zh": "搜尋並收錄水凝尿布 M",
        "criteria_zh": "商品名稱包含「水凝」且尺寸為 M",
        "collection": {"type": "product", "fields": ["name", "brand", "size", "price_twd"]},
        "steps": [
            {
                "type": "browse",
                "goal_en": 'Search for "水凝尿布M". Stop when results are visible.',
                "goal_zh": "搜尋",
                "max_actions": 99,
            },
            {"type": "collect_candidates", "criteria_zh": "水凝 M", "max_items": 500},
            {
                "type": "foreach_candidate",
                "steps": [{"type": "navigate", "url": "https://x"}, {"type": "capture", "note_zh": "價格"}],
            },
            {"type": "report", "instructions_zh": "比較單價"},
        ],
    }


def test_validate_plan_normalises_limits_and_required_fields():
    plan = planner.validate_plan(sample_plan(), 20)
    assert plan["steps"][0]["max_actions"] == 30
    assert plan["steps"][1]["max_items"] == 20
    assert [s["type"] for s in plan["steps"][2]["steps"]] == ["capture"]
    assert {"matches_criteria", "reason_zh", "summary_zh"} <= set(plan["collection"]["fields"])
    assert planner.describe_step(plan["steps"][2]).startswith("逐一開啟")


@pytest.mark.parametrize(
    "bad",
    [
        {"steps": [{"type": "purchase"}]},
        {"steps": []},
        {"steps": [{"type": "browse"}]},
        {"steps": [{"type": "navigate", "url": "javascript:alert(1)"}]},
        {"steps": [{"type": "foreach_candidate", "steps": [{"type": "foreach_candidate"}]}]},
    ],
)
def test_validate_plan_rejects_unsupported_steps(bad):
    with pytest.raises(ValueError):
        planner.validate_plan(bad, 20)


def test_filter_candidates_maps_indices_and_dedupes(monkeypatch):
    page = {
        "title": "搜尋結果",
        "links": [
            {"text": "首頁", "href": "https://shop.test/", "context": ""},
            {"text": "水凝尿布 M 44片", "href": "https://shop.test/a?ref=list", "context": "NT$599"},
            {"text": "水凝尿布 M 44片", "href": "https://shop.test/a?ref=banner&utm=x", "context": "NT$599"},
            {"text": "幫寶適 L", "href": "https://shop.test/b", "context": "NT$700"},
        ],
    }
    answer = {
        "selected": [
            {"i": 1, "title": "水凝尿布 M 44片", "price_twd": 599, "reason_zh": "品牌尺寸符合"},
            {"i": 2},
            {"i": 99},
        ]
    }
    monkeypatch.setattr(capture, "chat_json", lambda *_a, **_k: (answer, {"kind": "judge", "cost_usd": 0.001}))
    selected, meta = capture.filter_candidates("水凝 M", page, 20)
    assert [s["url"] for s in selected] == ["https://shop.test/a?ref=list"]
    assert selected[0]["price_twd"] == 599 and meta["cost_usd"] == 0.001
    assert capture.item_key("https://m.test/GoodsDetail.jsp?i_code=1&mdiv=9") == capture.item_key(
        "https://m.test/GoodsDetail.jsp?i_code=1&kw=x"
    )
    assert capture.item_key("https://m.test/GoodsDetail.jsp?i_code=1") != capture.item_key(
        "https://m.test/GoodsDetail.jsp?i_code=2"
    )


def test_collection_writes_json_csv_and_screenshot(tmp_path):
    collection = capture.Collection(tmp_path, "task1")
    entry = collection.add({"name": "水凝 M", "price_twd": 599, "specs": {"片數": 44}}, screenshot_b64="/9j/4AAQ")
    assert entry["id"] == 1 and entry["screenshot"] == "01.jpg"
    assert json.loads((tmp_path / "task1" / "records.json").read_text(encoding="utf-8"))[0]["name"] == "水凝 M"
    csv_text = (tmp_path / "task1" / "records.csv").read_text(encoding="utf-8-sig")
    assert "price_twd" in csv_text.splitlines()[0] and "599" in csv_text
    assert (tmp_path / "task1" / "01.jpg").exists()


class FakeBrowser:
    def __init__(self, url):
        self.url = url
        self.closed = False

    def call(self, method, **params):
        if method == "Page.navigate":
            self.url = params["url"]
        if method == "Page.captureScreenshot":
            return {"data": "/9j/4AAQ"}
        return {}

    def evaluate(self, expression):
        if "readyState" in expression:
            return "complete"
        return {"url": self.url, "title": "頁面"}

    def close(self):
        self.closed = True


class FakeAgent:
    def __init__(self, browser, goal):
        self.state = {
            "status": "ready",
            "history": [],
            "decisions": [],
            "text_calls": [],
            "page": {"url": browser.url, "title": "結果", "screenshot": "x"},
            "elapsed_ms": 0,
        }

    @classmethod
    def attach(cls, browser, goal, **_kwargs):
        return cls(browser, goal)

    def command(self, _name):
        self.state["history"].append(
            {"step": 1, "operation": "TYPE_TEXT", "action": "搜尋", "text": "水凝尿布M", "probability": 0.9}
        )
        self.state["decisions"].append({"usage": {"input_tokens": 1000, "output_tokens": 50}})
        self.state["status"] = "done"

    def close(self):
        pass


def test_session_runs_plan_end_to_end_with_fakes(monkeypatch, tmp_path):
    monkeypatch.setattr(assistant, "Browser", FakeBrowser)
    monkeypatch.setattr(assistant, "Agent", FakeAgent)
    pages = {"https://shop.test/a": "水凝 M 44片 NT$599", "https://shop.test/b": "水凝 M 60片 NT$799"}
    monkeypatch.setattr(
        capture,
        "read_page",
        lambda browser, scroll=False: {
            "url": browser.url,
            "title": "頁",
            "text": pages.get(browser.url, ""),
            "links": [{"text": "x", "href": u, "context": ""} for u in pages],
        },
    )
    monkeypatch.setattr(
        capture,
        "filter_candidates",
        lambda criteria, page, n: (
            [{"title": "水凝 M", "url": u, "price_twd": None, "reason_zh": "符合"} for u in pages][:n],
            {"kind": "judge", "cost_usd": 0.001},
        ),
    )
    monkeypatch.setattr(
        capture,
        "extract_record",
        lambda collection, criteria, page, note="": (
            {"name": page["text"][:6], "matches_criteria": True},
            {"kind": "judge", "cost_usd": 0.002},
        ),
    )
    monkeypatch.setattr(
        capture,
        "write_report",
        lambda task, plan, records, instructions="": (
            "# 報告\n共 %d 筆" % len(records),
            {"kind": "judge", "cost_usd": 0.003},
        ),
    )
    session = assistant.Session(
        "找水凝尿布 M", ["momo"], "", 1, collections_root=tmp_path, reports_root=tmp_path / "reports"
    )
    session.set_plan(
        planner.validate_plan(sample_plan(), 20), {"kind": "planner", "model": "m", "latency_ms": 1, "cost_usd": 0.01}
    )
    session.run()
    snap = session.snapshot()
    assert snap["phase"] == "done"
    assert len(snap["records"]) == 1 and snap["records"][0]["url"] == "https://shop.test/a"
    assert snap["report_md"].startswith("# 報告")
    assert snap["usage"]["jev_input_tokens"] == 1000 and snap["usage"]["judge_calls"] == 3
    assert [s["status"] for s in snap["steps"]] == ["done", "done", "done", "done"]
    assert (tmp_path / session.task_id / "report.md").exists()
    assert snap["report_file"] and (tmp_path / "reports" / snap["report_file"]).exists()
    assert (tmp_path / "reports" / "index.md").read_text(encoding="utf-8").count("|") > 7


def test_session_stop_flag_ends_the_loop_early(monkeypatch, tmp_path):
    monkeypatch.setattr(assistant, "Browser", FakeBrowser)
    monkeypatch.setattr(assistant, "Agent", FakeAgent)
    session = assistant.Session("任務", ["momo"], "", 5, collections_root=tmp_path, reports_root=tmp_path / "reports")
    session.set_plan(planner.validate_plan({"steps": [{"type": "capture"}, {"type": "capture"}]}, 5))
    session.stop_flag.set()
    session.run()
    assert session.snapshot()["phase"] == "stopped" and session.snapshot()["records"] == []


def test_search_urls_and_page_limits():
    from jev_ultrafast import sites

    assert sites.search_url("momo", "水凝尿布 M", 2).startswith("https://www.momoshop.com.tw/search/%E6%B0%B4%E5%87%9D")
    assert "curPage=2" in sites.search_url("momo", "x", 2)
    assert "start=10" in sites.search_url("google", "x", 2)
    assert sites.search_url("news", "x", 2) is None and sites.search_url("flights", "x") is None
    assert sites.max_pages_for("news", 5) == 1 and sites.max_pages_for("momo", 9) == 5


def test_search_collect_dedupes_across_keywords_and_learns(monkeypatch, tmp_path):
    monkeypatch.setattr(assistant, "Browser", FakeBrowser)
    monkeypatch.setattr(assistant, "Agent", FakeAgent)
    monkeypatch.setattr(capture, "expand_results", lambda browser, **_k: 10)
    found = {
        "水凝尿布M": [("A", "https://shop.test/a?ref=1"), ("B", "https://shop.test/b")],
        "Parasol M": [("A", "https://shop.test/a?ref=2"), ("C", "https://shop.test/c")],
    }

    def read_page(browser, scroll=False):
        return {
            "url": browser.url,
            "title": "結果",
            "text": "",
            "links": [{"text": "x", "href": browser.url, "context": ""}],
        }

    def filter_candidates(criteria, page, n):
        from urllib.parse import unquote, urlparse

        keyword = unquote(urlparse(page["url"]).path.split("/")[-1])
        items = [{"title": t, "url": u, "price_twd": None, "reason_zh": ""} for t, u in found.get(keyword, [])]
        return items, {"kind": "judge", "cost_usd": 0.001}

    monkeypatch.setattr(capture, "read_page", read_page)
    monkeypatch.setattr(capture, "filter_candidates", filter_candidates)
    monkeypatch.setattr(
        capture, "learn_keywords", lambda c, used, cands: (["Parasol M"], {"kind": "judge", "cost_usd": 0.001})
    )
    monkeypatch.setattr(
        capture,
        "extract_record",
        lambda col, crit, page, note="": (
            {"name": page["url"], "matches_criteria": True},
            {"kind": "judge", "cost_usd": 0.001},
        ),
    )
    monkeypatch.setattr(capture, "write_report", lambda *a, **k: ("# ok", {"kind": "judge", "cost_usd": 0.001}))
    session = assistant.Session(
        "找水凝尿布 M", ["momo"], "", 10, collections_root=tmp_path, reports_root=tmp_path / "reports"
    )
    plan = planner.validate_plan(
        {
            "keywords": ["水凝尿布M"],
            "sites": ["momo"],
            "max_pages": 1,
            "steps": [{"type": "search_collect"}, {"type": "foreach_candidate"}, {"type": "report"}],
        },
        10,
        task_zh="找水凝尿布 M",
        allowed_sites=["momo"],
        default_sites=["momo"],
    )
    session.set_plan(plan)
    session.run()
    snap = session.snapshot()
    assert snap["phase"] == "done"
    assert [c["title"] for c in snap["candidates"]] == ["A", "B", "C"]
    assert snap["keywords"] == ["水凝尿布M", "Parasol M"]
    assert [s["keyword"] for s in snap["searches"]] == ["水凝尿布M", "Parasol M"]
    assert len(snap["records"]) == 3 and snap["records"][0]["site"] == "momo 購物網"


def test_archive_lists_and_guards_paths(tmp_path):
    from jev_ultrafast import archive

    state = {
        "task_zh": "找東西",
        "site_names": ["momo 購物網"],
        "records": [
            {
                "id": 1,
                "name": "甲",
                "price_twd": 10,
                "matches_criteria": True,
                "url": "https://x/1",
                "screenshot": "01.jpg",
            }
        ],
        "searches": [],
        "collection_dir": "/tmp/c",
        "report_md": "# 報告",
    }
    usage = {"jev_cost_usd": 0.001, "helper_cost_usd": 0, "llm_cost_usd": 0.02, "elapsed_ms": 5000}
    path = archive.write_report(tmp_path, state, usage, {"keywords": ["甲乙/丙"], "criteria_zh": "條件"})
    assert path.exists() and "甲乙-丙" in path.name
    listed = archive.list_reports(tmp_path)
    assert listed[0]["title"] == "甲乙/丙" and listed[0]["records"] == "1"
    assert "# 報告" in archive.read_report(tmp_path, path.name)
    with pytest.raises(ValueError):
        archive.read_report(tmp_path, "../secret.md")
