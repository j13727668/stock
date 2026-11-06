# -*- coding: utf-8 -*-
"""
collect_symbol.py — 合并采集器 v1（EDGAR 主 + 雅虎补充，每只股票一个合并小包）
================================================================================
产出（两种模式）：
  本地模式  BUILD_DIR/stocks/{SYM}/{SYM}.json      <- 你的服务器直接读，文件夹按代码分
  上传模式  R2: stocks/{SYM}.json                  <- 1 只 1 次 PUT（Class A 减半）

合并包结构：
  {
    "symbol": "AAPL",
    "updated_at": "...",
    "edgar": { company, filings, financials },   # SEC 为主
    "yahoo": { profile, financials, actions, kline_*, indicators, holders }
  }

用法（GitHub Actions 分片 / 手动单测）：
  python collect_symbol.py SHARD TOTAL          # 分片并行
  python collect_symbol.py 0 1 AAPL             # 单只调试
  python collect_symbol.py --local SHARD TOTAL  # 只落本地不传（服务器定时任务用）

环境变量：
  INGEST_URL / INGEST_SECRET    传自己服务器（PHP 接收端）
  R2_ENABLE=1 + R2_*            同时写 R2
  两个都不配时自动进入本地模式，只写 build 目录
================================================================================
"""
import json
import os
import sys
import time
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import collect_yahoo as yh          # noqa: E402
import collect_edgar as ed          # noqa: E402
from uploader import Uploader       # noqa: E402

SYMBOLS_FILE = os.path.abspath(os.path.join(BASE, "..", "SYMBOLS_ALL.txt"))
BUILD_DIR = os.path.abspath(os.path.join(BASE, "..", "build"))
REQ_GAP = 0.5                      # 每只股票之间的额外间隔


def log(msg):
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)


def collect_one(sym, cik_map):
    """EDGAR 主采集 + 雅虎补充，合并成一个包"""
    edgar_part, yahoo_part = {}, {}
    cik = cik_map.get(sym)
    if cik:
        try:
            company, filings = ed.fetch_submissions(cik)
            edgar_part = {"company": company, "filings": filings,
                          "financials": ed.fetch_financials(cik), "cik": cik}
        except Exception as e:  # noqa: BLE001
            log(f"  {sym} EDGAR 失败: {e}")
            edgar_part = {"error": str(e)[:120], "cik": cik}
    else:
        edgar_part = {"error": "no_cik"}

    try:
        y = yh.collect_symbol(sym)
        yahoo_part = {k: v for k, v in y.items() if k not in ("symbol", "updated_at")}
    except Exception as e:  # noqa: BLE001
        log(f"  {sym} 雅虎失败: {e}")
        yahoo_part = {"error": str(e)[:120]}

    return {"symbol": sym,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "edgar": edgar_part,
            "yahoo": yahoo_part}


def save_local(sym, bundle):
    """本地落盘：build/stocks/AAPL/AAPL.json（原子写）"""
    d = os.path.join(BUILD_DIR, "stocks", sym)
    os.makedirs(d, exist_ok=True)
    full = os.path.join(d, f"{sym}.json")
    tmp = full + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(bundle, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, full)
    return full


def main():
    args = sys.argv[1:]
    local_only = False
    if args and args[0] == "--local":
        local_only = True
        args = args[1:]

    shard, total, only = 0, 1, None
    if args and args[0].isdigit() and len(args) >= 2 and args[1].isdigit():
        shard, total = int(args[0]), int(args[1])
        if len(args) > 2:
            only = args[2].upper()
    elif args:
        only = args[0].upper()

    with open(SYMBOLS_FILE) as f:
        symbols = [ln.strip() for ln in f if ln.strip()]
    if only:
        symbols = [only]
    elif total > 1:
        symbols = symbols[shard::total]

    cik_map = ed.load_cik_map()
    up = None
    if not local_only:
        try:
            up = Uploader()
        except Exception as e:  # noqa: BLE001
            log(f"上传层未配置，转本地模式: {e}")
            local_only = True

    log(f"分片 {shard}/{total}，本片 {len(symbols)} 只，模式={'本地' if local_only else '上传'}")
    ok, failed = 0, []
    for i, sym in enumerate(symbols, 1):
        try:
            bundle = collect_one(sym, cik_map)
            body = json.dumps(bundle, ensure_ascii=False,
                              separators=(",", ":")).encode()
            save_local(sym, bundle)                     # 始终落本地（便于打包归档）
            if not local_only:
                up.put(f"stocks/{sym}.json", body)      # 1 只 1 次 PUT
            ok += 1
            if i % 50 == 0:
                log(f"进度 {i}/{len(symbols)}，成功 {ok}")
        except Exception as e:  # noqa: BLE001
            failed.append(sym)
            log(f"{sym} 失败: {e}")
        time.sleep(REQ_GAP)
    log(f"=== 分片 {shard} 完成: 成功 {ok}, 失败 {len(failed)} ===")
    if failed:
        with open(os.path.join(BASE, "..", f"failed_{shard}.txt"), "w") as f:
            f.write("\n".join(failed))


if __name__ == "__main__":
    main()
