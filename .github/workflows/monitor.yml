#!/usr/bin/env python3
"""
Horizon Robotics (9660.HK) Disclosure & News Monitor
======================================================

Monitors reliable, high-signal sources for material information about
Horizon Robotics (地平线机器人, HK:9660):

  HKEX        - hkexnews.hk official disclosure portal (filings, announcements)
  WSCN        - 华尔街见闻 wallstreetcn.com (real-time financial news)
  Caixin      - 财新网 caixin.com (deep investigative reporting)
  36Kr        - 36氪 36kr.com (tech ecosystem reporting)

EXPLICITLY EXCLUDED, with reasons:
  Bloomberg   - paywall + ToS + near-zero Horizon-specific coverage
  NYT         - paywall + zero Horizon-specific coverage
  WSJ/Reuters - paywall + minimal coverage; whatever they publish is
                typically picked up by WSCN or Caixin within hours
  Xueqiu/Eastmoney/Forums - retail-investor noise, not "reliable"

USAGE
  python3 horizon_monitor.py                # full run, write digest.md
  python3 horizon_monitor.py --since 365    # look back 365 days
  python3 horizon_monitor.py --reset        # forget seen state
  python3 horizon_monitor.py --json         # also emit digest.json
  python3 horizon_monitor.py --no-news      # only HKEX filings
  python3 horizon_monitor.py --test         # connectivity test, no writes

State (which items were already seen) is stored in
  ~/.horizon_monitor/seen.json
so the next run only flags new items.

DEPENDENCIES
  pip install requests beautifulsoup4 lxml
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sys
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, quote_plus

try:
    import requests
    from bs4 import BeautifulSoup  # type: ignore
except ImportError:
    sys.exit(
        "Missing dependencies. Run:\n"
        "    pip install requests beautifulsoup4 lxml"
    )


# ============================================================
# CONFIG
# ============================================================

# Horizon Robotics has TWO identifiers on HKEX:
#   - Trading code: 9660 (used for stock quotes)
#   - Internal stockId: 106174 (used for hkexnews search)
STOCK_CODE = "9660"
HKEX_STOCK_ID = "106174"

KEYWORDS_CN = ["地平线机器人", "地平线", "Horizon Robotics", "9660"]

STATE_DIR = Path.home() / ".horizon_monitor"
STATE_FILE = STATE_DIR / "seen.json"
DEFAULT_DIGEST = "digest.md"
DEFAULT_JSON = "digest.json"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
HEADERS = {"User-Agent": USER_AGENT, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
TIMEOUT = 25

# ============================================================
# DATA MODEL
# ============================================================

@dataclass
class Item:
    source: str          # 'HKEX' / 'WSCN' / 'CAIXIN' / '36KR'
    category: str        # source-specific category
    date: str            # ISO 8601 string
    title: str
    url: str
    summary: str = ""
    importance: int = 2  # 1 (low/routine) — 5 (must-read)
    tags: list[str] = field(default_factory=list)

    @property
    def uid(self) -> str:
        # Stable id across runs based on source + url
        return hashlib.md5(f"{self.source}|{self.url}".encode()).hexdigest()[:14]


# ============================================================
# STATE PERSISTENCE
# ============================================================

def load_state() -> set[str]:
    STATE_DIR.mkdir(exist_ok=True)
    if STATE_FILE.exists():
        try:
            return set(json.loads(STATE_FILE.read_text()))
        except json.JSONDecodeError:
            return set()
    return set()


def save_state(seen: set[str]) -> None:
    STATE_FILE.write_text(json.dumps(sorted(seen)))


# ============================================================
# SOURCE 1 — HKEX DISCLOSURE PORTAL (PRIMARY)
# ============================================================
#
# This is the single most important source. Every material announcement
# for an HK-listed company MUST go through this portal first by law.
#
# Endpoint: https://www1.hkexnews.hk/search/titleSearchServlet.do
# Returns: JSON when called with ?lang=EN
#
# If HKEX changes their endpoint structure, this is where to debug.
# Fallback: parse the HTML at https://www1.hkexnews.hk/search/titlesearch.xhtml

def fetch_hkex(since_days: int = 365, lang: str = "EN") -> list[Item]:
    end_date = dt.date.today()
    start_date = end_date - dt.timedelta(days=since_days)

    url = "https://www1.hkexnews.hk/search/titleSearchServlet.do"
    params = {
        "sortDir": "0",
        "sortByOptions": "DateTime",
        "category": "0",
        "market": "SEHK",
        "stockId": HKEX_STOCK_ID,
        "documentType": "-1",
        "fromDate": start_date.strftime("%Y%m%d"),
        "toDate": end_date.strftime("%Y%m%d"),
        "title": "",
        "searchType": "1",
        "t1code": "-2",
        "t2Gcode": "-2",
        "t2code": "-2",
        "rowRange": "100",
        "lang": lang,
    }

    items: list[Item] = []
    try:
        r = requests.get(url, params=params, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()

        # HKEX servlet returns JSON-shaped text but content-type can be text/html.
        # Strip BOM/whitespace and parse manually.
        text = r.text.strip().lstrip("\ufeff")
        # Some responses come wrapped in callback paren — strip if so
        if text.startswith("(") and text.endswith(")"):
            text = text[1:-1]

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            # If it's not JSON, dump the first 500 chars so we can see what it is
            print(f"  [HKEX] response is not JSON. First 500 chars:", file=sys.stderr)
            print(f"  {text[:500]}", file=sys.stderr)
            return items

        # Unwrap dict if response is wrapped (e.g., {"result": [...]})
        if isinstance(data, dict):
            for key in ("result", "data", "results", "items", "records"):
                if key in data and isinstance(data[key], list):
                    data = data[key]
                    break
            else:
                # Single dict with no list field — log and bail
                print(f"  [HKEX] unexpected dict response, keys: {list(data.keys())}",
                      file=sys.stderr)
                return items

        if not isinstance(data, list):
            print(f"  [HKEX] expected list, got {type(data).__name__}", file=sys.stderr)
            return items

        # Multiple field-name conventions HKEX has used over the years:
        title_keys     = ["TITLE", "title", "DOC_TITLE", "doc_title", "newsTitle"]
        file_keys      = ["FILE_LINK", "file_link", "fileLink", "DOC_LINK", "doc_link"]
        category_keys  = ["LONG_TEXT", "long_text", "longText", "CATEGORY", "category", "headLine"]
        date_keys      = ["DATE_TIME", "date_time", "dateTime", "DATETIME", "RELEASE_TIME", "releaseTime"]

        def get_field(entry: dict, candidates: list[str]) -> str:
            """Try each candidate key; return first non-empty value."""
            for k in candidates:
                if k in entry and entry[k]:
                    return str(entry[k]).strip()
            return ""

        skipped = 0
        for i, entry in enumerate(data):
            try:
                # Skip non-dict entries (the original bug — sometimes this is a string)
                if not isinstance(entry, dict):
                    if i == 0:  # log shape on first occurrence
                        print(f"  [HKEX] entry not a dict, got {type(entry).__name__}: "
                              f"{str(entry)[:120]}", file=sys.stderr)
                    skipped += 1
                    continue

                title = get_field(entry, title_keys)
                file_link = get_field(entry, file_keys)
                cat = get_field(entry, category_keys)
                date_raw = get_field(entry, date_keys)

                # Skip entries with no title (probably a header row or filter)
                if not title:
                    skipped += 1
                    continue

                full_url = urljoin("https://www1.hkexnews.hk", file_link) if file_link else ""

                # Try several date formats
                date_iso = date_raw
                for fmt in ("%d/%m/%Y %H:%M", "%Y-%m-%d %H:%M:%S",
                            "%Y-%m-%d %H:%M", "%d/%m/%Y", "%Y%m%d"):
                    try:
                        date_iso = dt.datetime.strptime(date_raw, fmt).isoformat()
                        break
                    except ValueError:
                        continue

                score, tags = score_hkex(title, cat)
                items.append(Item(
                    source="HKEX",
                    category=cat,
                    date=date_iso,
                    title=title,
                    url=full_url,
                    summary="",
                    importance=score,
                    tags=tags,
                ))
            except Exception as e:
                # Don't let a single bad entry kill the whole batch
                print(f"  [HKEX] entry {i} parse error ({type(e).__name__}): {e}",
                      file=sys.stderr)
                skipped += 1

        if skipped > 0:
            print(f"  [HKEX] skipped {skipped} entries (no title or parse error)",
                  file=sys.stderr)

        if not items and data:
            # We got data back but couldn't parse any of it — dump shape for diagnosis
            sample = data[0] if data else None
            print(f"  [HKEX] could not parse any entry. First entry shape:",
                  file=sys.stderr)
            print(f"  type={type(sample).__name__}  preview={str(sample)[:300]}",
                  file=sys.stderr)
            if isinstance(sample, dict):
                print(f"  keys={list(sample.keys())}", file=sys.stderr)

    except requests.RequestException as e:
        print(f"  [HKEX] fetch failed: {type(e).__name__}: {e}", file=sys.stderr)
        if lang == "EN":
            print("  [HKEX] retrying with Chinese...", file=sys.stderr)
            return fetch_hkex(since_days=since_days, lang="ZH")
    return items


def score_hkex(title: str, category: str) -> tuple[int, list[str]]:
    """
    Importance scoring for HKEX disclosures.

    5 = must read (annual/interim report, profit alert, major contracts,
        material acquisitions/disposals, financing >5% of cap)
    4 = high (strategic transactions, share placements, JV announcements,
        material updates)
    3 = medium (routine announcements with substantive content)
    2 = default
    1 = noise (monthly returns, proxy forms, list of directors)
    """
    text = f"{title} {category}".lower()
    tags: list[str] = []

    # === HIGH-PRIORITY (importance 5) ===
    high5_patterns = [
        ("annual report", "annual_report"),
        ("interim report", "interim_report"),
        ("half-year", "interim_report"),
        ("year-end", "annual_report"),
        ("profit warning", "profit_alert"),
        ("profit alert", "profit_alert"),
        ("inside information", "inside_info"),
        ("results announcement", "results"),
        ("trading update", "trading_update"),
        ("年度报告", "annual_report"),
        ("中期报告", "interim_report"),
        ("盈利警告", "profit_alert"),
        ("内幕消息", "inside_info"),
    ]
    for pat, tag in high5_patterns:
        if pat in text:
            tags.append(tag)
            return 5, tags

    # === HIGH (importance 4) ===
    high4_patterns = [
        ("placing", "financing"),
        ("subscription", "financing"),
        ("issue of shares", "financing"),
        ("major transaction", "transaction"),
        ("notifiable transaction", "transaction"),
        ("connected transaction", "transaction"),
        ("very substantial", "transaction"),
        ("acquisition", "transaction"),
        ("disposal", "transaction"),
        ("strategic", "strategic"),
        ("cooperation", "strategic"),
        ("joint venture", "strategic"),
        ("配股", "financing"),
        ("增发", "financing"),
        ("收购", "transaction"),
        ("合作", "strategic"),
        ("战略", "strategic"),
    ]
    for pat, tag in high4_patterns:
        if pat in text:
            tags.append(tag)
            return 4, tags

    # === MEDIUM (importance 3) ===
    med_patterns = [
        ("share award", "incentive"),
        ("share option", "incentive"),
        ("rsu", "incentive"),
        ("restricted share", "incentive"),
        ("change of director", "governance"),
        ("appointment", "governance"),
        ("resignation", "governance"),
        ("circular", "circular"),
        ("agm", "agm"),
        ("egm", "agm"),
        ("股权激励", "incentive"),
        ("董事变更", "governance"),
        ("股东大会", "agm"),
    ]
    for pat, tag in med_patterns:
        if pat in text:
            tags.append(tag)
            return 3, tags

    # === LOW (importance 1) ===
    low_patterns = [
        "monthly return",
        "next day disclosure",
        "form of proxy",
        "list of directors",
        "constitutional documents",
        "月报表",
        "翌日披露",
    ]
    for pat in low_patterns:
        if pat in text:
            return 1, ["routine"]

    return 2, tags


# ============================================================
# SOURCE 2 — 华尔街见闻 (WALL STREET INSIGHTS)
# ============================================================
#
# Public search API exists at api.wallstcn.com.
# No auth required for search; auth required for full premium articles.

def fetch_wallstreetcn(keyword: str, limit: int = 20) -> list[Item]:
    api = "https://api.wallstcn.com/apiv1/search/articles"
    params = {
        "keyword": keyword,
        "limit": limit,
    }
    items: list[Item] = []
    try:
        r = requests.get(api, params=params, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
        articles = (
            data.get("data", {}).get("items")
            or data.get("data", {}).get("results")
            or []
        )
        for art in articles:
            title = (art.get("title") or "").strip()
            if not title or not _matches_company(title):
                continue
            uri = art.get("uri") or art.get("url") or ""
            aid = art.get("id") or ""
            full_url = (
                uri if uri.startswith("http")
                else f"https://wallstreetcn.com/articles/{aid}"
            )
            ts = art.get("display_time") or art.get("created_at") or 0
            if isinstance(ts, (int, float)) and ts > 0:
                date_iso = dt.datetime.fromtimestamp(int(ts)).isoformat()
            else:
                date_iso = str(ts)

            summary = (art.get("content_short") or art.get("summary") or "")[:240]
            score, tags = score_news(title, summary)

            items.append(Item(
                source="WSCN",
                category="新闻",
                date=date_iso,
                title=title,
                url=full_url,
                summary=summary,
                importance=score,
                tags=tags,
            ))
    except (requests.RequestException, json.JSONDecodeError) as e:
        print(f"  [WSCN] fetch failed: {type(e).__name__}: {e}", file=sys.stderr)
    return items


# ============================================================
# SOURCE 3 — 财新网 (CAIXIN) via Google site search fallback
# ============================================================
#
# Caixin doesn't expose a public search API. We use DuckDuckGo HTML
# (no API key needed) with a site: filter — gets us recent indexed
# articles. Result quality is good for cn.caixin.com and 36kr.com.

def fetch_via_ddg(site: str, keywords: list[str], source_label: str,
                  limit: int = 10) -> list[Item]:
    items: list[Item] = []
    base = "https://duckduckgo.com/html/"
    for kw in keywords[:2]:  # cap to avoid rate-limiting
        q = f"site:{site} {kw}"
        try:
            r = requests.post(
                base,
                data={"q": q},
                headers=HEADERS,
                timeout=TIMEOUT,
            )
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "lxml")
            for result in soup.select(".result")[:limit]:
                a = result.select_one(".result__title a") or result.select_one("a.result__a")
                snippet_el = result.select_one(".result__snippet")
                if not a:
                    continue
                title = a.get_text(strip=True)
                href = a.get("href", "")
                # DDG may return redirect URLs; extract the real one
                m = re.search(r"uddg=([^&]+)", href)
                if m:
                    from urllib.parse import unquote
                    href = unquote(m.group(1))
                if site not in href:
                    continue
                if not _matches_company(title):
                    continue
                summary = snippet_el.get_text(" ", strip=True) if snippet_el else ""
                score, tags = score_news(title, summary)
                items.append(Item(
                    source=source_label,
                    category="新闻",
                    date=dt.date.today().isoformat(),  # DDG doesn't expose date
                    title=title,
                    url=href,
                    summary=summary,
                    importance=score,
                    tags=tags,
                ))
            time.sleep(1.5)  # be polite
        except requests.RequestException as e:
            print(f"  [{source_label}] fetch failed: {type(e).__name__}: {e}",
                  file=sys.stderr)
            break
    return items


def _matches_company(text: str) -> bool:
    if not text:
        return False
    return any(k.lower() in text.lower() for k in KEYWORDS_CN)


# ============================================================
# NEWS IMPORTANCE SCORING
# ============================================================

def score_news(title: str, summary: str = "") -> tuple[int, list[str]]:
    text = f"{title} {summary}".lower()
    tags: list[str] = []

    # 5 — material event
    high5 = {
        "财报": "earnings", "业绩": "earnings", "营收": "earnings", "亏损": "earnings",
        "盈利": "earnings", "利润": "earnings", "毛利": "earnings",
        "停牌": "trading", "复牌": "trading",
        "重大": "material",
        "earnings": "earnings", "revenue": "earnings", "loss": "earnings",
    }
    for k, t in high5.items():
        if k in text:
            tags.append(t)
            return 5, tags

    # 4 — strategic / transaction
    high4 = {
        "中标": "contract", "定点": "contract", "签约": "contract",
        "合作": "partnership", "牵手": "partnership",
        "增持": "shareholding", "减持": "shareholding",
        "收购": "transaction", "并购": "transaction",
        "发布": "launch", "推出": "launch", "量产": "launch",
        "上车": "deployment", "搭载": "deployment",
        "HSD": "hsd", "城区": "city_noa", "NOA": "noa",
        "J6": "chip", "征程": "chip", "Journey": "chip",
        "大众": "vw", "Volkswagen": "vw", "酷睿程": "vw", "CARIZON": "vw",
    }
    for k, t in high4.items():
        if k in text:
            tags.append(t)
            return 4, tags

    # 3 — substantive but not material
    if any(k in text for k in ["进展", "更新", "升级", "扩张", "新增"]):
        return 3, tags

    return 2, tags


# ============================================================
# DIGEST RENDERING
# ============================================================

def write_digest(new_items: list[Item], all_items: list[Item],
                 since_days: int, path: str = DEFAULT_DIGEST) -> None:
    today = dt.datetime.now().strftime("%Y-%m-%d %H:%M")

    # Sort: highest importance first, then most recent
    def sort_key(it: Item):
        return (-it.importance, it.date)
    new_items = sorted(new_items, key=sort_key)

    # By source for the summary
    by_source: dict[str, int] = {}
    for it in new_items:
        by_source[it.source] = by_source.get(it.source, 0) + 1

    lines: list[str] = []
    lines.append("# 地平线机器人 (9660.HK) 监测摘要")
    lines.append("")
    lines.append(f"**生成于** {today}　|　**回溯** {since_days} 天　|　"
                 f"**新增** {len(new_items)} 条　|　**累计** {len(all_items)} 条")
    if by_source:
        breakdown = "　".join(f"{s}:{c}" for s, c in by_source.items())
        lines.append(f"**来源分布**: {breakdown}")
    lines.append("")
    lines.append("---")
    lines.append("")

    if not new_items:
        lines.append("> 本次运行没有新条目。")
        lines.append("")
        Path(path).write_text("\n".join(lines), encoding="utf-8")
        return

    buckets: dict[str, list[Item]] = {
        "must_read": [it for it in new_items if it.importance == 5],
        "high":      [it for it in new_items if it.importance == 4],
        "medium":    [it for it in new_items if it.importance == 3],
        "low":       [it for it in new_items if it.importance <= 2],
    }
    headers = {
        "must_read": "## 🔴 必读 (importance 5)",
        "high":      "## 🟠 高优先级 (importance 4)",
        "medium":    "## 🟡 中优先级 (importance 3)",
        "low":       "## ⚪ 低优先级 / 例行 (importance ≤ 2)",
    }

    for k in ["must_read", "high", "medium", "low"]:
        bucket = buckets[k]
        if not bucket:
            continue
        lines.append(headers[k])
        lines.append("")
        for it in bucket:
            lines.extend(render_item(it))
        lines.append("")

    Path(path).write_text("\n".join(lines), encoding="utf-8")


def render_item(it: Item) -> list[str]:
    src_emoji = {
        "HKEX":   "📋",
        "WSCN":   "📰",
        "CAIXIN": "🔍",
        "36KR":   "💼",
    }.get(it.source, "📌")
    date_short = it.date[:10] if it.date else "???"

    lines = [f"### {src_emoji} {it.title}"]
    lines.append("")
    meta = [f"`{date_short}`", f"`{it.source}`"]
    if it.category:
        meta.append(f"_{it.category}_")
    if it.tags:
        meta.append("`" + " ".join(it.tags) + "`")
    lines.append("　·　".join(meta))
    lines.append("")
    if it.summary:
        # Quote the summary, trim to single paragraph
        clean = re.sub(r"\s+", " ", it.summary).strip()
        lines.append(f"> {clean}")
        lines.append("")
    if it.url:
        lines.append(f"[→ 原文]({it.url})")
        lines.append("")
    lines.append("---")
    lines.append("")
    return lines


# ============================================================
# CLI
# ============================================================

def cmd_test() -> int:
    """Connectivity test for all sources."""
    print("Testing endpoints…\n")
    results: list[tuple[str, str, str]] = []

    # HKEX
    try:
        r = requests.get(
            "https://www1.hkexnews.hk/search/titleSearchServlet.do",
            params={
                "sortDir": "0", "sortByOptions": "DateTime",
                "category": "0", "market": "SEHK",
                "stockId": HKEX_STOCK_ID,
                "documentType": "-1",
                "fromDate": (dt.date.today() - dt.timedelta(days=30)).strftime("%Y%m%d"),
                "toDate": dt.date.today().strftime("%Y%m%d"),
                "title": "", "searchType": "1",
                "t1code": "-2", "t2Gcode": "-2", "t2code": "-2",
                "rowRange": "10", "lang": "EN",
            },
            headers=HEADERS, timeout=TIMEOUT,
        )
        results.append(("HKEX", "✅ OK" if r.status_code == 200 else f"⚠️ HTTP {r.status_code}",
                        f"{len(r.text)} bytes"))
    except Exception as e:
        results.append(("HKEX", "❌ FAIL", str(e)))

    # WSCN
    try:
        r = requests.get(
            "https://api.wallstcn.com/apiv1/search/articles",
            params={"keyword": "地平线机器人", "limit": 5},
            headers=HEADERS, timeout=TIMEOUT,
        )
        results.append(("WSCN", "✅ OK" if r.status_code == 200 else f"⚠️ HTTP {r.status_code}",
                        f"{len(r.text)} bytes"))
    except Exception as e:
        results.append(("WSCN", "❌ FAIL", str(e)))

    # DDG (used by Caixin/36Kr)
    try:
        r = requests.post(
            "https://duckduckgo.com/html/",
            data={"q": "site:caixin.com 地平线"},
            headers=HEADERS, timeout=TIMEOUT,
        )
        results.append(("DuckDuckGo", "✅ OK" if r.status_code == 200 else f"⚠️ HTTP {r.status_code}",
                        f"{len(r.text)} bytes"))
    except Exception as e:
        results.append(("DuckDuckGo", "❌ FAIL", str(e)))

    print(f"{'Source':<14} {'Status':<14} {'Detail'}")
    print("-" * 60)
    for src, status, detail in results:
        print(f"{src:<14} {status:<14} {detail}")
    print()
    print("If a source fails, the corresponding scraper will be skipped on `run`.")
    return 0


def cmd_run(args) -> int:
    if args.reset and STATE_FILE.exists():
        STATE_FILE.unlink()
        print("[state] cleared")

    seen = load_state()
    print(f"[start] lookback={args.since}d　seen={len(seen)}")

    all_items: list[Item] = []

    def _safe_fetch(label: str, fn, *fargs, **fkwargs) -> list[Item]:
        """Call a fetch function, log any uncaught error, return [] on failure."""
        try:
            return fn(*fargs, **fkwargs)
        except Exception as e:
            print(f"  [{label}] CRASHED ({type(e).__name__}): {e}", file=sys.stderr)
            import traceback
            traceback.print_exc(file=sys.stderr)
            return []

    if not args.no_hkex:
        print("[1] HKEX 披露…")
        items = _safe_fetch("HKEX", fetch_hkex, since_days=args.since)
        print(f"    → {len(items)} 条")
        all_items.extend(items)
        time.sleep(1)

    if not args.no_news:
        print("[2] 华尔街见闻…")
        wscn_items: list[Item] = []
        for kw in ["地平线机器人", "9660.HK", "Horizon Robotics"]:
            chunk = _safe_fetch("WSCN", fetch_wallstreetcn, keyword=kw, limit=20)
            wscn_items.extend(chunk)
            time.sleep(1.2)
        wscn_items = _dedupe(wscn_items)
        print(f"    → {len(wscn_items)} 条 (去重后)")
        all_items.extend(wscn_items)

        print("[3] 财新网…")
        cx = _safe_fetch("CAIXIN", fetch_via_ddg,
                         "caixin.com", ["地平线机器人", "9660.HK"], "CAIXIN", limit=10)
        print(f"    → {len(cx)} 条")
        all_items.extend(cx)
        time.sleep(2)

        print("[4] 36氪…")
        kr = _safe_fetch("36KR", fetch_via_ddg,
                         "36kr.com", ["地平线机器人"], "36KR", limit=10)
        print(f"    → {len(kr)} 条")
        all_items.extend(kr)

    all_items = _dedupe(all_items)
    new_items = [it for it in all_items if it.uid not in seen]
    print(f"[result] new={len(new_items)}　total={len(all_items)}")

    write_digest(new_items, all_items, args.since, path=args.out)
    print(f"[output] {args.out}")

    if args.json:
        json_path = Path(args.out).with_suffix(".json")
        json_path.write_text(json.dumps(
            {
                "generated_at": dt.datetime.now().isoformat(),
                "lookback_days": args.since,
                "new": [asdict(i) for i in new_items],
                "all": [asdict(i) for i in all_items],
            },
            ensure_ascii=False, indent=2,
        ))
        print(f"[output] {json_path}")

    for it in new_items:
        seen.add(it.uid)
    save_state(seen)
    print(f"[state] saved (now tracking {len(seen)} items)")
    return 0


def _dedupe(items: list[Item]) -> list[Item]:
    seen_urls: set[str] = set()
    out: list[Item] = []
    for it in items:
        key = it.url or it.title
        if key in seen_urls:
            continue
        seen_urls.add(key)
        out.append(it)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Horizon Robotics 9660.HK 信息源监测",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python3 horizon_monitor.py --test\n"
            "  python3 horizon_monitor.py --since 365 --json\n"
            "  python3 horizon_monitor.py --no-news --reset\n"
        ),
    )
    ap.add_argument("--since", type=int, default=180,
                    help="lookback window in days (default: 180)")
    ap.add_argument("--reset", action="store_true",
                    help="clear seen-state, treat everything as new")
    ap.add_argument("--json", action="store_true",
                    help="also emit digest.json with structured data")
    ap.add_argument("--no-hkex", action="store_true", help="skip HKEX")
    ap.add_argument("--no-news", action="store_true", help="skip news sources")
    ap.add_argument("--out", default=DEFAULT_DIGEST,
                    help=f"digest output path (default: {DEFAULT_DIGEST})")
    ap.add_argument("--test", action="store_true",
                    help="connectivity test only, no scraping")
    args = ap.parse_args()

    if args.test:
        return cmd_test()
    return cmd_run(args)


if __name__ == "__main__":
    sys.exit(main())
