"""Local usage ledger and live balance lookups.

TypeSafe exposes no balance endpoint, so Jev spend is estimated locally from the token counts in each
response. OpenRouter does expose account credits and per-key limits, which are fetched live and cached.
"""

import json
import os
import threading
import time
from pathlib import Path

import httpx

JEV_USD_PER_MTOK = float(os.environ.get("JEV_USD_PER_MTOK", "0.042"))
LEDGER_PATH = Path.cwd() / "artifacts" / "usage-ledger.json"
_LOCK = threading.Lock()
_BALANCE = {"at": 0.0, "data": None}
_EMPTY_MISC = {
    "translate_calls": 0,
    "translate_cost_usd": 0.0,
    "translate_input_tokens": 0,
    "translate_output_tokens": 0,
}


def jev_cost_usd(input_tokens):
    return input_tokens / 1_000_000 * JEV_USD_PER_MTOK


def task_usage(state):
    """Summarise one agent state into Jev and text-helper counters. Output tokens are free on Jev."""
    decisions = state.get("decisions") or []
    calls = state.get("text_calls") or []
    jev_in = sum(int((d.get("usage") or {}).get("input_tokens") or 0) for d in decisions)
    jev_out = sum(int((d.get("usage") or {}).get("output_tokens") or 0) for d in decisions)
    helper_in = sum(int((c.get("usage") or {}).get("prompt_tokens") or 0) for c in calls)
    helper_out = sum(int((c.get("usage") or {}).get("completion_tokens") or 0) for c in calls)
    helper_cost = sum(float((c.get("usage") or {}).get("cost") or 0) for c in calls)
    return {
        "jev_requests": len(decisions),
        "jev_input_tokens": jev_in,
        "jev_output_tokens": jev_out,
        "jev_cost_usd": jev_cost_usd(jev_in),
        "helper_calls": len(calls),
        "helper_input_tokens": helper_in,
        "helper_output_tokens": helper_out,
        "helper_cost_usd": helper_cost,
        "actions": len(state.get("history") or []),
        "elapsed_ms": state.get("elapsed_ms", 0),
        "status": state.get("status"),
    }


class Ledger:
    """Append-only JSON ledger: one record per task (upserted) plus a bucket for translation calls."""

    def __init__(self, path=LEDGER_PATH):
        self.path = Path(path)
        self.data = {"tasks": {}, "misc": dict(_EMPTY_MISC)}
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text())
                self.data["tasks"] = dict(loaded.get("tasks") or {})
                self.data["misc"] = {**_EMPTY_MISC, **(loaded.get("misc") or {})}
            except (ValueError, AttributeError):
                pass

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=1))

    def record_task(self, task_id, meta, usage):
        with _LOCK:
            self.data["tasks"][task_id] = {**meta, **usage, "updated_at": time.time()}
            self.save()

    def record_translate(self, usage):
        usage = usage or {}
        with _LOCK:
            misc = self.data["misc"]
            misc["translate_calls"] += 1
            misc["translate_cost_usd"] += float(usage.get("cost") or 0)
            misc["translate_input_tokens"] += int(usage.get("prompt_tokens") or 0)
            misc["translate_output_tokens"] += int(usage.get("completion_tokens") or 0)
            self.save()

    def totals(self):
        tasks = list(self.data["tasks"].values())
        misc = self.data["misc"]

        def total(key):
            return sum(float(t.get(key) or 0) for t in tasks)

        helper_cost = total("helper_cost_usd")
        llm_cost = total("llm_cost_usd")
        return {
            "tasks": len(tasks),
            "jev_requests": int(total("jev_requests")),
            "jev_input_tokens": int(total("jev_input_tokens")),
            "jev_output_tokens": int(total("jev_output_tokens")),
            "jev_cost_usd": jev_cost_usd(total("jev_input_tokens")),
            "helper_calls": int(total("helper_calls")),
            "helper_cost_usd": helper_cost,
            "llm_calls": int(total("llm_calls")),
            "llm_cost_usd": llm_cost,
            "planner_cost_usd": total("planner_cost_usd"),
            "judge_cost_usd": total("judge_cost_usd"),
            "translate_calls": misc["translate_calls"],
            "translate_cost_usd": misc["translate_cost_usd"],
            "openrouter_cost_usd": helper_cost + llm_cost + misc["translate_cost_usd"],
        }

    def recent(self, limit=8):
        rows = sorted(self.data["tasks"].items(), key=lambda kv: kv[1].get("updated_at", 0), reverse=True)
        keys = (
            "goal",
            "goal_zh",
            "scenario",
            "kind",
            "started_at_epoch",
            "status",
            "actions",
            "records",
            "elapsed_ms",
            "jev_requests",
            "jev_input_tokens",
            "jev_cost_usd",
            "helper_calls",
            "helper_cost_usd",
            "llm_cost_usd",
            "planner_cost_usd",
            "judge_cost_usd",
        )
        return [{"task_id": task_id, **{k: row.get(k) for k in keys}} for task_id, row in rows[:limit]]


def openrouter_balance(key, refresh=False, ttl=15):
    """Account credits plus this key's limit. Cached briefly so the console can poll freely."""
    now = time.time()
    if not refresh and _BALANCE["data"] and now - _BALANCE["at"] < ttl:
        return _BALANCE["data"]
    if not key:
        return {"error": "尚未設定 TEXT_MODEL_API_KEY"}
    if "openrouter.ai" not in os.environ.get("TEXT_MODEL_BASE_URL", "https://openrouter.ai/api/v1"):
        return {"error": "文字模型端點不是 OpenRouter，無法查詢餘額"}
    headers = {"Authorization": f"Bearer {key}"}
    out = {"fetched_at": now}
    try:
        with httpx.Client(timeout=10) as client:
            credits = client.get("https://openrouter.ai/api/v1/credits", headers=headers).json().get("data") or {}
            info = client.get("https://openrouter.ai/api/v1/auth/key", headers=headers).json().get("data") or {}
        total, used = credits.get("total_credits"), credits.get("total_usage")
        out.update(
            account_total_usd=total,
            account_used_usd=used,
            account_remaining_usd=(total - used) if total is not None and used is not None else None,
            key_limit_usd=info.get("limit"),
            key_used_usd=info.get("usage"),
            key_remaining_usd=info.get("limit_remaining"),
            key_usage_daily_usd=info.get("usage_daily"),
            key_usage_monthly_usd=info.get("usage_monthly"),
        )
    except (httpx.HTTPError, ValueError, AttributeError) as error:
        out["error"] = f"OpenRouter 查詢失敗：{type(error).__name__}"
    _BALANCE.update(at=now, data=out)
    return out
