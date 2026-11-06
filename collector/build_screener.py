# -*- coding: utf-8 -*-
"""
build_screener.py — 生成全市场筛选索引 meta/screener.json
================================================================================
从本地采集好的 build/stocks/*/{SYM}.json 里抽取每只股票的关键指标，
汇总成一个扁平数组，前端一次加载即可在浏览器内存里做筛选/排序（零请求、毫秒级）。

输出示例：
  [
    {"s":"AAPL","n":"Apple Inc.","px":232.1,"pct":1.23,"mcap":3510000000000,
     "pe":35.2,"sec":"Technology","ind":"Consumer Electronics",
     "hi52":260.1,"lo52":164.1,"dv":0.44,"ex":"NASDAQ","pf":1,
     "f8k":2,"fd":"2026-11-02"},
    ...
  ]
字段说明（短键，省体积约 40%）：
  s=symbol  n=名称  px=现价  pct=涨跌幅%  mcap=市值  pe=市盈率
  sec=板块  ind=行业  hi52/lo52=52周高/低  dv=股息率%  ex=交易所
  pf=资料完整度(0/1)  f8k=近30天8-K数量  fd=最近公告日期

体积：16502 只 × ~150 字节 ≈ 2.5MB（gzip 后 ~500KB）

用法：
  python build_screener.py                 # 本地生成 build/meta/screener.json
  python build_screener.py --upload        # 生成并上传 R2 meta/screener.json
  python build_screener.py --top 300       # 只保留市值前 300（体积再降）

环境变量同 uploader.py：R2_ENABLE=1 + R2_*  或 INGEST_URL + INGEST_SECRET
================================================================================
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

BUILD_STOCKS = os.path.abspath(os.path.join(BASE, "..", "build", "stocks"))
BUILD_META = os.path.abspath(os.path.join(BASE, "..", "build", "meta"))
OUT_FILE = os.path.join(BUILD_META, "screener.json")


def _num(v):
    try:
        if v is None:
            return None
        f = float(v)
        return round(f, 4) if abs(f) < 1e6 else round(f, 0)
    except (TypeError, ValueError):
        return None


def _recent_8k(filings, days=30):
    """统计近 N 天的 8-K 数量 + 最近公告日期"""
    if not filings:
        return 0, None
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    n8k, latest = 0, None
    for f in filings:
        fd = f.get("filed") or ""
        if not fd:
            continue
        if latest is None or fd > latest:
            latest = fd
        if f.get("form") == "8-K" and fd >= cutoff:
            n8k += 1
    return n8k, latest


def _pct_change(kline_daily):
    """用最近两根日K算涨跌幅"""
    if not kline_daily or len(kline_daily) < 2:
        return None
    try:
        last = kline_daily[-1].get("Close")
        prev = kline_daily[-2].get("Close")
        if last is None or not prev:
            return None
        return round((float(last) - float(prev)) / float(prev) * 100, 3)
    except Exception:  # noqa: BLE001
        return None


def extract(sym, bundle):
    """从合并包里抽出筛选字段；数据缺失的写 None，前端过滤时跳过即可"""
    edgar = bundle.get("edgar") or {}
    yahoo = bundle.get("yahoo") or {}
    prof = yahoo.get("profile") or {}
    comp = edgar.get("company") or {}

    daily = yahoo.get("kline_daily") or []
    px = _num(prof.get("currentPrice"))
    if px is None and daily:
        px = _num(daily[-1].get("Close"))

    pct = _pct_change(daily)
    n8k, latest_filing = _recent_8k(edgar.get("filings") or [])

    # 股息率：最近一年分红总额 / 现价
    dv = None
    try:
        divs = (yahoo.get("actions") or {}).get("dividends") or []
        if divs and px:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=365)).strftime("%Y-%m-%d")
            total = sum(float(d["amount"]) for d in divs
                        if (d.get("date") or "") >= cutoff)
            if total > 0:
                dv = round(total / float(px) * 100, 3)
    except Exception:  # noqa: BLE001
        pass

    return {
        "s": sym,
        "n": (prof.get("shortName") or comp.get("name") or "")[:40],
        "px": px,
        "pct": pct,
        "mcap": _num(prof.get("marketCap")),
        "pe": _num(prof.get("trailingPE")),
        "sec": (prof.get("sector") or comp.get("sicDescription") or "")[:30],
        "ind": (prof.get("industry") or "")[:40],
        "hi52": _num(prof.get("fiftyTwoWeekHigh")),
        "lo52": _num(prof.get("fiftyTwoWeekLow")),
        "dv": dv,
        "ex": (prof.get("exchange")
               or (comp.get("exchanges") or [None])[0] or "")[:10],
        "pf": 1 if (prof or comp) else 0,
        "f8k": n8k,
        "fd": latest_filing,
    }


def main():
    upload = "--upload" in sys.argv
    top = 0
    if "--top" in sys.argv:
        try:
            top = int(sys.argv[sys.argv.index("--top") + 1])
        except Exception:  # noqa: BLE001
            pass

    if not os.path.isdir(BUILD_STOCKS):
        print(f"没有可处理的数据目录: {BUILD_STOCKS}")
        return

    rows, skipped = [], 0
    syms = sorted(d for d in os.listdir(BUILD_STOCKS)
                  if os.path.isdir(os.path.join(BUILD_STOCKS, d)))
    for sym in syms:
        p = os.path.join(BUILD_STOCKS, sym, f"{sym}.json")
        if not os.path.exists(p):
            skipped += 1
            continue
        try:
            with open(p, "r", encoding="utf-8") as f:
                bundle = json.load(f)
        except Exception:  # noqa: BLE001
            skipped += 1
            continue
        rows.append(extract(sym, bundle))

    # 按市值降序，市值缺失排最后
    rows.sort(key=lambda r: (r["mcap"] is None, -(r["mcap"] or 0)))
    if top:
        rows = rows[:top]

    os.makedirs(BUILD_META, exist_ok=True)
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(rows),
        "fields": "s=symbol n=name px=price pct=chg% mcap=mktcap pe=pe "
                  "sec=sector ind=industry hi52/lo52 dv=div% ex=exchange "
                  "pf=has_profile f8k=8k_30d fd=last_filing",
        "items": rows,
    }
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    tmp = OUT_FILE + ".tmp"
    with open(tmp, "wb") as f:
        f.write(body)
    os.replace(tmp, OUT_FILE)
    print(f"筛选索引: {len(rows)} 只，{len(body)/1048576:.2f} MB -> {OUT_FILE}"
          f"（跳过 {skipped}）")

    if upload:
        from uploader import Uploader
        up = Uploader()
        up.put("meta/screener.json", body)
        print("已上传 meta/screener.json")


if __name__ == "__main__":
    main()
