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
# KPI TRACKING - 核心财务指标跟踪
# ============================================================
#
# This module tracks the financial KPIs that matter most for thesis validation:
#
#   1. Gross Margin (overall) - the headline figure
#   2. Gross Margin - Hardware (Product Solutions) - the most-watched
#   3. Gross Margin - Software (License & Services) - moat indicator
#   4. High-end shipment mix % - drives ASP uplift
#   5. R&D as % of revenue - operating leverage signal
#   6. Revenue YoY growth
#   7. Adjusted operating loss (excludes fair value of preferred shares)
#
# The baseline is hardcoded with FY2025 actuals. After each new H1/annual
# report, run `python3 horizon_monitor.py kpi update` to enter new numbers
# interactively. The script compares to baseline and flags any KPI that
# crosses a warning threshold.

# Baseline KPI snapshot - update after each new report
# Last updated: 2026-03 (FY2025 annual report)
KPI_BASELINE = {
    "as_of": "2025FY",
    "report_date": "2026-03-19",
    "report_url": "https://stockn.xueqiu.com/09660/20260319282482.pdf",
    # Income statement
    "revenue_cny_mm": 3758,
    "revenue_yoy": 0.577,
    "gm_overall": 0.645,       # 综合毛利率
    "gm_hardware": 0.345,      # 汽车产品方案毛利率 - 硬件
    "gm_software": 0.93,       # 汽车授权服务毛利率 - 软件
    "gm_nonauto": 0.50,        # 非汽车业务毛利率（估算）
    "rd_pct_revenue": 1.371,   # 研发费用率 137.1%
    "adj_op_loss_cny_mm": -2372,
    # Volume / mix
    "shipments_units_mm": 4.01,           # 全年出货 401 万套
    "highend_mix": 0.45,                  # 中高阶占比
    "highend_shipments_units_mm": 1.80,   # 中高阶 180 万套
    "cumulative_shipments_units_mm": 11.7,  # 累计 1170 万套
    # Mix
    "revenue_mix_product": 0.43,          # 硬件占比
    "revenue_mix_license": 0.52,          # 软件占比
    "revenue_mix_nonauto": 0.05,
    # Market share (China自主品牌)
    "market_share_l2_adas": 0.477,        # L2 ADAS 市占率
    "market_share_city_noa": 0.144,       # 城区 NOA 市占率
    # HSD
    "hsd_design_wins": 20,                # HSD 累计定点车型数
    "hsd_deployed_units": 22000,          # HSD 上市后已交付套数
}

# Health thresholds - what's a healthy / warning / danger reading
# Each tuple: (healthy_value, danger_value, direction)
#   direction = "higher_better" or "lower_better"
#
# CRITICAL: GM thresholds calibrated to current trajectory.
# Hardware GM is THE most-watched metric per analysis of FY2025 report.
KPI_THRESHOLDS = {
    "gm_overall":         (0.65,  0.55,  "higher_better"),  # 综合毛利率
    "gm_hardware":        (0.38,  0.28,  "higher_better"),  # 硬件毛利率 (key tracker)
    "gm_software":        (0.92,  0.85,  "higher_better"),  # 软件毛利率 (moat)
    "revenue_yoy":        (0.50,  0.25,  "higher_better"),  # 营收增速
    "highend_mix":        (0.55,  0.40,  "higher_better"),  # 中高阶占比
    "rd_pct_revenue":     (1.00,  1.50,  "lower_better"),   # R&D 费率（降才好）
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
    """Load saved KPI baseline if exists, else return hardcoded default."""
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
    """Return status: GREEN / YELLOW / RED based on value vs thresholds."""
    if value is None:
        return "GRAY"
    if direction == "higher_better":
        if value >= healthy:
            return "GREEN"
        if value <= danger:
            return "RED"
        return "YELLOW"
    else:  # lower_better
        if value <= healthy:
            return "GREEN"
        if value >= danger:
            return "RED"
        return "YELLOW"


# ============================================================
# KPI SIGNAL EXTRACTION FROM NEWS
# ============================================================
#
# Scan news titles + summaries for mentions of key KPIs.
# We're NOT extracting numbers (too unreliable from short snippets).
# Instead, we flag articles likely to update our KPI view.

GM_KEYWORDS = [
    # Chinese
    "毛利率", "毛利", "gross margin", "gm", "毛利率下滑", "毛利率提升",
    "硬件毛利", "软件毛利", "产品毛利", "授权毛利",
    "结构性下滑", "业务组合", "收入组合",
    # English
    "gross margin", "margin compression", "margin expansion", "blended margin",
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
    """
    Scan recent news for mentions of KPI-relevant keywords.
    Returns a dict of {kpi_category: [matching items]}.
    """
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
    """Write the KPI dashboard markdown file."""
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

        # Diagnostic
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
        for snap in history[-10:]:  # last 10
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
    """Print current KPI baseline."""
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
    """Interactive KPI input. Prompts user for each field."""
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
            elif is_pct and abs(v) > 5:  # likely entered as percent without %
                print(f"    (interpreting {v} as {v}%)")
                v = v / 100
            return v
        except ValueError:
            print(f"    ⚠ Invalid input '{s}', keeping {cur_str}")
            return cur

    # Period meta
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

    # Diff before save
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

    # Save old baseline to history first
    append_kpi_history(current)
    save_kpi_baseline(new_kpi)

    # Now compare to new thresholds and warn
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
    """Generate the KPI dashboard markdown file."""
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

    # 5 — material event (earnings, gross margin specifically, etc.)
    # NOTE: 毛利率 elevated to importance 5 because it's our core tracker.
    # Mentions of "毛利率下滑", "硬件毛利", etc. are MUST-READs.
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
        # NEW: KPI-relevant secondary signals
        "中高阶": "highend_mix", "占比": "mix_signal",
        "ASP": "asp", "单价": "asp", "平均售价": "asp",
        "研发费用": "rd_spend", "费用率": "opex_ratio",
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

    # === KPI SIGNAL BANNER ===
    # Highlight any new items that touch our core KPIs (gross margin, etc.)
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

    # KPI baseline summary
    kpi = load_kpi_baseline()
    lines.append("## 📊 当前 KPI 基线")
    lines.append("")
    gm_o = kpi.get("gm_overall")
    gm_h = kpi.get("gm_hardware")
    gm_s = kpi.get("gm_software")
    if all(v is not None for v in [gm_o, gm_h, gm_s]):
        # Color code based on hardware GM (the key metric)
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
        description="Horizon Robotics 9660.HK 信息源监测 + KPI 跟踪",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples (news/filings monitoring):\n"
            "  python3 horizon_monitor.py --test\n"
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

    # KPI subcommand (positional, optional)
    ap.add_argument("kpi_cmd", nargs="?", default=None,
                    choices=[None, "kpi"],
                    help="run KPI subcommand (use 'kpi' followed by show/update/dashboard)")
    ap.add_argument("kpi_action", nargs="?", default=None,
                    choices=[None, "show", "update", "dashboard"],
                    help="KPI action when kpi_cmd='kpi'")

    args = ap.parse_args()

    # KPI subcommand dispatch
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
    result = cmd_run(args)
    # Also regenerate the KPI dashboard each run (so it stays fresh)
    try:
        render_kpi_dashboard(args.out_kpi)
        print(f"[output] {args.out_kpi}")
    except Exception as e:
        print(f"[kpi-dashboard] failed: {e}", file=sys.stderr)
    return result


if __name__ == "__main__":
    sys.exit(main())
