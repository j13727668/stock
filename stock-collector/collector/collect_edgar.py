# -*- coding: utf-8 -*-
"""
collect_edgar.py — SEC EDGAR 主采集器（免费、合规 10 次/秒，不占任何行情配额）
================================================================================
每只股票产出 stocks/{SYM}/edgar.json，包含：
  company    公司注册资料（全名/交易所/行业SIC/州/电话）
  filings    最近 20 份公告（form/日期/原文链接），8-K 标 is_major
  financials XBRL 关键科目：营收/净利/EPS/总资产/净资产/经营现金流/毛利
             （年报取最近一份 10-K，季报取最近一份 10-Q）

全市场约 13000 只 × 2 请求（submissions + companyfacts）÷ 0.11s 间隔
≈ 50 分钟单进程；workflow 里开 3 个分片并行 ≈ 20 分钟跑完。

用法: python collect_edgar.py SHARD TOTAL
限速：0.11s/次（低于 SEC 10 次/秒红线）
================================================================================
"""
import json
import os
import sys
import time
from datetime import datetime, timezone

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from uploader import Uploader

BASE = os.path.dirname(os.path.abspath(__file__))
SYMBOLS_FILE = os.path.join(BASE, "..", "SYMBOLS_ALL.txt")

UA = {"User-Agent": "StockMonitor admin@ushy365.com",
      "Accept-Encoding": "gzip, deflate"}
SUB_URL = "https://data.sec.gov/submissions/CIK{:0>10}.json"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{:0>10}.json"
REQ_GAP = 0.11
MAJOR_FORMS = {"8-K", "10-K", "10-Q", "S-1", "S-3", "424B5", "DEF 14A", "25-NSE"}

# XBRL 关键科目（us-gaap 标签 -> 输出名）
CONCEPTS = {
    "Revenues": "revenues",
    "RevenueFromContractWithCustomerExcludingAssessedTax": "revenues_asc606",
    "NetIncomeLoss": "net_income",
    "EarningsPerShareDiluted": "eps_diluted",
    "EarningsPerShareBasic": "eps_basic",
    "GrossProfit": "gross_profit",
    "OperatingIncomeLoss": "operating_income",
    "Assets": "total_assets",
    "StockholdersEquity": "equity",
    "CashAndCashEquivalentsAtCarryingValue": "cash",
    "NetCashProvidedByUsedInOperatingActivities": "cash_flow_ops",
}
_session = requests.Session()
_session.proxies.update({"http": None, "https": None})


def log(msg):
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)


def load_cik_map():
    """SYMBOL -> cik（用 EDGAR 全量映射，本地先尝试缓存的 universe 元数据）"""
    r = _session.get("https://www.sec.gov/files/company_tickers.json",
                     headers=UA, timeout=60)
    items = r.json().values()
    return {it["ticker"].upper(): str(it["cik_str"])
            for it in items if it.get("ticker")}


def _get(url, tries=3):
    """带 429/503 退避的 GET；SEC 限速时自动等待重试"""
    for i in range(tries):
        time.sleep(REQ_GAP)
        try:
            r = _session.get(url, headers=UA, timeout=60)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (429, 503):
                wait = 5 * (i + 1)
                log(f"  {r.status_code} 限速，等待 {wait}s 重试")
                time.sleep(wait)
                continue
            log(f"  HTTP {r.status_code}: {url[-40:]}")
            return None
        except Exception as e:  # noqa: BLE001
            log(f"  异常({i+1}/{tries}): {e}")
            time.sleep(3)
    return None


def fetch_submissions(cik):
    j = _get(SUB_URL.format(cik))
    if not j:
        return {}, []
    fil = j.get("filings", {}).get("recent", {})
    company = {k: j.get(k) for k in ("name", "tickers", "exchanges",
                                     "sicDescription", "stateOfIncorporation",
                                     "phone", "ein")}
    out = []
    for i in range(min(20, len(fil.get("form", [])))):
        form = fil["form"][i]
        acc = (fil.get("accessionNumber", [""] * 20)[i] or "").replace("-", "")
        doc = fil.get("primaryDocument", [""] * 20)[i] or ""
        out.append({
            "form": form, "is_major": 1 if form in MAJOR_FORMS else 0,
            "filed": fil.get("filingDate", [""] * 20)[i],
            "title": (fil.get("items", [""] * 20)[i]
                      or fil.get("primaryDocDescription", [""] * 20)[i] or ""),
            "url": (f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc}/{doc}"
                    if acc and doc else None),
        })
    return company, out


def _latest(entries, form):
    """取指定表单里报告期(end)最新的一条"""
    best = None
    for e in entries:
        if e.get("form") == form and e.get("end"):
            if best is None or e["end"] > best["end"]:
                best = e
    if not best:
        return None
    return {"end": best["end"], "val": best.get("val"),
            "fy": best.get("fy"), "fp": best.get("fp")}


def fetch_financials(cik):
    j = _get(FACTS_URL.format(cik))
    if not j:
        return {}
    gaap = (j.get("facts", {}).get("us-gaap") or {})
    out = {}
    for tag, name in CONCEPTS.items():
        node = gaap.get(tag)
        if not node:
            continue
        units = node.get("units", {}).get("USD") or \
                node.get("units", {}).get("USD/shares") or []
        out[name] = {"annual": _latest(units, "10-K"),
                     "quarterly": _latest(units, "10-Q")}
    return out


def collect_one(sym, cik):
    company, filings = fetch_submissions(cik)
    fin = fetch_financials(cik)
    return {"symbol": sym, "cik": cik, "company": company,
            "filings": filings, "financials": fin,
            "updated_at": datetime.now(timezone.utc).isoformat()}


def main():
    shard, total = (int(sys.argv[1]), int(sys.argv[2])) if len(sys.argv) > 1 else (0, 1)
    with open(SYMBOLS_FILE) as f:
        symbols = [ln.strip() for ln in f if ln.strip()]
    mine = symbols[shard::total]
    log(f"EDGAR 分片 {shard}/{total}，本片 {len(mine)} 只")
    cik_map = load_cik_map()
    up = Uploader()
    ok, failed = 0, []
    for i, sym in enumerate(mine, 1):
        cik = cik_map.get(sym)
        if not cik:
            failed.append(sym)
            continue
        try:
            data = collect_one(sym, cik)
            body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()
            up.put(f"stocks/{sym}/edgar.json", body)
            ok += 1
            if i % 200 == 0:
                log(f"进度 {i}/{len(mine)}，成功 {ok}")
        except Exception as e:  # noqa: BLE001
            failed.append(sym)
            log(f"{sym} 失败: {e}")
    log(f"=== EDGAR 分片 {shard} 完成: 成功 {ok}, 失败 {len(failed)} ===")
    if failed:
        with open(os.path.join(BASE, "..", f"failed_edgar_{shard}.txt"), "w") as f:
            f.write("\n".join(failed))


if __name__ == "__main__":
    main()
