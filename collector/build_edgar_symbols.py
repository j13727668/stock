# -*- coding: utf-8 -*-
"""
build_edgar_symbols.py — 只生成 EDGAR 全量代码清单（不传 meta，避免覆盖线上 universe）
输出：SYMBOLS_ALL.txt
"""
import requests

UA = {"User-Agent": "StockMonitor admin@ushy365.com"}
r = requests.get("https://www.sec.gov/files/company_tickers.json",
                 headers=UA, timeout=60)
syms = sorted({it["ticker"].upper() for it in r.json().values() if it.get("ticker")})
with open("SYMBOLS_ALL.txt", "w") as f:
    f.write("\n".join(syms))
print(f"EDGAR symbols: {len(syms)}")
