"""Known sites: start pages and search URL patterns. Sites without a pattern fall back to Jev typing the search."""

from urllib.parse import quote

SITES = {
    "momo": {
        "name": "momo 購物網",
        "home": "https://www.momoshop.com.tw/main/Main.jsp",
        "search": "https://www.momoshop.com.tw/search/{q}?_isFuzzy=0&searchType=1&curPage={page}",
    },
    "pchome": {
        "name": "PChome 24h",
        "home": "https://24h.pchome.com.tw/",
        "search": "https://24h.pchome.com.tw/search/?q={q}&page={page}",
    },
    "coupang": {
        "name": "酷澎",
        "home": "https://www.tw.coupang.com/",
        "search": "https://www.tw.coupang.com/search?q={q}&page={page}",
    },
    "google": {
        "name": "Google 搜尋",
        "home": "https://www.google.com/?hl=zh-TW",
        "search": "https://www.google.com/search?q={q}&hl=zh-TW&start={offset10}",
    },
    "news": {
        "name": "Google 新聞",
        "home": "https://news.google.com/?hl=zh-TW&gl=TW&ceid=TW:zh-Hant",
        "search": "https://news.google.com/search?q={q}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant",
        "pages": 1,
    },
    "threads": {
        "name": "Threads",
        "home": "https://www.threads.net/",
        "search": "https://www.threads.net/search?q={q}",
        "pages": 1,
    },
    "wikipedia": {
        "name": "維基百科",
        "home": "https://zh.wikipedia.org/",
        "search": "https://zh.wikipedia.org/w/index.php?search={q}&ns0=1",
        "pages": 1,
    },
    "flights": {"name": "Google 航班", "home": "https://www.google.com/travel/flights?hl=en"},
}


def site_name(key):
    return SITES.get(key, {}).get("name", key or "自訂網址")


def home_url(key, fallback=None):
    return SITES.get(key, {}).get("home") or fallback


def search_url(key, keyword, page=1):
    """Direct search URL for a known site, or None when the site needs Jev (or has no pagination for page > 1)."""
    template = SITES.get(key, {}).get("search")
    if not template:
        return None
    if page > 1 and "{page}" not in template and "{offset10}" not in template:
        return None
    return template.format(q=quote(keyword, safe=""), page=page, offset10=(page - 1) * 10)


def max_pages_for(key, requested):
    return max(1, min(int(requested or 1), SITES.get(key, {}).get("pages", 5)))
