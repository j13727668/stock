# -*- coding: utf-8 -*-
"""
fetch_universe.py — 全市场股票清单生成器
从 SEC EDGAR 官方映射拉全部 ticker（约 10000+，覆盖主板+部分OTC），
并叠加 NASDAQ 官方 symbol 目录（nasdaqlisted + otherlisted，补充 ETF 等），
产出 SYMBOLS_ALL.txt（每行一个代码）+ meta/universe.json（上传 R2 供前端用）。
用法: python fetch_universe.py   （任何 job 开始前先跑一次）
"""
import json
import os
import sys

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from uploader import Uploader

BASE = os.path.dirname(os.path.abspath(__file__))
ALL_FILE = os.path.join(BASE, "..", "SYMBOLS_ALL.txt")

UA = {"User-Agent": "StockMonitor admin@ushy365.com"}
NASDAQ_URLS = [
    "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt",
    "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt",
]


def _nasdaq_symbols():
    out = set()
    for url in NASDAQ_URLS:
        try:
            r = requests.get(url, headers=UA, timeout=30)
            lines = r.text.strip().splitlines()
            hdr = lines[0].split("|")
            for ln in lines[1:]:
                p = dict(zip(hdr, ln.split("|")))
                sym = (p.get("Symbol") or p.get("ACT Symbol") or "").strip()
                flag = (p.get("Test Issue") or p.get("ETF Flag") or "N")
                if sym and p.get("Symbol", p.get("ACT Symbol")):
                    if p.get("Test Issue") == "Y":
                        continue
                    # 跳过带 $ . 等特殊符号的（优先股/权证类，行情源支持差）
                    if any(ch in sym for ch in "$^+=~"):
                        continue
                    out.add(sym.upper())
        except Exception as e:  # noqa: BLE001
            print(f"NASDAQ {url} 失败: {e}")
    return out


def main():
    import yfinance  # noqa: F401  确认依赖装好
    # 1) EDGAR 全量
    r = requests.get("https://www.sec.gov/files/company_tickers.json",
                     headers=UA, timeout=60)
    items = r.json().values()
    edgar = {it["ticker"].upper(): {"cik": it["cik_str"], "name": it["title"]}
             for it in items if it.get("ticker")}
    # 2) NASDAQ 目录补充
    nas = _nasdaq_symbols()
    # 3) 合并：EDGAR 优先（带 CIK），NASDAQ 补充（ETF 等）
    uni = dict(edgar)
    extra = [s for s in nas if s not in uni and 1 <= len(s) <= 5]
    symbols = sorted(set(list(uni.keys()) + extra))
    with open(ALL_FILE, "w") as f:
        f.write("\n".join(symbols))
    # 4) universe 元数据上传 R2
    meta = {"count": len(symbols),
            "edgar_count": len(uni),
            "extra_count": len(extra),
            "generated_at": __import__("datetime").datetime.utcnow().isoformat(),
            "symbols": symbols,
            "cik": {s: uni[s]["cik"] for s in uni}}
    body = json.dumps(meta, separators=(",", ":")).encode()
    up = Uploader()
    up.put("meta/universe.json", body)
    print(f"全市场清单完成: 总 {len(symbols)} (EDGAR {len(uni)} + NASDAQ补充 {len(extra)})，已上传 meta/universe.json")


if __name__ == "__main__":
    main()
