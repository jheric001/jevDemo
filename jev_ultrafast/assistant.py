"""The assistant session: plan with a strong model, then run typed steps on one browser tab.

Jev handles interactive steps; the program handles searching, navigation, candidate selection, capture, the report
and the Markdown archive.
"""

import threading
import time
import uuid
from pathlib import Path

from . import archive, capture, sites
from .agent import Agent
from .browser import Browser
from .planner import describe_step, make_plan, validate_plan
from .usage import jev_cost_usd, task_usage

STATUS_ZH = {"done": "完成", "blocked": "無法繼續", "ready": "進行中", "predicted": "進行中"}


class Session:
    def __init__(
        self,
        task_zh,
        site_keys,
        custom_url,
        max_items,
        *,
        search_mode="url",
        ledger=None,
        collections_root=None,
        reports_root=None,
    ):
        self.task_id = uuid.uuid4().hex[:12]
        self.task_zh = task_zh
        self.sites = list(site_keys) or ["url"]
        self.custom_url = custom_url or ""
        self.site = self.sites[0]
        self.start_url = sites.home_url(self.site, self.custom_url) or self.custom_url
        self.max_items = max_items
        self.search_mode = "jev" if search_mode == "jev" else "url"
        self.ledger = ledger
        self.reports_root = Path(reports_root or Path.cwd() / "reports")
        self.collection = capture.Collection(collections_root or Path.cwd() / "artifacts" / "collections", self.task_id)
        self.plan = None
        self.browser = None
        self.thread = None
        self.stop_flag = threading.Event()
        self.started_at = time.time()
        self.run_started = None
        self.usage = {
            "jev_requests": 0,
            "jev_input_tokens": 0,
            "jev_output_tokens": 0,
            "jev_cost_usd": 0.0,
            "helper_calls": 0,
            "helper_cost_usd": 0.0,
            "llm_calls": 0,
            "llm_cost_usd": 0.0,
            "planner_calls": 0,
            "planner_cost_usd": 0.0,
            "judge_calls": 0,
            "judge_cost_usd": 0.0,
            "actions": 0,
            "elapsed_ms": 0,
            "records": 0,
            "status": "planning",
        }
        self.state = {
            "task_id": self.task_id,
            "task_zh": task_zh,
            "site": self.site,
            "sites": self.sites,
            "site_names": [self.site_label(s) for s in self.sites],
            "custom_url": self.custom_url,
            "search_mode": self.search_mode,
            "start_url": self.start_url,
            "max_items": max_items,
            "phase": "planning",
            "plan": None,
            "plan_meta": None,
            "keywords": [],
            "steps": [],
            "current_step": None,
            "current_item": None,
            "searches": [],
            "candidates": [],
            "records": [],
            "report_md": None,
            "report_file": None,
            "page": None,
            "jev": None,
            "log": [],
            "error": None,
            "collection_dir": str(self.collection.dir),
            "reports_dir": str(self.reports_root),
        }

    # ---------- bookkeeping ----------
    def site_label(self, key):
        return self.custom_url if key == "url" else sites.site_name(key)

    def log(self, level, text):
        self.state["log"].append({"t": time.strftime("%H:%M:%S"), "level": level, "text": str(text)[:400]})
        del self.state["log"][:-400]

    def usage_add_llm(self, meta):
        if not meta:
            return
        self.usage["llm_calls"] += 1
        self.usage["llm_cost_usd"] += meta["cost_usd"]
        prefix = "planner" if meta["kind"] == "planner" else "judge"
        self.usage[f"{prefix}_calls"] += 1
        self.usage[f"{prefix}_cost_usd"] += meta["cost_usd"]

    def usage_add_agent(self, agent):
        summary = task_usage(agent.state)
        for key in (
            "jev_requests",
            "jev_input_tokens",
            "jev_output_tokens",
            "helper_calls",
            "helper_cost_usd",
            "actions",
        ):
            self.usage[key] += summary[key]
        self.usage["jev_cost_usd"] = jev_cost_usd(self.usage["jev_input_tokens"])

    def tick_usage(self):
        if self.run_started:
            self.usage["elapsed_ms"] = round((time.perf_counter() - self.run_started) * 1000)
        self.usage["records"] = len(self.state["records"])
        self.usage["status"] = self.state["phase"]

    def record_ledger(self):
        if not self.ledger:
            return
        self.tick_usage()
        meta = {
            "goal": self.task_zh,
            "goal_zh": self.task_zh,
            "scenario": "+".join(self.sites),
            "start_url": self.start_url,
            "started_at_epoch": self.started_at,
            "kind": "assistant",
        }
        self.ledger.record_task(self.task_id, meta, dict(self.usage))

    def snapshot(self):
        self.tick_usage()
        return {**self.state, "usage": dict(self.usage)}

    def is_running(self):
        return self.thread is not None and self.thread.is_alive()

    # ---------- planning ----------
    def make_plan(self):
        self.state["phase"] = "planning"
        try:
            plan, meta = make_plan(self.task_zh, self.sites, self.custom_url, self.max_items, self.search_mode)
        except Exception as error:
            self.state.update(phase="failed", error=f"任務分析失敗：{error}")
            self.log("error", self.state["error"])
            self.record_ledger()
            raise ValueError(self.state["error"]) from None
        self.usage_add_llm(meta)
        self.set_plan(plan, meta)
        self.record_ledger()

    def set_plan(self, plan, meta=None):
        self.plan = plan
        if plan.get("sites"):
            self.sites = list(plan["sites"])
        self.state.update(
            plan=plan,
            plan_meta=meta,
            keywords=list(plan.get("keywords") or []),
            sites=self.sites,
            site_names=[self.site_label(s) for s in self.sites],
            steps=[
                {"index": i, "text": describe_step(s, plan), "type": s["type"], "status": "pending"}
                for i, s in enumerate(plan["steps"])
            ],
            phase="planned",
        )
        self.log("info", f"任務分析完成：{plan['summary_zh'] or '已產生步驟'}")

    def replace_plan(self, raw_plan):
        plan = validate_plan(
            raw_plan,
            self.max_items,
            task_zh=self.task_zh,
            allowed_sites=self.sites,
            default_sites=self.sites,
            search_mode=self.search_mode,
        )
        self.set_plan(plan, self.state.get("plan_meta"))

    def apply_overrides(self, body):
        """Keyword, site and page-count edits from the plan card."""
        if not self.plan:
            return
        changed = False
        if isinstance(body.get("keywords"), list):
            words = []
            for raw in body["keywords"]:
                word = str(raw).strip()
                if word and word not in words:
                    words.append(word)
            if words:
                self.plan["keywords"] = words[:8]
                changed = True
        if isinstance(body.get("sites"), list):
            chosen = [s for s in body["sites"] if s in self.sites]
            if chosen:
                self.plan["sites"] = chosen
                changed = True
        if body.get("max_pages"):
            try:
                self.plan["max_pages"] = max(1, min(int(body["max_pages"]), 5))
                changed = True
            except (TypeError, ValueError):
                pass
        if changed:
            self.set_plan(self.plan, self.state.get("plan_meta"))

    # ---------- running ----------
    def start(self):
        if self.state["phase"] not in {"planned", "stopped", "failed", "done"} or not self.plan:
            raise ValueError("請先完成任務分析")
        if self.is_running():
            raise ValueError("任務已在執行中")
        self.stop_flag.clear()
        self.state.update(phase="running", error=None, report_md=None, report_file=None, searches=[], candidates=[])
        for step in self.state["steps"]:
            step["status"] = "pending"
        self.thread = threading.Thread(target=self.run, name="assistant-run", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_flag.set()
        self.log("warn", "收到停止指令，會在目前這一步結束後停下")

    def run(self):
        self.run_started = time.perf_counter()
        try:
            self.log("info", f"開啟起始頁：{self.start_url}")
            self.browser = Browser(self.start_url)
            self.update_page()
            for index, step in enumerate(self.plan["steps"]):
                if self.stop_flag.is_set():
                    break
                self.state["current_step"] = index
                self.state["steps"][index]["status"] = "running"
                self.log("step", f"步驟 {index + 1}：{describe_step(step, self.plan)}")
                try:
                    self.run_step(step)
                    self.state["steps"][index]["status"] = "done"
                except Exception as error:
                    self.state["steps"][index]["status"] = "failed"
                    self.log("error", f"步驟 {index + 1} 失敗：{error}")
                    if step["type"] in {"navigate", "browse"} and index == 0:
                        raise
            self.state["phase"] = "stopped" if self.stop_flag.is_set() else "done"
            self.log(
                "info", "已停止。" if self.stop_flag.is_set() else f"全部完成，共收錄 {len(self.state['records'])} 筆。"
            )
            self.archive()
        except Exception as error:
            self.state.update(phase="failed", error=str(error)[:400])
            self.log("error", f"任務中止：{error}")
        finally:
            self.state["current_step"] = None
            self.state["current_item"] = None
            self.record_ledger()
            self.close_browser()

    def archive(self):
        if not self.state["records"] and not self.state.get("report_md"):
            return
        try:
            self.tick_usage()
            path = archive.write_report(self.reports_root, self.state, self.usage, self.plan)
            self.state["report_file"] = path.name
            self.log("ok", f"報告已存檔：{path}")
        except Exception as error:
            self.log("error", f"報告存檔失敗：{error}")

    def run_step(self, step, candidate=None):
        kind = step["type"]
        if kind == "search_collect":
            self.run_search_collect(step)
        elif kind == "browse":
            self.run_browse(step)
        elif kind == "navigate":
            self.navigate(step["url"])
        elif kind == "collect_candidates":
            self.run_collect(step)
        elif kind == "foreach_candidate":
            self.run_foreach(step)
        elif kind == "capture":
            self.run_capture(step, candidate)
        elif kind == "report":
            self.run_report(step)

    # ---------- searching ----------
    def run_search_collect(self, step):
        criteria = step.get("criteria_zh") or self.plan["criteria_zh"]
        limit = min(step.get("max_items", self.max_items), self.max_items)
        keywords = list(self.plan.get("keywords") or [])
        site_keys = list(self.plan.get("sites") or self.sites)
        seen = {}

        def search_round(words):
            for site_key in site_keys:
                for word in words:
                    if self.stop_flag.is_set() or len(seen) >= limit * 2:
                        return
                    try:
                        self.search_one(site_key, word, criteria, limit, seen)
                    except Exception as error:
                        self.log("error", f"{self.site_label(site_key)}｜「{word}」搜尋失敗：{error}")

        search_round(keywords)
        if step.get("learn_keywords", True) and seen and not self.stop_flag.is_set():
            extra, meta = capture.learn_keywords(criteria, keywords, list(seen.values()))
            self.usage_add_llm(meta)
            if extra:
                self.log("info", f"從首輪結果學到新關鍵字：{'、'.join(extra)}，再搜一輪")
                self.plan["keywords"] = keywords + extra
                self.state["keywords"] = self.plan["keywords"]
                search_round(extra)
            else:
                self.log("detail", "首輪結果沒有學到新的關鍵字")
        self.state["candidates"] = list(seen.values())
        self.log("info", f"搜尋完成，共 {len(seen)} 個不重複的候選項目，接下來最多收錄 {min(len(seen), limit)} 筆")

    def search_one(self, site_key, keyword, criteria, limit, seen):
        label = self.site_label(site_key)
        pages = sites.max_pages_for(site_key, self.plan.get("max_pages", 2))
        for page_no in range(1, pages + 1):
            if self.stop_flag.is_set():
                return
            url = (
                None
                if self.plan.get("search_mode") == "jev" and page_no == 1
                else sites.search_url(site_key, keyword, page_no)
            )
            if url:
                self.navigate(url)
            elif page_no == 1:
                self.navigate(sites.home_url(site_key, self.custom_url) or self.start_url)
                self.run_browse(
                    {
                        "goal_en": f'Search for "{keyword}" using the site search box and submit the search. '
                        "Stop when the search results are visible.",
                        "goal_zh": f"用站內搜尋「{keyword}」",
                        "max_actions": 12,
                    }
                )
            else:
                return
            capture.expand_results(self.browser)
            page = capture.read_page(self.browser)
            found, meta = capture.filter_candidates(criteria, page, limit)
            self.usage_add_llm(meta)
            new = 0
            for candidate in found:
                key = capture.item_key(candidate["url"])
                if key in seen:
                    continue
                candidate.update(site=site_key, site_name=label, keyword=keyword)
                seen[key] = candidate
                new += 1
            links = len(page.get("links") or [])
            self.state["searches"].append(
                {"site": label, "keyword": keyword, "page": page_no, "links": links, "new": new}
            )
            self.log(
                "info", f"{label}｜「{keyword}」第 {page_no} 頁：{links} 個連結，新增 {new} 個候選（累計 {len(seen)}）"
            )
            if new == 0 or len(seen) >= limit * 2:
                return

    # ---------- steps ----------
    def run_browse(self, step):
        agent = Agent.attach(self.browser, step["goal_en"], screenshots=True)
        self.state["jev"] = {
            "goal_en": step["goal_en"],
            "goal_zh": step.get("goal_zh"),
            "history": [],
            "status": "ready",
        }
        limit = step.get("max_actions", 20)
        try:
            while agent.state["status"] not in {"done", "blocked"} and not self.stop_flag.is_set():
                if len(agent.state["history"]) >= limit:
                    self.log("warn", f"Jev 已達這一步的動作上限（{limit}），先繼續下一步")
                    break
                # A page that keeps changing (animated calendars, price refreshes) makes every decision stale.
                # Stop burning requests after a dozen decisions that never executed.
                if len(agent.state["decisions"]) - len(agent.state["history"]) > 12:
                    self.log("warn", "Jev 連續多次決策都因頁面持續變動而無法執行，提前結束這一步")
                    break
                try:
                    agent.command("tick")
                except ValueError as error:
                    self.log("warn", f"Jev 這一步中止：{error}")
                    break
                self.sync_jev(agent)
            self.sync_jev(agent)
            status = agent.state["status"]
            self.log("info", f"Jev 回報「{STATUS_ZH.get(status, status)}」，共 {len(agent.state['history'])} 個動作")
        finally:
            self.usage_add_agent(agent)
            agent.close()

    def sync_jev(self, agent):
        page = agent.state["page"]
        self.state["page"] = {"url": page["url"], "title": page["title"], "screenshot": page.get("screenshot")}
        self.state["jev"].update(
            status=agent.state["status"],
            history=[
                {k: h.get(k) for k in ("step", "operation", "action", "text", "probability", "page_changed")}
                for h in agent.state["history"][-8:]
            ],
        )

    def navigate(self, url):
        self.browser.call("Page.navigate", url=url)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                if self.browser.evaluate("document.readyState") == "complete":
                    break
            except Exception:
                pass
            time.sleep(0.05)
        time.sleep(1.2)
        self.update_page()

    def update_page(self):
        try:
            info = self.browser.evaluate("({url: location.href, title: document.title})") or {}
            shot = self.browser.call("Page.captureScreenshot", format="jpeg", quality=55).get("data")
            self.state["page"] = {"url": info.get("url"), "title": info.get("title"), "screenshot": shot}
        except Exception:
            pass

    def run_collect(self, step):
        capture.expand_results(self.browser)
        page = capture.read_page(self.browser)
        criteria = step.get("criteria_zh") or self.plan["criteria_zh"]
        limit = min(step.get("max_items", self.max_items), self.max_items)
        candidates, meta = capture.filter_candidates(criteria, page, limit)
        self.usage_add_llm(meta)
        for candidate in candidates:
            candidate.update(site=self.site, site_name=self.site_label(self.site), keyword="")
        self.state["candidates"] = candidates
        self.log("info", f"頁面上有 {len(page.get('links') or [])} 個連結，判斷後保留 {len(candidates)} 個候選項目")

    def run_foreach(self, step):
        candidates = self.state["candidates"][: self.max_items]
        if not candidates:
            self.log("warn", "沒有符合條件的候選項目，略過逐一收錄")
            return
        children = step.get("steps") or [{"type": "capture", "note_zh": ""}]
        for index, candidate in enumerate(candidates, 1):
            if self.stop_flag.is_set():
                break
            self.state["current_item"] = {"index": index, "total": len(candidates), "title": candidate["title"]}
            self.log(
                "info",
                f"第 {index}/{len(candidates)} 筆（{candidate.get('site_name', '')}）：{candidate['title'][:50]}",
            )
            try:
                self.navigate(candidate["url"])
                for child in children:
                    self.run_step(child, candidate=candidate)
            except Exception as error:
                self.log("error", f"第 {index} 筆處理失敗：{error}")
        self.state["current_item"] = None

    def run_capture(self, step, candidate=None):
        page = capture.read_page(self.browser)
        record, meta = capture.extract_record(
            self.plan["collection"], self.plan["criteria_zh"], page, step.get("note_zh", "")
        )
        self.usage_add_llm(meta)
        record["url"] = page.get("url")
        record["title"] = page.get("title")
        record["site"] = (candidate or {}).get("site_name") or self.site_label(self.site)
        if candidate:
            record["keyword"] = candidate.get("keyword")
            record["list_title"] = candidate.get("title")
            record["list_price_twd"] = candidate.get("price_twd")
            record["candidate_reason_zh"] = candidate.get("reason_zh")
        try:
            shot = self.browser.call("Page.captureScreenshot", format="jpeg", quality=50).get("data")
        except Exception:
            shot = None
        saved = self.collection.add(record, shot)
        self.state["records"].append(saved)
        verdict = "" if saved.get("matches_criteria") in (True, None) else "（判定不符合條件）"
        self.log("ok", f"已收錄第 {saved['id']} 筆：{archive.record_label(saved) or saved['url']}{verdict}")

    def run_report(self, step):
        if not self.state["records"] and self.browser is not None:
            # Nothing captured yet (results may be buttons, not links): capture the current page so the report has data.
            self.log("warn", "還沒有收錄任何資料，先收錄目前這一頁再寫報告")
            try:
                self.run_capture({"type": "capture", "note_zh": "收錄目前頁面上可見的結果"})
            except Exception as error:
                self.log("error", f"保底收錄失敗：{error}")
        if not self.state["records"]:
            self.log("warn", "沒有收錄任何資料，略過報告")
            return
        report, meta = capture.write_report(
            self.task_zh, self.plan, self.state["records"], step.get("instructions_zh", "")
        )
        self.usage_add_llm(meta)
        self.state["report_md"] = report
        self.collection.save_report(report)
        self.log("ok", "報告已產生")

    def close_browser(self):
        browser, self.browser = self.browser, None
        if browser:
            try:
                browser.close()
            except Exception:
                pass

    def close(self):
        self.stop_flag.set()
        if self.is_running():
            self.thread.join(timeout=5)
        self.close_browser()
