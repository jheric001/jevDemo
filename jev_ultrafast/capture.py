"""Reading pages without Jev: candidate links, structured records, saved collections and the final report."""

import base64
import csv
import json
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .llm import chat_json

PAGE_READ_JS = r"""
(() => {
  const clip = (s, n) => String(s || "").replace(/\s+/g, " ").trim().slice(0, n);
  const links = [];
  const seen = new Set();
  for (const a of document.querySelectorAll("a[href]")) {
    const href = a.href;
    if (!href || !/^https?:/.test(href) || seen.has(href)) continue;
    const text = clip(a.innerText || a.getAttribute("aria-label") || a.title || a.querySelector("img")?.alt, 160);
    if (text.length < 2) continue;
    seen.add(href);
    const card = a.closest("li, article, [class*=goods], [class*=prod], [class*=item], [class*=card]");
    const context = card && card !== a ? clip(card.innerText, 220) : "";
    links.push({ text, href, context });
    if (links.length >= 400) break;
  }
  const jsonld = [...document.querySelectorAll('script[type="application/ld+json"]')]
    .map((s) => clip(s.textContent, 4000)).slice(0, 3);
  const meta = {};
  for (const m of document.querySelectorAll('meta[property^="og:"], meta[property^="product:"], meta[name="description"]')) {
    meta[m.getAttribute("property") || m.getAttribute("name")] = clip(m.content, 300);
  }
  return { url: location.href, title: document.title, text: clip(document.body?.innerText, 20000), links, jsonld, meta };
})()
"""

FILTER_RULES = """You receive the acceptance criteria (Chinese) for a research task and a list of links found on one web page.
Select the links that point to INDIVIDUAL item pages (a single product, post or article) that satisfy the criteria,
judging from the link text and the surrounding card text. Exclude navigation, categories, banners, ads, filters,
"more" links and duplicates of the same item (keep the first). Be strict about brand, model and size words in the
criteria: if the text shows a different brand or size, exclude it; if the text does not mention size at all and the
criteria require one, exclude it unless the criteria say otherwise. Return only JSON:
{"selected": [{"i": <index>, "title": "<clean item title>", "price_twd": <number or null>, "reason_zh": "<短說明>"}]}
ordered as they appear, at most the requested maximum."""

EXTRACT_RULES = """You receive the text of one web page plus the acceptance criteria of a research task. Extract the requested fields as
a JSON object. Use null when a field is unknown; never invent values. price_twd must be a number (no currency symbol):
the price the buyer actually pays for the listed purchase. pack_count is the TOTAL number of pieces/units in that
purchase (for example 2 packs x 32 pieces = 64), not the number of packs. unit_price_note must state the arithmetic,
for example "850 元 ÷ 64 片 = 13.28 元/片", or null when the piece count is unknown. matches_criteria is true only if the
page itself satisfies the criteria; reason_zh explains why in one short Chinese sentence; summary_zh is a Chinese summary
of at most 60 characters. Return only the JSON object with exactly the requested field names."""

REPORT_RULES = """You are writing the final report of a web research task for a Traditional Chinese reader. You receive the original task,
the acceptance criteria and the captured records (JSON, each with a "site" field). Write Markdown in Traditional
Chinese: a short summary (which sites and keywords were covered, how many records matched), then a comparison table of
the records that match the criteria (site, name, brand/size or author/date as relevant, price or key numbers, unit
price when computable, seller/source, link), then a list of records that did not match and why, then 2 to 4 bullet
conclusions (for products: cheapest overall, best unit price, which site wins, caveats). Never invent data that is not in
the records. Return only JSON: {"report_md": "<markdown>"}"""


def item_key(url):
    """Identity of an item page: host and path, plus the id-like query parameters some shops rely on."""
    parsed = urlparse(url)
    ids = sorted(
        (k, v[0])
        for k, v in parse_qs(parsed.query).items()
        if k.lower() in {"i_code", "id", "pid", "prod", "product", "goods", "itemid", "item_id", "sku", "code"}
    )
    return (parsed.netloc.lower(), parsed.path.rstrip("/"), tuple(ids))


def read_page(browser, *, scroll=False):
    if scroll:
        for expression in ("window.scrollTo(0, document.body.scrollHeight)", "window.scrollTo(0, 0)"):
            try:
                browser.evaluate(expression)
            except Exception:
                pass
            time.sleep(0.8)
    page = browser.evaluate(PAGE_READ_JS)
    if not isinstance(page, dict):
        raise ValueError("讀不到頁面內容，頁面可能還在載入或被封鎖")
    return page


def expand_results(browser, *, max_rounds=8, settle=0.9):
    """Scroll to the bottom until the page stops adding links (lazy-loaded result lists). Returns the link count."""
    last = -1
    for _ in range(max_rounds):
        try:
            browser.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        except Exception:
            pass
        time.sleep(settle)
        try:
            count = int(browser.evaluate("document.querySelectorAll('a[href]').length") or 0)
        except Exception:
            count = last
        if count <= last:
            break
        last = count
    try:
        browser.evaluate("window.scrollTo(0, 0)")
    except Exception:
        pass
    return max(last, 0)


LEARN_RULES = """You receive the acceptance criteria of a search task, the keywords already searched, and the titles of items found so
far. Propose up to N ADDITIONAL search keywords that would find more items matching the criteria: the canonical brand,
series or model name exactly as the listings write it (for example the user typed 水凝尿布 but listings say
Parasol Clear+Dry), possibly combined with the required size or spec. Do not repeat keywords already used, do not
propose broader generic terms, and do not invent names that do not appear in the titles. Return only JSON:
{"keywords": ["...", "..."]}"""


def learn_keywords(criteria_zh, used_keywords, candidates, limit=2):
    titles = [c.get("title", "") for c in candidates if c.get("title")][:40]
    if not titles:
        return [], None
    request = {"criteria_zh": criteria_zh, "used_keywords": used_keywords, "titles": titles, "limit": limit}
    data, meta = chat_json("judge", LEARN_RULES, request, max_tokens=400)
    out = []
    for raw in data.get("keywords") or []:
        word = str(raw).strip()
        if word and word not in used_keywords and word not in out and len(word) <= 60:
            out.append(word)
    return out[:limit], meta


def filter_candidates(criteria_zh, page, max_items):
    links = [
        {"i": i, "text": link["text"], "context": link.get("context", "")[:220], "href": link["href"][:160]}
        for i, link in enumerate(page.get("links") or [])
    ]
    if not links:
        return [], None
    request = {
        "criteria_zh": criteria_zh,
        "max_items": max_items,
        "page_title": page.get("title"),
        "links": links[:300],
    }
    data, meta = chat_json("judge", FILTER_RULES, request, max_tokens=4000)
    selected, seen = [], set()
    for item in data.get("selected") or []:
        try:
            link = page["links"][int(item["i"])]
        except (KeyError, ValueError, TypeError, IndexError):
            continue
        # The same item often appears with different tracking parameters; dedupe on the path only.
        key = item_key(link["href"])
        if key in seen:
            continue
        seen.add(key)
        price = item.get("price_twd")
        selected.append(
            {
                "title": str(item.get("title") or link["text"])[:160],
                "url": link["href"],
                "price_twd": float(price) if isinstance(price, (int, float)) else None,
                "reason_zh": str(item.get("reason_zh") or "")[:120],
            }
        )
        if len(selected) >= max_items:
            break
    return selected, meta


def extract_record(collection, criteria_zh, page, note_zh=""):
    request = {
        "fields": collection["fields"],
        "collection_type": collection["type"],
        "criteria_zh": criteria_zh,
        "focus_zh": note_zh or "",
        "page": {
            "url": page.get("url"),
            "title": page.get("title"),
            "meta": page.get("meta") or {},
            "jsonld": page.get("jsonld") or [],
            "text": (page.get("text") or "")[:12000],
        },
    }
    data, meta = chat_json("judge", EXTRACT_RULES, request, max_tokens=2000)
    record = {field: data.get(field) for field in collection["fields"]}
    return record, meta


def write_report(task_zh, plan, records, instructions_zh=""):
    request = {
        "task_zh": task_zh,
        "criteria_zh": plan.get("criteria_zh"),
        "instructions_zh": instructions_zh or "",
        "records": [{k: v for k, v in r.items() if k not in {"screenshot"}} for r in records],
    }
    data, meta = chat_json("judge", REPORT_RULES, request, max_tokens=4000)
    report = str(data.get("report_md") or "").strip()
    if not report:
        raise ValueError("報告模型沒有產出內容")
    return report, meta


class Collection:
    """One folder per task: records.json, records.csv, report.md and a JPEG per captured page."""

    def __init__(self, root, task_id):
        self.dir = Path(root) / task_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.records = []

    def add(self, record, screenshot_b64=None):
        entry = {"id": len(self.records) + 1, "captured_at": time.strftime("%Y-%m-%d %H:%M:%S"), **record}
        if screenshot_b64:
            name = f"{entry['id']:02d}.jpg"
            (self.dir / name).write_bytes(base64.b64decode(screenshot_b64))
            entry["screenshot"] = name
        self.records.append(entry)
        self.save()
        return entry

    def save(self):
        (self.dir / "records.json").write_text(json.dumps(self.records, ensure_ascii=False, indent=1), encoding="utf-8")
        if not self.records:
            return
        fields = []
        for record in self.records:
            for key in record:
                if key not in fields:
                    fields.append(key)
        with (self.dir / "records.csv").open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for record in self.records:
                writer.writerow(
                    {
                        k: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v
                        for k, v in record.items()
                    }
                )

    def save_report(self, markdown):
        (self.dir / "report.md").write_text(markdown, encoding="utf-8")
