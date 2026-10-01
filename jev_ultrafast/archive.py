"""Markdown archive of finished tasks: one file per task plus an index, so results stay searchable later."""

import re
import time
from pathlib import Path

INDEX_HEADER = (
    "# 收錄報告索引\n\n| 日期 | 關鍵字 | 網站 | 收錄 | 符合 | 費用（美元） | 檔案 |\n|---|---|---|---|---|---|---|\n"
)


def slugify(text, limit=40):
    text = re.sub(r"[\\/:*?\"<>|#%&{}$!'@+`=\s]+", "-", str(text or "")).strip("-")
    return text[:limit] or "task"


def record_label(record):
    return str(record.get("name") or record.get("headline") or record.get("author") or record.get("title") or "")[:60]


def write_report(root, state, usage, plan):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    keywords = plan.get("keywords") or []
    title = keywords[0] if keywords else state["task_zh"][:20]
    sites = state.get("site_names") or []
    records = state.get("records") or []
    matched = [r for r in records if r.get("matches_criteria") is True]
    cost = usage["jev_cost_usd"] + usage["helper_cost_usd"] + usage["llm_cost_usd"]
    stamp = time.strftime("%Y-%m-%d_%H%M")
    name = f"{stamp}_{slugify(title)}_{slugify('+'.join(sites), 30)}.md"
    lines = [
        "---",
        f"title: {title}",
        f"date: {time.strftime('%Y-%m-%d %H:%M')}",
        f"sites: {', '.join(sites)}",
        f"keywords: {', '.join(keywords)}",
        f"criteria: {plan.get('criteria_zh', '')}",
        f"records: {len(records)}",
        f"matched: {len(matched)}",
        f"cost_usd: {cost:.4f}",
        f"duration_s: {usage.get('elapsed_ms', 0) // 1000}",
        f"collection: {state.get('collection_dir', '')}",
        "---",
        "",
        f"# {title}",
        "",
        "## 任務",
        "",
        state["task_zh"],
        "",
        "## 搜尋過程",
        "",
    ]
    searches = state.get("searches") or []
    if searches:
        lines += ["| 網站 | 關鍵字 | 頁 | 連結數 | 新增候選 |", "|---|---|---|---|---|"]
        lines += [f"| {s['site']} | {s['keyword']} | {s['page']} | {s['links']} | {s['new']} |" for s in searches]
    else:
        lines.append("（這次沒有搜尋步驟）")
    lines += ["", "## 報告", "", state.get("report_md") or "（沒有產生報告）", "", "## 收錄清單", ""]
    lines += ["| # | 網站 | 名稱 | 價格 | 單價 | 符合 | 連結 |", "|---|---|---|---|---|---|---|"]
    for r in records:
        verdict = "是" if r.get("matches_criteria") is True else "否" if r.get("matches_criteria") is False else "？"
        price = r.get("price_twd") if r.get("price_twd") is not None else ""
        lines.append(
            f"| {r.get('id')} | {r.get('site', '')} | {record_label(r)} | {price} | {r.get('unit_price_note') or ''} "
            f"| {verdict} | [開啟]({r.get('url')}) |"
        )
    shots = [r for r in records if r.get("screenshot")]
    if shots:
        lines += ["", "## 截圖", ""]
        lines += [f"- 第 {r.get('id')} 筆：{state.get('collection_dir', '')}/{r['screenshot']}" for r in shots]
    path = root / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    update_index(root, name, title, sites, len(records), len(matched), cost)
    return path


def update_index(root, name, title, sites, count, matched, cost):
    index = Path(root) / "index.md"
    existing = index.read_text(encoding="utf-8") if index.exists() else ""
    body = existing[len(INDEX_HEADER) :] if existing.startswith(INDEX_HEADER) else ""
    stamp = time.strftime("%Y-%m-%d %H:%M")
    row = f"| {stamp} | {title} | {', '.join(sites)} | {count} | {matched} | {cost:.4f} | [{name}]({name}) |\n"
    index.write_text(INDEX_HEADER + row + body, encoding="utf-8")


def front_matter(text):
    meta = {}
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            for line in parts[1].splitlines():
                if ":" in line:
                    key, value = line.split(":", 1)
                    meta[key.strip()] = value.strip()
    return meta


def list_reports(root, limit=50):
    root = Path(root)
    if not root.exists():
        return []
    files = sorted(
        (p for p in root.glob("*.md") if p.name != "index.md"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    out = []
    for path in files[:limit]:
        meta = front_matter(path.read_text(encoding="utf-8"))
        out.append(
            {
                "name": path.name,
                **{k: meta.get(k) for k in ("title", "date", "sites", "records", "matched", "cost_usd")},
            }
        )
    return out


def read_report(root, name):
    root = Path(root).resolve()
    path = (root / name).resolve()
    if path.parent != root or path.suffix != ".md" or not path.exists():
        raise ValueError("找不到這份報告")
    return path.read_text(encoding="utf-8")
