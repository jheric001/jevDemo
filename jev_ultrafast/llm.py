"""JSON calls to the assistant's planner and judge models through the OpenAI-compatible endpoint."""

import json
import os
import re
import time

import httpx

DEFAULTS = {
    "planner": "anthropic/claude-sonnet-5",
    "judge": "google/gemini-2.5-flash",
    "actor": "google/gemini-2.5-flash",  # decides browser actions in the pure-LLM comparison arm
}
ENV_NAMES = {"planner": "PLANNER_MODEL", "judge": "JUDGE_MODEL", "actor": "ACTOR_MODEL"}
LABELS = {"planner": "規劃模型", "judge": "判斷模型", "actor": "操作模型"}
# Planning with a strong model regularly takes 15–30 s; the shared 25 s client used for Jev's text helper is too short.
TIMEOUTS = {"planner": 150, "judge": 90, "actor": 60}
RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 529}
CLIENT = httpx.Client(http2=True, timeout=httpx.Timeout(90, connect=15))


def model_for(kind):
    return os.environ.get(ENV_NAMES[kind]) or DEFAULTS[kind]


def post_json(url, key, body, *, kind="judge"):
    """POST with a long read timeout, retries on transient failures, and Chinese error messages."""
    label = LABELS.get(kind, "模型")
    timeout = httpx.Timeout(TIMEOUTS.get(kind, 90), connect=15)
    last_error = None
    for attempt in range(3):
        try:
            response = CLIENT.post(url, json=body, headers={"Authorization": f"Bearer {key}"}, timeout=timeout)
        except httpx.TimeoutException:
            limit = TIMEOUTS.get(kind, 90)
            raise ValueError(f"{label}回應逾時（超過 {limit} 秒），請再試一次，或在 .env 換較快的模型") from None
        except httpx.HTTPError as error:
            last_error = f"{label}連線失敗（{type(error).__name__}）"
            time.sleep(1.0 * (attempt + 1))
            continue
        if response.status_code in RETRY_STATUS and attempt < 2:
            last_error = f"{label}服務暫時無法使用（HTTP {response.status_code}）"
            time.sleep(1.5 * 2**attempt)
            continue
        if response.is_error:
            detail = ""
            try:
                detail = (response.json().get("error") or {}).get("message") or ""
            except ValueError:
                pass
            suffix = f"：{detail[:200]}" if detail else ""
            raise ValueError(f"{label}服務回應錯誤（HTTP {response.status_code}）{suffix}")
        try:
            return response.json()
        except ValueError:
            raise ValueError(f"{label}回傳的內容不是 JSON") from None
    raise ValueError((last_error or f"{label}無法使用") + "，已重試 3 次，請檢查網路後再試")


def strip_fences(content):
    content = str(content or "").strip()
    match = re.match(r"^```(?:json)?\s*(.*?)\s*```$", content, flags=re.S)
    return match.group(1) if match else content


def chat_json(kind, system, user, *, max_tokens=4000):
    """Ask one model for a JSON object. Returns (data, meta) where meta carries model, latency and cost."""
    key = os.environ.get("TEXT_MODEL_API_KEY")
    if not key:
        raise ValueError("需要 TEXT_MODEL_API_KEY（OpenRouter 金鑰），請先在 .env 設定")
    base = os.environ.get("TEXT_MODEL_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
    model = model_for(kind)
    content = user if isinstance(user, str) else json.dumps(user, ensure_ascii=False)
    body = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}],
    }
    if kind in {"judge", "actor"}:
        body["reasoning"] = {"enabled": False}
    started = time.perf_counter()
    try:
        result = post_json(
            base + "/chat/completions", key, {**body, "response_format": {"type": "json_object"}}, kind=kind
        )
    except ValueError as error:
        if "HTTP 400" not in str(error):
            raise
        result = post_json(base + "/chat/completions", key, body, kind=kind)  # provider without JSON mode
    try:
        data = json.loads(strip_fences(result["choices"][0]["message"]["content"]))
        if not isinstance(data, dict):
            raise ValueError()
    except (KeyError, IndexError, TypeError, ValueError):
        detail = (result.get("error") or {}).get("message") if isinstance(result, dict) else None
        label = {"planner": "規劃", "actor": "操作"}.get(kind, "判斷")
        raise ValueError(f"{label}模型沒有回傳有效的 JSON" + (f"：{detail}" if detail else "")) from None
    return data, usage_meta(kind, model, started, result)


FENCE = re.compile(r"^```[A-Za-z]*\s*\n?(.*?)\s*```$", re.S)


def chat_text(kind, system, user, *, max_tokens=4000):
    """Ask one model for plain text such as a Markdown report. Nothing to parse, so nothing to break:
    a Markdown table wrapped inside a JSON string regularly came back as invalid JSON."""
    key = os.environ.get("TEXT_MODEL_API_KEY")
    if not key:
        raise ValueError("需要 TEXT_MODEL_API_KEY（OpenRouter 金鑰），請先在 .env 設定")
    base = os.environ.get("TEXT_MODEL_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
    model = model_for(kind)
    content = user if isinstance(user, str) else json.dumps(user, ensure_ascii=False)
    body = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}],
    }
    if kind in {"judge", "actor"}:
        body["reasoning"] = {"enabled": False}
    started = time.perf_counter()
    result = post_json(base + "/chat/completions", key, body, kind=kind)
    try:
        text = str(result["choices"][0]["message"]["content"] or "").strip()
    except (KeyError, IndexError, TypeError):
        text = ""
    match = FENCE.match(text)
    text = match.group(1).strip() if match else text
    if not text:
        detail = (result.get("error") or {}).get("message") if isinstance(result, dict) else None
        raise ValueError(f"{LABELS.get(kind, '模型')}沒有回傳內容" + (f"：{detail}" if detail else ""))
    return text, usage_meta(kind, model, started, result)


def usage_meta(kind, model, started, result):
    usage = result.get("usage") or {}
    return {
        "kind": kind,
        "model": model,
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "cost_usd": float(usage.get("cost") or 0),
        "input_tokens": int(usage.get("prompt_tokens") or 0),
        "output_tokens": int(usage.get("completion_tokens") or 0),
    }
