#!/usr/bin/env python3
"""
Horizon Robotics (9660.HK) Disclosure & News Monitor v2
========================================================

Monitors reliable, high-signal sources for material information about
Horizon Robotics (地平线机器人, HK:9660).

TIER 1 — Official / Mandatory:
  HKEX        - hkexnews.hk official disclosure portal

TIER 2 — High-signal Professional Media:
  36Kr        - 36kr.com (tech/business, original reporting)
  Sohu        - sohu.com (high-volume reprint hub, good aggregator)
  Iyiou       - iyiou.com / 亿欧 (tech business, original reporting)

TIER 3 — Supplementary:
  QQ News     - news.qq.com (occasional in-depth coverage)
  WSCN        - wallstreetcn.com (search indexing poor, low yield)
  Caixin      - caixin.com (search indexing poor, low yield)

NEW IN v2 — Source Reliability Scoring:
  Each item now carries a `source_score` (1-5) rating how reliable
  the source is for this specific piece of information.
  The digest includes a "来源可靠性评估" section ranking sources.

EXPLICITLY EXCLUDED:
  Bloomberg/WSJ/Reuters - paywall + minimal Horizon coverage
  Xueqiu/Eastmoney - retail-investor noise
  Toutiao/Baijiahao - low-quality aggregation, duplicates Sohu coverage

USAGE
  python3 horizon_monitor.py                 # full run, write digest.md
  python3 horizon_monitor.py --since 365     # look back 365 days
  python3 horizon_monitor.py --reset         # forget seen state
  python3 horizon_monitor.py --json          # also emit digest.json
  python3 horizon_monitor.py --no-news       # only HKEX filings
  python3 horizon_monitor.py --test          # connectivity test only
  python3 horizon_monitor.py --diagnose      # source reliability audit

State (which items were already seen) is stored in
  ~/.horizon_monitor/seen.json

DEPENDENCIES
  pip install requests beautifulsoup4 lxml duckduckgo_search
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
        "    pip install requests beautifulsoup4 lxml duckduckgo_search"
    )

try:
    from duckduckgo_search import DDGS  # type: ignore
    HAS_DDGS = True
except ImportError:
    HAS_DDGS = False


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
    source: str          # 'HKEX' / 'WSCN' / 'CAIXIN' / '36KR' / 'SOHU' / 'IYIOU' / 'QQNEWS'
    category: str        # source-specific category
    date: str            # ISO 8601 string
    title: str
    url: str
    summary: str = ""
    importance: int = 2  # 1 (low/routine) — 5 (must-read)
    tags: list[str] = field(default_factory=list)
    source_score: int = 3  # 1 (noise) — 5 (authoritative)

    @property
    def uid(self) -> str:
        # Stable id across runs based on source + url
        return hashlib.md5(f"{self.source}|{self.url}".encode()).hexdigest()[:14]


# ============================================================
# SOURCE RELIABILITY DATABASE
# ============================================================
#
# Each source is rated on 4 dimensions:
#   authority    - Is it official/regulated/professional? (1-5)
#   precision    - How often are search results actually about Horizon? (0-1)
#   freshness    - Are the results timely? (1-5)
#   uniqueness   - Does it find things other sources miss? (1-5)
#
# The `base_score` is the composite reliability score (1-5) used as default
# for source_score on items from this source.

@dataclass
class SourceProfile:
    key: str
    label: str
    authority: int      # 1-5
    precision: float    # 0-1
    freshness: int      # 1-5
    uniqueness: int     # 1-5
    base_score: int     # 1-5, derived from above
    notes: str = ""


SOURCE_PROFILES: dict[str, SourceProfile] = {
    "HKEX": SourceProfile(
        key="HKEX", label="港交所披露易",
        authority=5, precision=1.0, freshness=5, uniqueness=5, base_score=5,
        notes="官方披露平台,100%准确,法律效力。任何地平线的年报/中报/内幕消息/配股/董事变更均在此披露。",
    ),
    "36KR": SourceProfile(
        key="36KR", label="36氪",
        authority=4, precision=0.80, freshness=4, uniqueness=4, base_score=4,
        notes="科技商业媒体,有原创深度报道。地平线融资/战略/产品发布常在此首发。",
    ),
    "SOHU": SourceProfile(
        key="SOHU", label="搜狐",
        authority=3, precision=0.70, freshness=4, uniqueness=3, base_score=3,
        notes="综合媒体,高频转载专业财经内容。地平线相关文章量大,但部分是转载,需注意原创性。",
    ),
    "IYIOU": SourceProfile(
        key="IYIOU", label="亿欧",
        authority=4, precision=0.75, freshness=3, uniqueness=4, base_score=4,
        notes="科技产业媒体,有行业深度分析。地平线作为头部智驾公司常被覆盖。",
    ),
    "QQNEWS": SourceProfile(
        key="QQNEWS", label="腾讯新闻",
        authority=3, precision=0.65, freshness=4, uniqueness=3, base_score=3,
        notes="综合新闻平台,偶尔有地平线深度报道,但噪音较大。",
    ),
    "WSCN": SourceProfile(
        key="WSCN", label="华尔街见闻",
        authority=3, precision=0.60, freshness=3, uniqueness=2, base_score=3,
        notes="财经快讯平台,但搜索引擎索引质量差。site:搜索命中率低,实际内容存在但难以抓取。",
    ),
    "CAIXIN": SourceProfile(
        key="CAIXIN", label="财新网",
        authority=5, precision=0.50, freshness=2, uniqueness=3, base_score=3,
        notes="权威财经媒体,但搜索引擎索引质量差。site:搜索命中率极低,且多为付费墙内容。",
    ),
}

SOURCE_EMOJI = {
    "HKEX": "📋", "36KR": "💼", "SOHU": "📰",
    "IYIOU": "🔬", "QQNEWS": "📡", "WSCN": "📉", "CAIXIN": "🔍",
}


def get_source_score(source: str) -> int:
    """Get base reliability score for a source."""
    profile = SOURCE_PROFILES.get(source)
    return profile.base_score if profile else 2


def score_item_source(item: Item) -> int:
    """
    Adjust source_score based on content signals.
    Items with high importance get a +1 boost (the source is delivering signal).
    Items with low importance get -1 (routine noise).
    """
    base = get_source_score(item.source)
    if item.importance >= 5:
        return min(5, base + 1)
    if item.importance <= 1:
        return max(1, base - 1)
    return base


# ============================================================
# KPI TRACKING - 核心财务指标跟踪
# ============================================================

KPI_BASELINE = {
    "as_of": "2025FY",
    "report_date": "2026-03-19",
    "report_url": "https://stockn.xueqiu.com/09660/20260319282482.pdf",
    "revenue_cny_mm": 3758,
    "revenue_yoy": 0.577,
    "gm_overall": 0.645,
    "gm_hardware": 0.345,
    "gm_software": 0.93,
    "gm_nonauto": 0.50,
    "rd_pct_revenue": 1.371,
    "adj_op_loss_cny_mm": -2372,
    "shipments_units_mm": 4.01,
    "highend_mix": 0.45,
    "highend_shipments_units_mm": 1.80,
    "cumulative_shipments_units_mm": 11.7,
    "revenue_mix_product": 0.43,
    "revenue_mix_license": 0.52,
    "revenue_mix_nonauto": 0.05,
    "market_share_l2_adas": 0.477,
    "market_share_city_noa": 0.144,
    "hsd_design_wins": 20,
    "hsd_deployed_units": 22000,
}

KPI_THRESHOLDS = {
    "gm_overall":         (0.65,  0.55,  "higher_better"),
    "gm_hardware":        (0.38,  0.28,  "higher_better"),
    "gm_software":        (0.92,  0.85,  "higher_better"),
    "revenue_yoy":        (0.50,  0.25,  "higher_better"),
    "highend_mix":        (0.55,  0.40,  "higher_better"),
    "rd_pct_revenue":     (1.00,  1.50,  "lower_better"),
    "market_share_l2_adas":  (0.45,  0.35,  "higher_better"),
    "market_share_city_noa": (0.18,  0.10,  "higher_better"),
}

KPI_LABELS = {
    "gm_overall":            "综合毛利率",
    "gm_hardware":           "硬件 GM (汽车产品方案)",
    "gm_software":           "软件 GM (汽车授权服务)",
    "revenue_yoy":           "营收同比增速",
    "highend_mix":           "中高阶出货占比",
    "rd_pct_revenue":        "研发费用率",
    "market_share_l2_adas":  "L2 ADAS 市占率 (自主)",
    "market_share_city_noa": "城区 NOA 市占率 (自主)",
}

KPI_BASELINE_FILE = STATE_DIR / "kpi_baseline.json"
KPI_HISTORY_FILE = STATE_DIR / "kpi_history.json"


def load_kpi_baseline() -> dict:
    if KPI_BASELINE_FILE.exists():
        try:
            return json.loads(KPI_BASELINE_FILE.read_text())
        except json.JSONDecodeError:
            pass
    return dict(KPI_BASELINE)


def save_kpi_baseline(kpi: dict) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    KPI_BASELINE_FILE.write_text(json.dumps(kpi, indent=2, ensure_ascii=False))


def load_kpi_history() -> list:
    if KPI_HISTORY_FILE.exists():
        try:
            return json.loads(KPI_HISTORY_FILE.read_text())
        except json.JSONDecodeError:
            pass
    return []


def append_kpi_history(kpi: dict) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    history = load_kpi_history()
    history.append(kpi)
    KPI_HISTORY_FILE.write_text(json.dumps(history, indent=2, ensure_ascii=False))


def score_kpi(value, healthy: float, danger: float, direction: str) -> str:
    if value is None:
        return "GRAY"
    if direction == "higher_better":
        if value >= healthy:
            return "GREEN"
        if value <= danger:
            return "RED"
        return "YELLOW"
    else:
        if value <= healthy:
            return "GREEN"
        if value >= danger:
            return "RED"
        return "YELLOW"


# ============================================================
# KPI SIGNAL EXTRACTION FROM NEWS
# ============================================================

GM_KEYWORDS = [
    "毛利率", "毛利", "gross margin", "gm", "毛利率下滑", "毛利率提升",
    "硬件毛利", "软件毛利", "产品毛利", "授权毛利",
    "结构性下滑", "业务组合", "收入组合",
    "margin compression", "margin expansion", "blended margin",
]

VOLUME_KEYWORDS = [
    "出货量", "出货", "shipments", "交付量", "累计出货",
    "中高阶", "中高端", "high-end", "high end", "高阶占比",
    "ASP", "单价", "平均售价", "average system price",
]

OPEX_KEYWORDS = [
    "研发费用", "研发投入", "R&D", "费用率", "运营杠杆",
    "管理费用", "销售费用", "operating expense",
]


def extract_kpi_signals(items: list[Item]) -> dict:
    signals = {
        "gross_margin": [],
        "volume_asp":   [],
        "opex":         [],
    }
    for item in items:
        text = f"{item.title} {item.summary}".lower()
        if any(k.lower() in text for k in GM_KEYWORDS):
            signals["gross_margin"].append(item)
        if any(k.lower() in text for k in VOLUME_KEYWORDS):
            signals["volume_asp"].append(item)
        if any(k.lower() in text for k in OPEX_KEYWORDS):
            signals["opex"].append(item)
    return signals


def render_kpi_dashboard(path: str = "kpi_dashboard.md") -> None:
    kpi = load_kpi_baseline()
    history = load_kpi_history()

    lines: list[str] = []
    lines.append("# 地平线 (9660.HK) KPI 跟踪仪表盘")
    lines.append("")
    lines.append(f"**基线快照**: {kpi.get('as_of', '?')}　|　"
                 f"**报告日期**: {kpi.get('report_date', '?')}　|　"
                 f"**已记录历史**: {len(history)} 期")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 核心健康度指标")
    lines.append("")
    lines.append("| 指标 | 最新值 | 健康线 | 警戒线 | 状态 |")
    lines.append("|---|---:|---:|---:|:---:|")

    status_emoji = {"GREEN": "🟢", "YELLOW": "🟡", "RED": "🔴", "GRAY": "⚪"}
    for key, (healthy, danger, direction) in KPI_THRESHOLDS.items():
        value = kpi.get(key)
        label = KPI_LABELS.get(key, key)
        status = score_kpi(value, healthy, danger, direction)
        emoji = status_emoji[status]

        def fmt(v):
            if v is None:
                return "—"
            return f"{v*100:.1f}%" if abs(v) < 5 else f"{v:.1f}"

        lines.append(f"| {label} | {fmt(value)} | {fmt(healthy)} | "
                     f"{fmt(danger)} | {emoji} {status} |")

    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 毛利率结构 (核心关注点)")
    lines.append("")
    gm_o = kpi.get("gm_overall")
    gm_h = kpi.get("gm_hardware")
    gm_s = kpi.get("gm_software")
    mix_p = kpi.get("revenue_mix_product")
    mix_l = kpi.get("revenue_mix_license")

    if all(v is not None for v in [gm_o, gm_h, gm_s, mix_p, mix_l]):
        lines.append(f"- **综合毛利率**: {gm_o*100:.1f}%")
        lines.append(f"- **硬件业务** (占营收 {mix_p*100:.0f}%): GM = {gm_h*100:.1f}%　← 关键跟踪")
        lines.append(f"- **软件业务** (占营收 {mix_l*100:.0f}%): GM = {gm_s*100:.1f}%　← 护城河")
        lines.append("")
        spread = gm_s - gm_h
        lines.append(f"软件 vs 硬件 GM 差: **{spread*100:.0f}pp** "
                     f"(差异越大, 业务结构变化对综合 GM 影响越大)")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("## 历史趋势")
    lines.append("")
    if not history:
        lines.append("> 尚无历史快照。运行 `python3 horizon_monitor.py kpi update` 录入新报告数据后, 历史会自动累积。")
    else:
        lines.append("| 期间 | 综合 GM | 硬件 GM | 软件 GM | 高端占比 | 营收 YoY |")
        lines.append("|---|---:|---:|---:|---:|---:|")
        for snap in history[-10:]:
            period = snap.get("as_of", "?")
            def f(v):
                return f"{v*100:.1f}%" if isinstance(v, (int, float)) else "—"
            lines.append(f"| {period} | {f(snap.get('gm_overall'))} | "
                         f"{f(snap.get('gm_hardware'))} | {f(snap.get('gm_software'))} | "
                         f"{f(snap.get('highend_mix'))} | {f(snap.get('revenue_yoy'))} |")

    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 操作说明")
    lines.append("")
    lines.append("- 新半年报/年报披露后, 运行 `python3 horizon_monitor.py kpi update`")
    lines.append("- 查看当前基线: `python3 horizon_monitor.py kpi show`")
    lines.append("- 重置基线: 删除 `~/.horizon_monitor/kpi_baseline.json`")
    lines.append("")
    lines.append(f"_报告原文_: [{kpi.get('report_url', '')}]({kpi.get('report_url', '')})")

    Path(path).write_text("\n".join(lines), encoding="utf-8")


def cmd_kpi_show() -> int:
    kpi = load_kpi_baseline()
    print(f"\n=== Current KPI Baseline ({kpi.get('as_of', '?')}) ===\n")
    for key, label in KPI_LABELS.items():
        value = kpi.get(key)
        if value is None:
            print(f"  {label:<32} : —")
        elif abs(value) < 5:
            print(f"  {label:<32} : {value*100:.2f}%")
        else:
            print(f"  {label:<32} : {value:,.2f}")

    print("\n=== Health Status ===\n")
    for key, (healthy, danger, direction) in KPI_THRESHOLDS.items():
        value = kpi.get(key)
        label = KPI_LABELS.get(key, key)
        status = score_kpi(value, healthy, danger, direction)
        marker = {"GREEN": "✓", "YELLOW": "!", "RED": "✗", "GRAY": "?"}[status]
        print(f"  [{marker}] {label:<32} : {status}")
    print()
    return 0


def cmd_kpi_update() -> int:
    print("\n=== KPI Update - 录入新报告数据 ===")
    print("Enter new values (press Enter to keep current baseline value)\n")

    current = load_kpi_baseline()
    new_kpi = dict(current)

    def prompt(key: str, label: str, is_pct: bool = True):
        cur = current.get(key)
        if cur is None:
            cur_str = "—"
        elif is_pct and abs(cur) < 5:
            cur_str = f"{cur*100:.2f}%"
        else:
            cur_str = f"{cur:,.2f}"
        s = input(f"  {label} [{cur_str}]: ").strip()
        if not s:
            return cur
        try:
            v = float(s.rstrip("%"))
            if is_pct and "%" in s:
                v = v / 100
            elif is_pct and abs(v) > 5:
                print(f"    (interpreting {v} as {v}%)")
                v = v / 100
            return v
        except ValueError:
            print(f"    ⚠ Invalid input '{s}', keeping {cur_str}")
            return cur

    period = input(f"  报告期间 e.g. 2026H1 [{current.get('as_of')}]: ").strip()
    if period:
        new_kpi["as_of"] = period
    report_date = input(f"  报告日期 YYYY-MM-DD [{current.get('report_date')}]: ").strip()
    if report_date:
        new_kpi["report_date"] = report_date
    url = input(f"  报告 URL (可选): ").strip()
    if url:
        new_kpi["report_url"] = url

    print("\n--- 收入与盈利 ---")
    new_kpi["revenue_cny_mm"] = prompt("revenue_cny_mm", "营收 CNY mm", is_pct=False)
    new_kpi["revenue_yoy"] = prompt("revenue_yoy", "营收同比")
    new_kpi["gm_overall"] = prompt("gm_overall", "综合毛利率")
    new_kpi["gm_hardware"] = prompt("gm_hardware", "硬件 GM (汽车产品方案)")
    new_kpi["gm_software"] = prompt("gm_software", "软件 GM (汽车授权服务)")
    new_kpi["rd_pct_revenue"] = prompt("rd_pct_revenue", "研发费用率")
    new_kpi["adj_op_loss_cny_mm"] = prompt("adj_op_loss_cny_mm", "经调整经营亏损 CNY mm", is_pct=False)

    print("\n--- 出货量 ---")
    new_kpi["shipments_units_mm"] = prompt("shipments_units_mm", "出货量 百万套", is_pct=False)
    new_kpi["highend_mix"] = prompt("highend_mix", "中高阶占比")
    new_kpi["highend_shipments_units_mm"] = prompt("highend_shipments_units_mm", "中高阶出货 百万套", is_pct=False)

    print("\n--- 业务结构 ---")
    new_kpi["revenue_mix_product"] = prompt("revenue_mix_product", "硬件业务占营收")
    new_kpi["revenue_mix_license"] = prompt("revenue_mix_license", "软件业务占营收")

    print("\n--- 市占率 ---")
    new_kpi["market_share_l2_adas"] = prompt("market_share_l2_adas", "L2 ADAS 市占率")
    new_kpi["market_share_city_noa"] = prompt("market_share_city_noa", "城区 NOA 市占率")

    print("\n=== 变动概览 ===")
    changes = []
    for key in KPI_THRESHOLDS:
        old = current.get(key)
        new = new_kpi.get(key)
        if old is not None and new is not None and old != new:
            delta = new - old
            label = KPI_LABELS.get(key, key)
            if abs(old) < 5:
                changes.append(f"  {label}: {old*100:.1f}% → {new*100:.1f}%  "
                               f"({'↑' if delta > 0 else '↓'}{abs(delta)*100:.1f}pp)")
            else:
                changes.append(f"  {label}: {old:.2f} → {new:.2f}  "
                               f"({'↑' if delta > 0 else '↓'}{abs(delta):.2f})")
    if changes:
        for c in changes:
            print(c)
    else:
        print("  (no changes)")

    print()
    confirm = input("保存？ (y/n): ").strip().lower()
    if confirm != "y":
        print("已取消")
        return 1

    append_kpi_history(current)
    save_kpi_baseline(new_kpi)

    print("\n=== 新基线健康状态 ===")
    warnings_found = []
    for key, (healthy, danger, direction) in KPI_THRESHOLDS.items():
        value = new_kpi.get(key)
        old_value = current.get(key)
        label = KPI_LABELS.get(key, key)
        status = score_kpi(value, healthy, danger, direction)
        old_status = score_kpi(old_value, healthy, danger, direction)
        marker = {"GREEN": "✓", "YELLOW": "!", "RED": "✗", "GRAY": "?"}[status]
        change = ""
        if status != old_status:
            change = f"  ← was {old_status}"
            if status == "RED" or (status == "YELLOW" and old_status == "GREEN"):
                warnings_found.append(label)
        print(f"  [{marker}] {label:<32} : {status}{change}")

    if warnings_found:
        print("\n⚠ 以下指标恶化, 建议重新审视投资逻辑:")
        for w in warnings_found:
            print(f"    - {w}")

    print(f"\n✓ 基线已更新, 旧基线归档到历史 (共 {len(load_kpi_history())} 期)")
    print(f"✓ 运行 `python3 horizon_monitor.py kpi dashboard` 生成最新仪表盘")
    return 0


def cmd_kpi_dashboard(args) -> int:
    path = getattr(args, 'out_kpi', None) or "kpi_dashboard.md"
    render_kpi_dashboard(path)
    print(f"✓ KPI dashboard written to {path}")
    return 0


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
# FIXED (2026-06-01): HKEX requires a 3-step JSF session-based approach.
# The old code called titleSearchServlet.do directly, which always returns
# recordCnt=0 without a valid JSF session. The fix:
#   1. GET titlesearch.xhtml to obtain ViewState + form action
#   2. POST the JSF form to initialize the session with date range
#   3. GET titleSearchServlet.do with proper headers to fetch JSON

def fetch_hkex(since_days: int = 365, lang: str = "EN") -> list[Item]:
    end_date = dt.date.today()
    start_date = end_date - dt.timedelta(days=since_days)
    from_str = start_date.strftime("%Y%m%d")
    to_str = end_date.strftime("%Y%m%d")

    HKEX_BASE = "https://www1.hkexnews.hk"
    SEARCH_PAGE = f"{HKEX_BASE}/search/titlesearch.xhtml"
    API_ENDPOINT = f"{HKEX_BASE}/search/titleSearchServlet.do"

    items: list[Item] = []

    try:
        session = requests.Session()
        session.headers.update(HEADERS)

        # Step 1: GET search page to extract ViewState and form action
        page_resp = session.get(
            SEARCH_PAGE,
            params={
                "sortDir": "0",
                "sortByRecordDate": "on",
                "searchType": "0",
                "category": "0",
                "t1code": "-2",
                "t2Gcode": "-2",
                "t2code": "-2",
                "documentType": "-1",
                "rowRange": "0",
                "lang": "EN",
            },
            timeout=TIMEOUT,
        )
        page_resp.raise_for_status()

        vs_match = re.search(r'javax\.faces\.ViewState.*?value="([^"]+)"', page_resp.text)
        view_state = vs_match.group(1) if vs_match else ""
        fa_match = re.search(r'<form[^>]*action="([^"]+)"', page_resp.text)
        form_action = fa_match.group(1) if fa_match else ""

        if not view_state or not form_action:
            print("  [HKEX] Could not extract ViewState/form action from search page",
                  file=sys.stderr)
            return items

        # Step 2: POST form to initialize session with date range
        submit_url = f"{HKEX_BASE}{form_action}" if form_action.startswith("/") else form_action
        session.post(
            submit_url,
            data={
                "j_idt10": "j_idt10",
                "j_idt10:loadMoreRange": "100",
                "javax.faces.ViewState": view_state,
                "from": from_str,
                "to": to_str,
            },
            timeout=TIMEOUT,
        )

        # Step 3: GET JSON API with proper headers
        api_headers = {
            **HEADERS,
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Referer": SEARCH_PAGE,
            "X-Requested-With": "XMLHttpRequest",
        }

        all_records: list[dict] = []
        fetched = 0
        while True:
            r = session.get(
                API_ENDPOINT,
                params={
                    "sortDir": "0",
                    "sortByOptions": "DateTime",
                    "category": "0",
                    "market": "SEHK",
                    "stockId": HKEX_STOCK_ID,
                    "documentType": "-1",
                    "fromDate": from_str,
                    "toDate": to_str,
                    "title": "",
                    "searchType": "0",
                    "t1code": "-2",
                    "t2Gcode": "-2",
                    "t2code": "-2",
                    "rowRange": str(fetched + 5000),
                    "lang": lang,
                },
                headers=api_headers,
                timeout=TIMEOUT,
            )
            r.raise_for_status()

            try:
                data = r.json()
            except json.JSONDecodeError:
                print(f"  [HKEX] response is not JSON. First 300 chars: {r.text[:300]}",
                      file=sys.stderr)
                break

            result_raw = data.get("result", "null")
            if not result_raw or result_raw == "null":
                break

            if isinstance(result_raw, str):
                records = json.loads(result_raw)
            else:
                records = result_raw

            if not records:
                break

            has_next = data.get("hasNextRow", False)
            new_records = records[fetched:] if fetched < len(records) else []
            all_records.extend(new_records)
            fetched = len(records)

            if not has_next:
                break

            time.sleep(0.3)

        for entry in all_records:
            if not isinstance(entry, dict):
                continue
            title = (entry.get("TITLE") or entry.get("title") or "").strip()
            if not title:
                continue
            title = re.sub(r"<[^>]+>", " ", title)
            title = " ".join(title.split())
            file_link = entry.get("FILE_LINK", "") or entry.get("file_link", "")
            cat = (entry.get("LONG_TEXT") or entry.get("long_text") or
                   entry.get("SHORT_TEXT") or entry.get("short_text") or "").strip()
            cat = re.sub(r"<[^>]+>", " ", cat)[:120]
            date_raw = (entry.get("DATE_TIME") or entry.get("date_time") or "").strip()
            full_url = f"{HKEX_BASE}{file_link}" if file_link else ""

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

        print(f"  [HKEX] lang={lang} → {len(all_records)} raw records, {len(items)} parsed",
              file=sys.stderr)

    except requests.RequestException as e:
        print(f"  [HKEX] fetch failed: {type(e).__name__}: {e}", file=sys.stderr)
        if lang == "EN":
            print("  [HKEX] retrying with Chinese...", file=sys.stderr)
            return fetch_hkex(since_days=since_days, lang="ZH")

    return items


def score_hkex(title: str, category: str) -> tuple[int, list[str]]:
    text = f"{title} {category}".lower()
    tags: list[str] = []

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
# SOURCE 2/3/4 — NEWS via DuckDuckGo Search (unified)
# ============================================================
#
# FIXED (2026-06-01): Replaced broken wallstreetcn API and DDG HTML scraping
# with the duckduckgo_search Python library. This library uses DDG's non-JS
# lite API, which is much more reliable than raw HTML POST requests.
#
# All three news sources (WSCN, Caixin, 36Kr) now use the same function.

def _fetch_news_ddg(site: str, source_label: str,
                    max_results: int = 15) -> list[Item]:
    """Fetch news from a site using DuckDuckGo text search (duckduckgo_search lib)."""
    items: list[Item] = []
    if not HAS_DDGS:
        print(f"  [{source_label}] duckduckgo_search not installed, skipping",
              file=sys.stderr)
        return items

    try:
        with DDGS() as ddgs:
            for kw in KEYWORDS_CN[:2]:
                query = f"site:{site} {kw}"
                try:
                    results = list(ddgs.text(query, max_results=max_results))
                    for r in results:
                        title = (r.get("title") or "").strip()
                        href = r.get("href") or ""
                        if not title or not href:
                            continue
                        if site not in href:
                            continue
                        if not _matches_company(title):
                            continue
                        body = (r.get("body") or "")[:240]
                        date_str = r.get("date", "")
                        score, tags = score_news(title, body)
                        items.append(Item(
                            source=source_label,
                            category="新闻",
                            date=date_str or dt.date.today().isoformat(),
                            title=title,
                            url=href,
                            summary=body,
                            importance=score,
                            tags=tags,
                        ))
                except Exception as e:
                    print(f"  [{source_label}] DDG error for query '{query}': "
                          f"{type(e).__name__}: {e}", file=sys.stderr)
                time.sleep(0.5)
    except Exception as e:
        print(f"  [{source_label}] fetch failed: {type(e).__name__}: {e}",
              file=sys.stderr)
    return items


def fetch_wallstreetcn(keyword: str = "", limit: int = 20) -> list[Item]:
    """Search wallstreetcn.com via DDG. (keyword/limit kept for API compat, ignored)"""
    return _fetch_news_ddg("wallstreetcn.com", "WSCN", max_results=limit)


def fetch_via_ddg(site: str, keywords: list[str], source_label: str,
                  limit: int = 10) -> list[Item]:
    """Search via DDG. (keywords kept for API compat, ignored)"""
    return _fetch_news_ddg(site, source_label, max_results=limit)


# ============================================================
# NEW SOURCES (v2) — Added after 2026-06-01 source audit
# ============================================================
#
# SOHU  - sohu.com articles consistently show up in search results
#         for Horizon Robotics keywords; high volume, mostly reprints
# IYIOU - iyiou.com / 亿欧 has tech-business coverage of Horizon
# QQNEWS - news.qq.com occasionally has in-depth Horizon coverage

def fetch_sohu(limit: int = 15) -> list[Item]:
    """Fetch Horizon Robotics articles from sohu.com via DDG."""
    return _fetch_news_ddg("sohu.com", "SOHU", max_results=limit)


def fetch_iyiou(limit: int = 15) -> list[Item]:
    """Fetch Horizon Robotics articles from iyiou.com via DDG."""
    return _fetch_news_ddg("iyiou.com", "IYIOU", max_results=limit)


def fetch_qqnews(limit: int = 15) -> list[Item]:
    """Fetch Horizon Robotics articles from news.qq.com via DDG."""
    return _fetch_news_ddg("news.qq.com", "QQNEWS", max_results=limit)


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

    high5 = {
        "财报": "earnings", "业绩": "earnings", "营收": "earnings", "亏损": "earnings",
        "盈利": "earnings", "利润": "earnings",
        "毛利率": "gross_margin", "毛利": "gross_margin",
        "gross margin": "gross_margin", "硬件毛利": "gross_margin_hw",
        "软件毛利": "gross_margin_sw", "毛利率下滑": "gm_decline",
        "停牌": "trading", "复牌": "trading",
        "重大": "material",
        "earnings": "earnings", "revenue": "earnings", "loss": "earnings",
    }
    for k, t in high5.items():
        if k in text:
            tags.append(t)
            return 5, tags

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
        "中高阶": "highend_mix", "占比": "mix_signal",
        "ASP": "asp", "单价": "asp", "平均售价": "asp",
        "研发费用": "rd_spend", "费用率": "opex_ratio",
    }
    for k, t in high4.items():
        if k in text:
            tags.append(t)
            return 4, tags

    if any(k in text for k in ["进展", "更新", "升级", "扩张", "新增"]):
        return 3, tags

    return 2, tags


# ============================================================
# DIGEST RENDERING
# ============================================================

def write_digest(new_items: list[Item], all_items: list[Item],
                 since_days: int, path: str = DEFAULT_DIGEST) -> None:
    today = dt.datetime.now().strftime("%Y-%m-%d %H:%M")

    def sort_key(it: Item):
        return (-it.importance, it.date)
    new_items = sorted(new_items, key=sort_key)

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

    # ─── Source Reliability Assessment ───
    lines.append("---")
    lines.append("")
    lines.append("## 📊 来源可靠性评估")
    lines.append("")
    lines.append("| 来源 | 类型 | 权威性 | 精确度 | 时效性 | 独特价值 | 综合可靠性 | 本次产出 |")
    lines.append("|---|---:|---:|---:|---:|:---:|:---:|")
    for src_key in ["HKEX", "36KR", "SOHU", "IYIOU", "QQNEWS", "WSCN", "CAIXIN"]:
        profile = SOURCE_PROFILES.get(src_key)
        if not profile:
            continue
        count = by_source.get(src_key, 0)
        count_str = f"**{count} 条**" if count > 0 else "0 条"
        score_bar = "█" * profile.base_score + "░" * (5 - profile.base_score)
        lines.append(
            f"| {SOURCE_EMOJI.get(src_key, '📌')} {profile.label} | "
            f"{profile.notes[:24]}... | "
            f"{'⭐' * profile.authority} | "
            f"{profile.precision*100:.0f}% | "
            f"{'⭐' * profile.freshness} | "
            f"{'⭐' * profile.uniqueness} | "
            f"`{score_bar}` {profile.base_score}/5 | "
            f"{count_str} |"
        )
    lines.append("")
    lines.append("> **评分说明**: 权威性=来源资质(官方/专业媒体/UGC), 精确度=搜索结果中相关占比, "
                 "时效性=信息更新速度, 独特价值=其他来源未覆盖的独家内容")
    lines.append("")

    # ─── KPI Signals ───
    signals = extract_kpi_signals(new_items)
    if any(signals.values()):
        lines.append("## ⚡ KPI 信号警报")
        lines.append("")
        if signals["gross_margin"]:
            lines.append(f"**🔴 毛利率相关**: {len(signals['gross_margin'])} 条 - "
                         "建议检查是否需要更新 KPI 基线 (`kpi update`)")
            for it in signals["gross_margin"][:3]:
                lines.append(f"  - [{it.title}]({it.url})")
        if signals["volume_asp"]:
            lines.append(f"**🟠 出货量/ASP**: {len(signals['volume_asp'])} 条")
            for it in signals["volume_asp"][:2]:
                lines.append(f"  - [{it.title}]({it.url})")
        if signals["opex"]:
            lines.append(f"**🟡 费用结构**: {len(signals['opex'])} 条")
            for it in signals["opex"][:2]:
                lines.append(f"  - [{it.title}]({it.url})")
        lines.append("")

    # ─── KPI Baseline ───
    kpi = load_kpi_baseline()
    lines.append("## 📊 当前 KPI 基线")
    lines.append("")
    gm_o = kpi.get("gm_overall")
    gm_h = kpi.get("gm_hardware")
    gm_s = kpi.get("gm_software")
    if all(v is not None for v in [gm_o, gm_h, gm_s]):
        hw_healthy, hw_danger, _ = KPI_THRESHOLDS["gm_hardware"]
        hw_status = score_kpi(gm_h, hw_healthy, hw_danger, "higher_better")
        emoji = {"GREEN": "🟢", "YELLOW": "🟡", "RED": "🔴", "GRAY": "⚪"}[hw_status]
        lines.append(f"基线期: **{kpi.get('as_of', '?')}**　|　"
                     f"综合 GM: {gm_o*100:.1f}%　|　"
                     f"{emoji} **硬件 GM: {gm_h*100:.1f}%** (关键)　|　"
                     f"软件 GM: {gm_s*100:.1f}%")
    lines.append("")
    lines.append("> 详细 KPI 仪表盘见 `kpi_dashboard.md` (运行 `kpi dashboard` 更新)")
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
    src_emoji = SOURCE_EMOJI.get(it.source, "📌")
    date_short = it.date[:10] if it.date else "???"

    lines = [f"### {src_emoji} {it.title}"]
    lines.append("")
    meta = [f"`{date_short}`", f"`{it.source}`"]
    if it.category:
        meta.append(f"_{it.category}_")
    if it.tags:
        meta.append("`" + " ".join(it.tags) + "`")
    # Source reliability score indicator
    score_bar = "█" * it.source_score + "░" * (5 - it.source_score)
    meta.append(f"来源可靠度: `{score_bar}` {it.source_score}/5")
    lines.append("　·　".join(meta))
    lines.append("")
    if it.summary:
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

    try:
        r = requests.get(
            "https://www1.hkexnews.hk/search/titlesearch.xhtml",
            params={"lang": "EN"},
            headers=HEADERS, timeout=TIMEOUT,
        )
        status = "✅ OK" if r.status_code == 200 else f"⚠️ HTTP {r.status_code}"
        has_vs = "javax.faces.ViewState" in r.text
        detail = f"{len(r.text)} bytes"
        if has_vs:
            detail += ", ViewState found"
        else:
            detail += ", NO ViewState"
            status = "⚠️ NO_VS" if r.status_code == 200 else status
        results.append(("HKEX (page)", status, detail))
    except Exception as e:
        results.append(("HKEX (page)", "❌ FAIL", str(e)))

    if HAS_DDGS:
        test_sites = [
            ("36kr", "36Kr"),
            ("sohu.com", "SOHU"),
            ("iyiou.com", "IYIOU"),
            ("news.qq.com", "QQ News"),
            ("wallstreetcn.com", "WSCN"),
            ("caixin.com", "CAIXIN"),
        ]
        for site, label in test_sites:
            try:
                with DDGS() as ddgs:
                    test_results = list(ddgs.text(
                        f"地平线机器人 site:{site}", max_results=3))
                n = len(test_results)
                status = "✅ OK" if n > 0 else "⚠️ EMPTY"
                results.append((f"DDG→{label}", status, f"{n} results"))
            except Exception as e:
                results.append((f"DDG→{label}", "❌ FAIL", str(e)))
    else:
        results.append(("DDG Search", "❌ NO_LIB", "duckduckgo_search not installed"))

    print(f"{'Source':<16} {'Status':<14} {'Detail'}")
    print("-" * 60)
    for src, status, detail in results:
        print(f"{src:<16} {status:<14} {detail}")
    print()
    return 0


def cmd_diagnose() -> int:
    """Source reliability audit — test each source and report quality metrics."""
    print("\n=== 来源可靠性诊断 ===\n")
    print("测试每个来源的搜索命中率、相关性和信号质量...\n")

    if not HAS_DDGS:
        print("❌ duckduckgo_search 未安装, 无法诊断")
        return 1

    report: list[dict] = []

    test_sources = [
        ("36kr.com", "36KR"),
        ("sohu.com", "SOHU"),
        ("iyiou.com", "IYIOU"),
        ("news.qq.com", "QQNEWS"),
        ("wallstreetcn.com", "WSCN"),
        ("caixin.com", "CAIXIN"),
    ]

    for site, src_key in test_sources:
        profile = SOURCE_PROFILES.get(src_key)
        print(f"  [{src_key}] {profile.label if profile else src_key}...")
        try:
            all_results: list[dict] = []
            with DDGS() as ddgs:
                for kw in KEYWORDS_CN[:2]:
                    query = f"site:{site} {kw}"
                    results = list(ddgs.text(query, max_results=10))
                    all_results.extend(results)
                    time.sleep(0.5)

            total = len(all_results)
            relevant = sum(1 for r in all_results
                          if _matches_company(r.get("title", "")))
            on_site = sum(1 for r in all_results
                         if site in (r.get("href", "") or ""))

            actual_precision = relevant / max(total, 1)
            signal_count = sum(
                1 for r in all_results
                if _matches_company(r.get("title", ""))
                and score_news(r.get("title", ""), r.get("body", ""))[0] >= 4
            )

            print(f"    总结果: {total} | 相关: {relevant} | "
                  f"本站: {on_site} | 高信号: {signal_count} | "
                  f"精确度: {actual_precision*100:.0f}%")

            report.append({
                "source": src_key,
                "label": profile.label if profile else src_key,
                "total": total,
                "relevant": relevant,
                "on_site": on_site,
                "high_signal": signal_count,
                "precision": actual_precision,
                "base_score": profile.base_score if profile else 3,
            })
        except Exception as e:
            print(f"    ❌ FAILED: {e}")
            report.append({
                "source": src_key,
                "label": profile.label if profile else src_key,
                "total": 0, "relevant": 0, "on_site": 0,
                "high_signal": 0, "precision": 0,
                "base_score": 0, "error": str(e),
            })

    # ─── Summary ───
    print("\n" + "=" * 60)
    print("来源可靠性排名\n")
    ranked = sorted(report, key=lambda x: (
        x["high_signal"] * 2 + x["relevant"],
        -abs(x["precision"] - (x.get("base_score", 3) / 5)),
    ), reverse=True)

    print(f"{'排名':<4} {'来源':<12} {'产出':<6} {'高信号':<6} {'精确度':<8} {'建议':<20}")
    print("-" * 60)
    for i, r in enumerate(ranked, 1):
        rec = "✅ 保留" if r["high_signal"] >= 2 or r["relevant"] >= 5 else (
            "⚠️ 降权" if r["relevant"] >= 2 else "❌ 考虑移除"
        )
        if r.get("error"):
            rec = "🔧 需修复"
        print(f"{i:<4} {r['source']:<12} {r['relevant']:<6} "
              f"{r['high_signal']:<6} {r['precision']*100:.0f}%{'':<4} {rec}")

    print("\n诊断完成。根据结果可调整 SOURCE_PROFILES 中的配置。")
    print("运行 `python3 horizon_monitor.py --reset --since 180` 进行正式监测。\n")
    return 0


def cmd_run(args) -> int:
    if args.reset and STATE_FILE.exists():
        STATE_FILE.unlink()
        print("[state] cleared")

    seen = load_state()
    print(f"[start] lookback={args.since}d　seen={len(seen)}")

    all_items: list[Item] = []

    def _safe_fetch(label: str, fn, *fargs, **fkwargs) -> list[Item]:
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
        print("[2] 36氪 (via DDG)…")
        kr = _safe_fetch("36KR", _fetch_news_ddg,
                         "36kr.com", "36KR", 15)
        print(f"    → {len(kr)} 条")
        all_items.extend(kr)
        time.sleep(1)

        print("[3] 搜狐 (via DDG)…")
        sohu_items = _safe_fetch("SOHU", fetch_sohu, 15)
        print(f"    → {len(sohu_items)} 条")
        all_items.extend(sohu_items)
        time.sleep(1)

        print("[4] 亿欧 (via DDG)…")
        iyiou_items = _safe_fetch("IYIOU", fetch_iyiou, 15)
        print(f"    → {len(iyiou_items)} 条")
        all_items.extend(iyiou_items)
        time.sleep(1)

        print("[5] 腾讯新闻 (via DDG)…")
        qq_items = _safe_fetch("QQNEWS", fetch_qqnews, 10)
        print(f"    → {len(qq_items)} 条")
        all_items.extend(qq_items)
        time.sleep(1)

        print("[6] 华尔街见闻 (via DDG)…")
        wscn_items = _safe_fetch("WSCN", _fetch_news_ddg,
                                 "wallstreetcn.com", "WSCN", 10)
        print(f"    → {len(wscn_items)} 条")
        all_items.extend(wscn_items)
        time.sleep(1)

        print("[7] 财新网 (via DDG)…")
        cx = _safe_fetch("CAIXIN", _fetch_news_ddg,
                         "caixin.com", "CAIXIN", 10)
        print(f"    → {len(cx)} 条")
        all_items.extend(cx)

    all_items = _dedupe(all_items)
    # Assign source reliability scores
    for it in all_items:
        it.source_score = score_item_source(it)
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
        description="Horizon Robotics 9660.HK 信息源监测 + KPI 跟踪",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples (news/filings monitoring):\n"
            "  python3 horizon_monitor.py --test\n"
            "  python3 horizon_monitor.py --diagnose\n"
            "  python3 horizon_monitor.py --since 365 --json\n"
            "  python3 horizon_monitor.py --no-news --reset\n"
            "\n"
            "Examples (KPI tracking):\n"
            "  python3 horizon_monitor.py kpi show           # 查看当前 KPI 基线\n"
            "  python3 horizon_monitor.py kpi update         # 新财报后录入\n"
            "  python3 horizon_monitor.py kpi dashboard      # 生成 kpi_dashboard.md\n"
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
    ap.add_argument("--out-kpi", default="kpi_dashboard.md",
                    help="KPI dashboard output path (default: kpi_dashboard.md)")
    ap.add_argument("--test", action="store_true",
                    help="connectivity test only, no scraping")
    ap.add_argument("--diagnose", action="store_true",
                    help="source reliability audit — test & rank all sources")

    ap.add_argument("kpi_cmd", nargs="?", default=None,
                    choices=[None, "kpi"],
                    help="run KPI subcommand (use 'kpi' followed by show/update/dashboard)")
    ap.add_argument("kpi_action", nargs="?", default=None,
                    choices=[None, "show", "update", "dashboard"],
                    help="KPI action when kpi_cmd='kpi'")

    args = ap.parse_args()

    if args.kpi_cmd == "kpi":
        if args.kpi_action == "show":
            return cmd_kpi_show()
        if args.kpi_action == "update":
            return cmd_kpi_update()
        if args.kpi_action == "dashboard":
            return cmd_kpi_dashboard(args)
        print("ERROR: kpi requires an action: show | update | dashboard", file=sys.stderr)
        return 2

    if args.test:
        return cmd_test()
    if args.diagnose:
        return cmd_diagnose()
    result = cmd_run(args)
    try:
        render_kpi_dashboard(args.out_kpi)
        print(f"[output] {args.out_kpi}")
    except Exception as e:
        print(f"[kpi-dashboard] failed: {e}", file=sys.stderr)
    return result


if __name__ == "__main__":
    sys.exit(main())