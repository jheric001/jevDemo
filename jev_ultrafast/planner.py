"""Planning layer: a strong model reads the Chinese task and splits it into typed steps for the runner."""

import datetime
import re

from .llm import chat_json
from .sites import SITES

STEP_TYPES = {"browse", "navigate", "search_collect", "collect_candidates", "foreach_candidate", "capture", "report"}
INTENTS = {"product_search", "sentiment", "patrol", "general"}
COLLECTIONS = {"product", "post", "article", "generic"}
DEFAULT_FIELDS = {
    "product": [
        "name",
        "brand",
        "size",
        "price_twd",
        "pack_count",
        "unit_price_note",
        "seller",
        "promotion",
        "specs",
        "availability",
        "matches_criteria",
        "reason_zh",
        "summary_zh",
    ],
    "post": [
        "author",
        "posted_at",
        "content",
        "likes",
        "comments",
        "shares",
        "sentiment",
        "matches_criteria",
        "reason_zh",
        "summary_zh",
    ],
    "article": ["source", "date", "headline", "key_points", "sentiment", "matches_criteria", "reason_zh", "summary_zh"],
    "generic": ["headline", "key_points", "matches_criteria", "reason_zh", "summary_zh"],
}

PLANNER_RULES = """You are the planning layer of a web research assistant. The user writes a task in Traditional Chinese.
A separate browser agent called Jev can operate a real website step by step, but it only understands a short English
goal with a visible stopping condition. Jev cannot judge brands, sizes, relevance or meaning, and it does not know what
"收錄" (capture/save this page) means. Your job is to understand the task, decide what to collect, and split the work
into steps using ONLY these step types:

- "search_collect": the program searches EVERY listed site for EVERY keyword (using the site's own search URL, or Jev
  typing into the search box when the site has none or search_mode is "jev"), scrolls until all results are loaded,
  follows pagination up to max_pages, and a judge model keeps the item pages that match the criteria. Fields:
  criteria_zh (precise rule, e.g. 商品名稱必須包含「水凝」且尺寸為 M，排除其他品牌與尺寸), max_items (integer),
  learn_keywords (true lets the judge propose up to 2 extra keywords from the first round's titles, such as the
  canonical brand or series name the shop actually uses, and search them once more).
- "browse": Jev performs an interactive sequence on the current tab. Fields: goal_en (English, imperative, keep
  keywords, brands, product and site names in their ORIGINAL script inside double quotes, end with a visible stop
  condition, never buy, book or log in), goal_zh (short Chinese summary), max_actions (integer, at most 30; a form
  with several fields and date pickers needs 20 to 30). Every quoted value in goal_en is typed literally into a
  field, so quote exactly what the field expects: for travel forms quote the CITY or airport ("Taipei" or "TPE",
  "Honolulu" or "HNL"), never the country, and write dates as YYYY-MM-DD. State one-way or round trip explicitly.
- "navigate": open an absolute URL directly on the current tab. Fields: url. The chosen start page is already open
  when the plan begins, so never add a navigate step to it.
- "collect_candidates": like search_collect but only reads the page that is currently open (no searching).
  Fields: criteria_zh, max_items.
- "foreach_candidate": for each kept candidate the program opens its URL, then runs the child steps. Fields: steps
  (normally a single "capture").
- "capture": save the current page as one record: URL, title and structured fields extracted by the judge model.
  Fields: note_zh.
- "report": write the final Chinese report from all records. Fields: instructions_zh.

Also decide at the top level:
- intent: product_search | sentiment | patrol | general.
- collection: {"type": product | post | article | generic, "fields": [...]}. Always include matches_criteria, reason_zh
  and summary_zh. For products include name, brand, size, price_twd, pack_count, unit_price_note, seller, promotion,
  specs, availability.
- criteria_zh: the acceptance rule for a record, precise enough for another model to apply.
- keywords: 3 to 5 search strings. The first is the user's own wording; the others are variants written the way real
  listings name things (brand + series, series + size, alternative size notation such as 3號 for M, English brand
  name). Do not invent brands you are not sure about; variants of the user's words are fine.
- sites: the site keys to cover, chosen from the provided list (cover all of them unless the task clearly limits it).
- max_pages: 1 to 5 result pages per keyword per site (2 or 3 for shopping comparisons).
- summary_zh: one Chinese sentence describing the plan.

Rules: 3 to 5 top-level steps. For any task that starts with a search (products, news, posts) use
search_collect → foreach_candidate(capture) → report. Use browse only for interactions a search URL cannot express.
When the answer lives on ONE results page that the user only needs summarised (flight or hotel search results, a
price table, a ranking), do NOT use collect_candidates; use browse (Jev fills the form and searches) → capture
(the results page itself) → report, and define collection fields that describe one result set, e.g. for flights:
route, dates, options (list of airline, depart_time, arrive_time, duration, stops, price_twd), cheapest_price_twd,
shortest_duration, matches_criteria, reason_zh, summary_zh.
Never plan purchases, logins, account changes or submitting personal data. max_items must not exceed the given limit.
Return only a JSON object:
{"intent": "...", "summary_zh": "...", "criteria_zh": "...", "keywords": [...], "sites": [...], "max_pages": 2,
 "collection": {"type": "...", "fields": [...]}, "steps": [...]}"""


def quoted_terms(text):
    return [t.strip() for t in re.findall(r"[「\"“]([^」\"”]{1,40})[」\"”]", text or "") if t.strip()]


def validate_plan(plan, max_items, *, task_zh="", allowed_sites=None, default_sites=None, search_mode="url"):
    if not isinstance(plan, dict):
        raise ValueError("規劃結果不是物件")
    intent = plan.get("intent") if plan.get("intent") in INTENTS else "general"
    collection = plan.get("collection") if isinstance(plan.get("collection"), dict) else {}
    ctype = collection.get("type") if collection.get("type") in COLLECTIONS else "generic"
    fields = [str(f) for f in (collection.get("fields") or []) if isinstance(f, str)] or DEFAULT_FIELDS[ctype]
    for required in ("matches_criteria", "reason_zh", "summary_zh"):
        if required not in fields:
            fields.append(required)
    steps = normalise_steps(plan.get("steps"), max_items, nested=False)
    if not steps:
        raise ValueError("規劃結果沒有任何步驟")
    keywords = []
    for raw in plan.get("keywords") or []:
        word = str(raw).strip()
        if word and word not in keywords:
            keywords.append(word)
    if not keywords:
        keywords = quoted_terms(task_zh) or ([task_zh.strip()[:30]] if task_zh.strip() else [])
    if any(s["type"] == "search_collect" for s in steps) and not keywords:
        raise ValueError("搜尋任務需要至少一組關鍵字")
    allowed = set(allowed_sites or SITES) | {"url"}
    sites = [s for s in (plan.get("sites") or []) if isinstance(s, str) and s in allowed]
    if not sites:
        sites = list(default_sites or [])
    try:
        max_pages = max(1, min(int(plan.get("max_pages") or 2), 5))
    except (TypeError, ValueError):
        max_pages = 2
    return {
        "intent": intent,
        "summary_zh": str(plan.get("summary_zh") or "").strip(),
        "criteria_zh": str(plan.get("criteria_zh") or "").strip(),
        "keywords": keywords[:6],
        "sites": sites,
        "max_pages": max_pages,
        "search_mode": "jev" if search_mode == "jev" else "url",
        "collection": {"type": ctype, "fields": fields},
        "steps": steps,
    }


def normalise_steps(steps, max_items, *, nested):
    if not isinstance(steps, list):
        raise ValueError("步驟必須是清單")
    out = []
    for raw in steps:
        if not isinstance(raw, dict) or raw.get("type") not in STEP_TYPES:
            raise ValueError(f"不支援的步驟：{raw!r}"[:200])
        kind = raw["type"]
        step = {"type": kind}
        if kind == "browse":
            goal = str(raw.get("goal_en") or "").strip()
            if not goal:
                raise ValueError("browse 步驟缺少 goal_en")
            step.update(
                goal_en=goal,
                goal_zh=str(raw.get("goal_zh") or "").strip(),
                max_actions=max(1, min(int(raw.get("max_actions") or 20), 30)),
            )
        elif kind == "navigate":
            url = str(raw.get("url") or "").strip()
            if not url.startswith(("http://", "https://")):
                raise ValueError("navigate 步驟的網址必須是 http 或 https")
            if "google.com/travel/flights" in url and "hl=" not in url:
                url += ("&" if "?" in url else "?") + "hl=en"  # Jev's goals are English; keep the form labels English
            step["url"] = url
        elif kind in {"search_collect", "collect_candidates"}:
            if nested:
                raise ValueError(f"{kind} 不能放在逐一處理裡")
            step.update(
                criteria_zh=str(raw.get("criteria_zh") or "").strip(),
                max_items=max(1, min(int(raw.get("max_items") or max_items), max_items)),
            )
            if kind == "search_collect":
                step["learn_keywords"] = bool(raw.get("learn_keywords", True))
        elif kind == "foreach_candidate":
            if nested:
                raise ValueError("foreach_candidate 不能巢狀")
            children = normalise_steps(raw.get("steps") or [{"type": "capture"}], max_items, nested=True)
            step["steps"] = [c for c in children if c["type"] not in {"navigate", "foreach_candidate", "report"}] or [
                {"type": "capture", "note_zh": ""}
            ]
        elif kind == "capture":
            step["note_zh"] = str(raw.get("note_zh") or "").strip()
        elif kind == "report":
            step["instructions_zh"] = str(raw.get("instructions_zh") or "").strip()
        out.append(step)
    return out


def make_plan(task_zh, sites, custom_url, max_items, search_mode="url"):
    site_list = [
        {"key": s, "name": SITES[s]["name"], "has_search_url": "search" in SITES[s]} for s in sites if s in SITES
    ]
    if custom_url:
        site_list.append({"key": "url", "name": custom_url, "has_search_url": False})
    request = {
        "task_zh": task_zh,
        "sites": site_list,
        "search_mode": search_mode,
        "max_items_limit": max_items,
        "today": datetime.date.today().isoformat(),
    }
    data, meta = chat_json("planner", PLANNER_RULES, request, max_tokens=3000)
    plan = validate_plan(
        data,
        max_items,
        task_zh=task_zh,
        allowed_sites=[s["key"] for s in site_list],
        default_sites=[s["key"] for s in site_list],
        search_mode=search_mode,
    )
    return plan, meta


def describe_step(step, plan=None):
    """Chinese one-liner for the UI and the log."""
    kind = step["type"]
    if kind == "search_collect":
        keywords = "、".join(f"「{k}」" for k in (plan or {}).get("keywords") or []) or "規劃的關鍵字"
        sites = (
            "、".join(SITES.get(s, {}).get("name", "自訂網址") for s in (plan or {}).get("sites") or []) or "選定網站"
        )
        learn = "，並從結果學習新關鍵字再搜一輪" if step.get("learn_keywords") else ""
        return f"在 {sites} 搜尋 {keywords}，每組最多 {(plan or {}).get('max_pages', 2)} 頁，挑出符合條件的項目（最多 {step['max_items']} 個）{learn}"
    if kind == "browse":
        return f"Jev 操作：{step.get('goal_zh') or step['goal_en']}"
    if kind == "navigate":
        return f"直接開啟網址：{step['url']}"
    if kind == "collect_candidates":
        return f"從目前頁面的連結挑出符合條件的項目（最多 {step['max_items']} 個）"
    if kind == "foreach_candidate":
        inner = "、".join(describe_step(s, plan) for s in step["steps"])
        return f"逐一開啟每個候選項目，然後：{inner}"
    if kind == "capture":
        return "收錄本頁" + (f"（{step['note_zh']}）" if step.get("note_zh") else "")
    if kind == "report":
        return "整理報告並存成 Markdown" + (f"：{step['instructions_zh']}" if step.get("instructions_zh") else "")
    return kind
