"""Turn a Chinese task into one English instruction for the agent. Keywords stay in their original script."""

import json
import os
import time

from .model import post_json

TRANSLATE_RULES = """You convert a task written in Chinese (Traditional or Simplified) into ONE clear English
instruction for a browser-automation agent that operates real websites.
Rules:
1. Keep every search keyword, product name, brand, model number, website name, place name, person name and quoted
   phrase in its ORIGINAL script; never translate them. Wrap each such term in double quotes so the agent types it
   exactly as written.
2. Keep dates, numbers, currencies and quantities exact. Write dates as "Month D, YYYY".
3. Do not add requirements the user did not state, and do not drop any.
4. Use the imperative mood, present tense, at most four sentences.
5. If the task has no explicit stopping point, append one sentence that says when the task is visibly complete on
   screen, for example: Stop when the search results are visible.
6. If the user did not ask to buy, book, log in or submit personal data, append: Do not add to cart, purchase or book.
Return only a JSON object: {"goal_en": "<the English instruction>"}."""


def translate_goal(goal_zh):
    key = os.environ.get("TEXT_MODEL_API_KEY")
    if not key:
        raise ValueError("翻譯需要 TEXT_MODEL_API_KEY，請先在 .env 設定")
    base = os.environ.get("TEXT_MODEL_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
    model = os.environ.get("TRANSLATE_MODEL") or os.environ.get("TEXT_MODEL", "google/gemini-2.5-flash-lite")
    reasoning = {"thinking": {"type": "disabled"}} if "api.deepseek.com/" in base else {"reasoning": {"effort": "low"}}
    if os.environ.get("TEXT_MODEL_REASONING") == "none":
        reasoning = {"reasoning": {"enabled": False}}
    started = time.perf_counter()
    result = post_json(
        base + "/chat/completions",
        key,
        {
            "model": model,
            "max_tokens": 1024,
            "response_format": {"type": "json_object"},
            **reasoning,
            "messages": [
                {"role": "system", "content": TRANSLATE_RULES},
                {"role": "user", "content": json.dumps({"task_zh": goal_zh}, ensure_ascii=False)},
            ],
        },
    )
    try:
        output = json.loads(result["choices"][0]["message"]["content"])
        goal_en = output["goal_en"]
        if set(output) != {"goal_en"} or not isinstance(goal_en, str) or not goal_en.strip() or len(goal_en) > 2000:
            raise ValueError()
    except (ValueError, KeyError, TypeError, IndexError):
        detail = (result.get("error") or {}).get("message") if isinstance(result, dict) else None
        raise ValueError("翻譯模型沒有回傳有效結果" + (f"：{detail}" if detail else "")) from None
    return {
        "goal_en": goal_en.strip(),
        "model": model,
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "usage": result.get("usage") or {},
    }
