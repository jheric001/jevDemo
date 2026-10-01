"""Loopback-only server: the research assistant at /, the single-goal console at /console, the original demo at /en."""

import atexit
import json
import os
import secrets
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import archive, compare, model
from .agent import Agent
from .assistant import Session
from .questions import MAX_STEPS
from .sites import SITES as SITE_INFO
from .translate import translate_goal
from .usage import JEV_USD_PER_MTOK, Ledger, openrouter_balance, task_usage

ROOT = Path(__file__).parent
PORT = int(os.environ.get("TYPESAFE_DEMO_PORT", "8767"))
ORIGIN = f"http://127.0.0.1:{PORT}"
TOKEN = secrets.token_urlsafe(32)
LOCK = threading.Lock()
ASSISTANT_LOCK = threading.Lock()
COMPARE_LOCK = threading.Lock()
AGENT = None
ASSISTANT = None
COMPARE = None
LEDGER = None
MAX_ITEMS_HARD = 50
REPORTS_ROOT = Path.cwd() / "reports"
SITES = {key: info["home"] for key, info in SITE_INFO.items()}
FILES = {
    "/": ("assistant.html", "text/html"),
    "/assistant.js": ("assistant.js", "text/javascript"),
    "/console": ("zh.html", "text/html"),
    "/zh": ("zh.html", "text/html"),
    "/zh.js": ("zh.js", "text/javascript"),
    "/en": ("index.html", "text/html"),
    "/app.js": ("app.js", "text/javascript"),
    "/style.css": ("style.css", "text/css"),
    "/fixture.html": ("fixture.html", "text/html"),
    "/compare": ("compare.html", "text/html"),
    "/compare.js": ("compare.js", "text/javascript"),
    "/lab": ("lab.html", "text/html"),
    "/lab.js": ("lab.js", "text/javascript"),
    "/lab-items.json": ("lab-items.json", "application/json"),
}
LAB_TYPES = {"choice", "noul", "score"}


def load_environment():
    path = Path.cwd() / ".env"
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                key, value = line.split("=", 1)
                os.environ.setdefault(key, value)


def max_items_default():
    try:
        return max(1, min(int(os.environ.get("ASSISTANT_MAX_ITEMS", "20")), MAX_ITEMS_HARD))
    except ValueError:
        return 20


def start_url(body):
    scenario = body.get("scenario", "flights")
    if scenario in {"travel", "research"}:
        return scenario, f"{ORIGIN}/fixture.html?scenario={scenario}"
    if scenario in SITES:
        return scenario, SITES[scenario]
    if scenario == "url":
        url = str(body.get("url", "")).strip()
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or len(url) > 500:
            raise ValueError("請輸入 http 或 https 開頭的完整網址")
        return scenario, url
    raise ValueError("不支援的起始網站")


def response_state():
    if AGENT:
        state = AGENT.snapshot()
        state["usage"] = task_usage(AGENT.state)
    else:
        state = {"page": None, "status": "idle", "history": [], "decision": None, "usage": None}
    return {**state, "text_model": os.environ.get("TEXT_MODEL", "deepseek-chat"), "max_steps": MAX_STEPS}


def record_usage():
    if LEDGER and AGENT and AGENT.state.get("task_id"):
        state = AGENT.state
        meta = {k: state.get(k) for k in ("goal", "goal_zh", "scenario", "start_url", "started_at_epoch")}
        LEDGER.record_task(state["task_id"], meta, task_usage(state))


def usage_report(refresh=False):
    with LOCK:
        current = task_usage(AGENT.state) if AGENT else None
    prepaid = os.environ.get("JEV_PREPAID_USD")
    text_model = os.environ.get("TEXT_MODEL", "deepseek-chat")
    return {
        "current": current,
        "assistant": ASSISTANT.snapshot()["usage"] if ASSISTANT else None,
        "all_time": LEDGER.totals() if LEDGER else None,
        "recent": LEDGER.recent() if LEDGER else [],
        "openrouter": openrouter_balance(os.environ.get("TEXT_MODEL_API_KEY"), refresh=refresh),
        "jev": {
            "model": os.environ.get("TYPESAFE_MODEL", "jev-latest"),
            "usd_per_mtok": JEV_USD_PER_MTOK,
            "prepaid_usd": float(prepaid) if prepaid else None,
            "console_url": "https://console.typesafe.ai",
        },
        "models": {
            "text": text_model,
            "translate": os.environ.get("TRANSLATE_MODEL") or text_model,
            "planner": os.environ.get("PLANNER_MODEL") or "anthropic/claude-sonnet-5",
            "judge": os.environ.get("JUDGE_MODEL") or "google/gemini-2.5-flash",
        },
    }


def close_browser():
    global AGENT
    if AGENT:
        AGENT.close()
        AGENT = None


def close_assistant():
    global ASSISTANT
    if ASSISTANT:
        ASSISTANT.close()
        ASSISTANT = None


def command(name, body):
    global AGENT
    try:
        if name == "reset":
            scenario, url = start_url(body)
            goal = body.get("goal", "").strip()
            if not goal or len(goal) > 2000:
                raise ValueError("請輸入 1 至 2,000 字的任務")
            goal_zh = str(body.get("goal_zh", "")).strip()[:2000]
            close_browser()
            AGENT = Agent(
                url,
                goal,
                screenshots=True,
                record_dir=Path.cwd() / "artifacts" / "frames" if body.get("record") else None,
            )
            AGENT.state.update(
                scenario=scenario,
                start_url=url,
                goal_zh=goal_zh,
                task_id=uuid.uuid4().hex[:12],
                started_at_epoch=time.time(),
            )
        else:
            if AGENT is None:
                raise ValueError("請先開始一個任務")
            try:
                AGENT.command(name, body)
            except Exception as error:
                if "No target with given id" not in str(error):
                    raise
                AGENT.browser.target = None
                AGENT.state.update(status="blocked", decision=None)
                raise ValueError("任務用的瀏覽器分頁已被關閉或失效，請重新按「開始任務」") from None
    finally:
        record_usage()
    return response_state()


def translate(body):
    goal_zh = str(body.get("goal_zh", "")).strip()
    if not goal_zh or len(goal_zh) > 2000:
        raise ValueError("請輸入 1 至 2,000 字的任務")
    result = translate_goal(goal_zh)
    if LEDGER:
        LEDGER.record_translate(result["usage"])
    return {**result, "cost_usd": float((result["usage"] or {}).get("cost") or 0)}


def assistant_command(name, body):
    global ASSISTANT
    with ASSISTANT_LOCK:
        if name == "plan":
            if ASSISTANT and ASSISTANT.is_running():
                raise ValueError("上一個任務還在執行，請先按「停止」")
            task_zh = str(body.get("task_zh", "")).strip()
            if not task_zh or len(task_zh) > 2000:
                raise ValueError("請輸入 1 至 2,000 字的任務")
            site_keys = [s for s in (body.get("sites") or []) if isinstance(s, str) and s in SITE_INFO]
            custom_url = str(body.get("url") or "").strip()
            if custom_url:
                parsed = urlparse(custom_url)
                if parsed.scheme not in {"http", "https"} or not parsed.netloc or len(custom_url) > 500:
                    raise ValueError("請輸入 http 或 https 開頭的完整網址")
                site_keys.append("url")
            if not site_keys:
                raise ValueError("請至少勾選一個網站，或填一個自訂網址")
            try:
                max_items = int(body.get("max_items") or max_items_default())
            except (TypeError, ValueError):
                max_items = max_items_default()
            max_items = max(1, min(max_items, MAX_ITEMS_HARD))
            close_assistant()
            ASSISTANT = Session(
                task_zh,
                site_keys,
                custom_url,
                max_items,
                search_mode="jev",  # 搜尋一律由 Jev 在站內搜尋框打字，不用搜尋網址樣板
                ledger=LEDGER,
                reports_root=REPORTS_ROOT,
            )
            ASSISTANT.make_plan()
        elif name == "run":
            if not ASSISTANT:
                raise ValueError("請先分析任務")
            if body.get("plan"):
                ASSISTANT.replace_plan(body["plan"])
            ASSISTANT.apply_overrides(body)
            ASSISTANT.start()
        elif name == "stop":
            if ASSISTANT:
                ASSISTANT.stop()
        else:
            raise ValueError("未知的指令")
    return assistant_state()


def assistant_state():
    return ASSISTANT.snapshot() if ASSISTANT else {"phase": "idle", "log": [], "records": [], "steps": []}


def close_compare():
    global COMPARE
    if COMPARE:
        COMPARE.close()
        COMPARE = None


def compare_command(name, body):
    global COMPARE
    with COMPARE_LOCK:
        if name == "start":
            if COMPARE and COMPARE.is_running():
                raise ValueError("上一輪比較還在執行，請先按「停止」")
            if ASSISTANT and ASSISTANT.is_running():
                raise ValueError("網頁搜尋助手正在執行任務，請等它結束再比較")
            task_zh = str(body.get("task_zh", "")).strip()
            if not task_zh or len(task_zh) > 1000:
                raise ValueError("請輸入 1 至 1,000 字的任務")
            site = body.get("site") if body.get("site") in compare.SEARCH_SITES else "momo"
            modes = [m for m in compare.MODES if m in (body.get("modes") or [])]
            if not modes:
                raise ValueError("請至少勾選一種模式")
            try:
                max_items = max(1, min(int(body.get("max_items") or 5), 10))
            except (TypeError, ValueError):
                max_items = 5
            close_compare()
            COMPARE = compare.Session(task_zh, site, modes, max_items, ledger=LEDGER)
            COMPARE.start()
        elif name == "stop":
            if COMPARE:
                COMPARE.stop()
        else:
            raise ValueError("未知的指令")
    return compare_state()


def compare_state():
    return COMPARE.snapshot() if COMPARE else {"phase": "idle", "runs": []}


def lab_ask(body):
    """Question-type lab: forward one state and its typed questions to Jev unchanged."""
    state = str(body.get("state", "")).strip()
    questions = body.get("questions")
    if not state or len(state) > 4000:
        raise ValueError("請輸入 1 至 4,000 字的文字")
    if not isinstance(questions, dict) or not 0 < len(questions) <= 20:
        raise ValueError("問題要是一個 JSON 物件，1 至 20 題")
    for qid, question in questions.items():
        if not isinstance(question, dict) or question.get("type") not in LAB_TYPES or not question.get("instructions"):
            raise ValueError(f"問題「{qid}」需要 type（choice、noul 或 score）和 instructions")
    if not os.environ.get("TYPESAFE_API_KEY"):
        raise ValueError("缺少 TYPESAFE_API_KEY，請在 .env 設定")
    body = {"state": state, "model": os.environ.get("TYPESAFE_MODEL", "jev-latest"), "questions": questions}
    started = time.perf_counter()
    result = model.post_json("https://api.typesafe.ai/v1/systemone", os.environ["TYPESAFE_API_KEY"], body)
    usage = result.get("usage") or {}
    return {
        "model": result.get("model"),
        "answers": result.get("answers") or {},
        "usage": usage,
        "elapsed_ms": round((time.perf_counter() - started) * 1000),
        "cost_usd": usage.get("input_tokens", 0) * JEV_USD_PER_MTOK / 1_000_000,
    }


class Handler(BaseHTTPRequestHandler):
    def send(self, status, content, mime="application/json"):
        content = content if isinstance(content, bytes) else content.encode()
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        if self.headers.get("Host") != f"127.0.0.1:{PORT}":
            return self.send(403, "Forbidden", "text/plain")
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/state":
            with LOCK:
                return self.send(200, json.dumps(response_state()))
        if path == "/api/assistant/state":
            return self.send(200, json.dumps(assistant_state()))
        if path == "/api/compare/state":
            return self.send(200, json.dumps(compare_state()))
        if path == "/api/reports":
            return self.send(200, json.dumps({"dir": str(REPORTS_ROOT), "reports": archive.list_reports(REPORTS_ROOT)}))
        if path == "/api/report":
            name = parse_qs(parsed.query).get("name", [""])[0]
            try:
                return self.send(200, json.dumps({"name": name, "markdown": archive.read_report(REPORTS_ROOT, name)}))
            except ValueError as error:
                return self.send(404, json.dumps({"error": str(error)}))
        if path == "/api/sites":
            return self.send(
                200,
                json.dumps([{"key": k, "name": v["name"], "search": "search" in v} for k, v in SITE_INFO.items()]),
            )
        if path == "/api/usage":
            refresh = parse_qs(parsed.query).get("refresh", ["0"])[0] == "1"
            return self.send(200, json.dumps(usage_report(refresh)))
        if path == "/demo.mp4":
            video = ROOT.parent / "docs" / "demo.mp4"
            if video.exists():
                return self.send(200, video.read_bytes(), "video/mp4")
        if path not in FILES:
            return self.send(404, "Not found", "text/plain")
        name, mime = FILES[path]
        content = (ROOT / "static" / name).read_text().replace("__TOKEN__", TOKEN)
        self.send(200, content, mime + "; charset=utf-8")

    def read_body(self):
        length = int(self.headers.get("Content-Length", "0"))
        if not 0 < length < 65536:
            raise ValueError("請求內容大小不正確")
        return json.loads(self.rfile.read(length))

    def do_POST(self):
        if (
            self.headers.get("Host") != f"127.0.0.1:{PORT}"
            or self.headers.get("X-Demo-Token") != TOKEN
            or self.headers.get("Origin") not in (None, ORIGIN)
        ):
            return self.send(403, json.dumps({"error": "只接受本機請求；若你剛重啟伺服器，請重新整理這個分頁"}))
        name = self.path.removeprefix("/api/")
        if name.startswith("assistant/"):
            try:
                self.send(200, json.dumps(assistant_command(name.removeprefix("assistant/"), self.read_body())))
            except (ValueError, RuntimeError, TimeoutError) as error:
                self.send(400, json.dumps({"error": str(error)}))
            except Exception:
                self.send(500, json.dumps({"error": "本機伺服器發生錯誤，請重新分析任務"}))
            return
        if name.startswith("compare/"):
            try:
                self.send(200, json.dumps(compare_command(name.removeprefix("compare/"), self.read_body())))
            except (ValueError, RuntimeError, TimeoutError) as error:
                self.send(400, json.dumps({"error": str(error)}))
            except Exception:
                self.send(500, json.dumps({"error": "本機伺服器發生錯誤，請重新開始比較"}))
            return
        if name == "lab/ask":
            try:
                self.send(200, json.dumps(lab_ask(self.read_body())))
            except (ValueError, RuntimeError, TimeoutError) as error:
                self.send(400, json.dumps({"error": str(error)}))
            except Exception:
                self.send(500, json.dumps({"error": "本機伺服器發生錯誤，請再試一次"}))
            return
        if not LOCK.acquire(blocking=False):
            return self.send(409, json.dumps({"error": "上一個步驟還在執行中，請稍候再試"}))
        try:
            body = self.read_body()
            result = translate(body) if name == "translate" else command(name, body)
            self.send(200, json.dumps(result))
        except (ValueError, RuntimeError, TimeoutError) as error:
            self.send(400, json.dumps({"error": str(error)}))
        except Exception:
            self.send(500, json.dumps({"error": "本機伺服器發生錯誤，不會自動重試，請重新開始任務"}))
        finally:
            LOCK.release()

    def log_message(self, *_args):
        pass


def main():
    global LEDGER
    load_environment()
    LEDGER = Ledger()
    atexit.register(close_browser)
    atexit.register(close_assistant)
    atexit.register(close_compare)
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Jev 網頁搜尋助手: {ORIGIN}", flush=True)
    print(f"三種模式比價示範: {ORIGIN}/compare", flush=True)
    print(f"單步任務台: {ORIGIN}/console  ·  原始英文示範: {ORIGIN}/en", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
