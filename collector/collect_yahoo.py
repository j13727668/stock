# -*- coding: utf-8 -*-
"""
collect.py — GitHub Actions 采集器（yfinance 免费源，按个股打包成 bundle.json 存 R2）
================================================================================
数据源：yfinance（雅虎财经免费数据），无需任何 API Key
每只股票产出一个 bundle.json，包含：
  profile      公司基础资料（名称/行业/市值/简介/官网/员工数）
  financials   季度利润表/资产负债表/现金流（近 4~5 季）
  actions      分红历史 + 拆股历史（公司行动）
  kline_1m     1 分钟线（近 7 天，yf 免费源上限）
  kline_daily  日 K（近 2 年，前复权）
  kline_weekly 周 K（近 5 年）
  kline_monthly 月 K（全部）
  indicators   MA5/10/20/60、BOLL(20,2)、MACD(12,26,9)（按日K本地计算）
  holders      机构持股（institutional_holders + major_holders）
R2 布局： stocks/{SYMBOL}/bundle.json
上传： boto3 走 R2 的 S3 兼容 API（GitHub Secrets 提供密钥）

GitHub Secrets 需配置：
  R2_ACCOUNT_ID        Cloudflare 账户 ID
  R2_ACCESS_KEY_ID     R2 API 令牌（访问密钥 ID）
  R2_SECRET_ACCESS_KEY R2 API 令牌（机密访问密钥）
  R2_BUCKET            桶名，如 stock-data
================================================================================
"""
import io
import json
import math
import os
import sys
import time
from datetime import date, datetime, timezone

import numpy as np
import pandas as pd
import yfinance as yf

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from uploader import Uploader

# ==================== 配置 ====================
SYMBOLS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "SYMBOLS_ALL.txt")
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "out")

REQ_GAP = 1.0          # 每只股票请求间隔（雅虎没有明确红线，0.8s 稳妥）
FETCH_1M = True        # 是否抓 1 分线（近 7 天）
FAIL_DIR_NAME = "failed"

# ==================== 工具 ====================
def _clean(obj):
    """把 pandas / numpy 类型转成 JSON 安全类型（否则 json.dumps 会抛
    'Object of type Timestamp is not JSON serializable'）

    规则：
      pd.Timestamp / datetime -> "YYYY-MM-DD HH:MM:SS"；零点整则只留 "YYYY-MM-DD"
      datetime.date          -> "YYYY-MM-DD"
      numpy int/float/bool   -> 对应 python 原生类型
      NaN / Inf / NaT / NA   -> None
    """
    # ---- 空值优先（pd.NaT 也是 datetime 子类，必须先拦）----
    if obj is None or obj is pd.NaT or obj is pd.NA:
        return None
    # ---- 时间类 ----
    if isinstance(obj, (pd.Timestamp, datetime)):
        s = obj.strftime("%Y-%m-%d %H:%M:%S")
        return s[:10] if s.endswith(" 00:00:00") else s
    if isinstance(obj, date):
        return obj.strftime("%Y-%m-%d")
    # ---- numpy 标量 ----
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating):
        f = float(obj)
        return None if (math.isnan(f) or math.isinf(f)) else round(f, 6)
    if isinstance(obj, np.ndarray):
        return [_clean(v) for v in obj.tolist()]
    # ---- python 原生 ----
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else round(obj, 6)
    if isinstance(obj, dict):
        return {(_clean(k) if not isinstance(k, str) else k): _clean(v)
                for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_clean(v) for v in obj]
    # 其它未知类型（Decimal 等）交给 json 的 default=str 兜底
    return obj


def _df_to_records(df):
    if df is None or df.empty:
        return []
    d = df.reset_index()
    # 索引统一命名为 Date（日K原为 Date，1 分线原为 Datetime，缺省为 index）
    if d.columns[0] in ("index", "level_0", "Datetime", "date"):
        d = d.rename(columns={d.columns[0]: "Date"})
    return _clean(d.to_dict(orient="records"))


def log(msg):
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)


def _retry(fn, tries=3, what=""):
    """雅虎偶发限流（429），指数退避重试"""
    last = None
    for i in range(tries):
        try:
            r = fn()
            if r is not None:
                return r
            last = "empty"
        except Exception as e:  # noqa: BLE001
            last = e
        time.sleep(3 * (i + 1))
    log(f"  重试{tries}次仍失败 {what}: {last}")
    return None


# ==================== 指标计算 ====================
def calc_indicators(daily: pd.DataFrame):
    """基于前复权日K计算 MA/BOLL/MACD，返回 list of dict"""
    if daily is None or daily.empty or len(daily) < 26:
        return []
    c = daily["Close"]
    out = pd.DataFrame(index=daily.index)
    for n in (5, 10, 20, 60):
        out[f"ma{n}"] = c.rolling(n).mean()
    mid = c.rolling(20).mean()
    std = c.rolling(20).std()
    out["boll_mid"], out["boll_up"], out["boll_low"] = mid, mid + 2 * std, mid - 2 * std
    ema12 = c.ewm(span=12, adjust=False).mean()
    ema26 = c.ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26
    dea = dif.ewm(span=9, adjust=False).mean()
    out["macd_dif"], out["macd_dea"] = dif, dea
    out["macd_hist"] = (dif - dea) * 2
    out["date"] = daily.index.strftime("%Y-%m-%d")
    recs = out.tail(120)          # 指标只存最近 120 个交易日，控制体积
    return _clean(recs.to_dict(orient="records"))


# ==================== 单只股票采集 ====================
def collect_symbol(sym):
    t = yf.Ticker(sym)
    bundle = {"symbol": sym, "updated_at": datetime.now(timezone.utc).isoformat()}

    # ---- 公司基础资料 ----
    try:
        info = _retry(lambda: t.info, what=f'{sym} info') or {}
        keep = ["shortName", "longName", "industry", "sector", "website",
                "country", "fullTimeEmployees", "marketCap", "currency",
                "exchange", "longBusinessSummary", "currentPrice",
                "fiftyTwoWeekHigh", "fiftyTwoWeekLow", "trailingPE"]
        bundle["profile"] = {k: info.get(k) for k in keep}
    except Exception as e:
        log(f"{sym} profile 失败: {e}")
        bundle["profile"] = {}

    # ---- 季度财务 ----
    fin = {}
    for key, fn_ in (("income", lambda: _retry(lambda: t.quarterly_income_stmt, what=f"{sym} income")),
                     ("balance", lambda: _retry(lambda: t.quarterly_balance_sheet, what=f"{sym} balance")),
                     ("cashflow", lambda: _retry(lambda: t.quarterly_cashflow, what=f"{sym} cashflow"))):
        try:
            df = fn_()
            # DataFrame: 行=科目 列=报告期 -> 转成 [{period, items:{}}]
            if df is not None and not df.empty:
                cols = [str(c)[:10] for c in df.columns]
                quarters = []
                for i in range(len(cols)):
                    items = _clean({str(r): df.iloc[j, i]
                                    for j, r in enumerate(df.index)})
                    quarters.append({"period": cols[i], "items": items})
                fin[key] = quarters
            else:
                fin[key] = []
        except Exception as e:
            log(f"{sym} {key} 失败: {e}")
            fin[key] = []
    bundle["financials"] = fin

    # ---- 公司行动：分红 + 拆股 ----
    try:
        div = t.dividends
        spl = t.splits
        bundle["actions"] = {
            "dividends": [{"date": str(d.date()), "amount": float(v)}
                          for d, v in div.tail(60).items()],
            "splits": [{"date": str(d.date()), "ratio": float(v)}
                       for d, v in spl.tail(20).items()],
        }
    except Exception as e:
        log(f"{sym} actions 失败: {e}")
        bundle["actions"] = {"dividends": [], "splits": []}

    # ---- K 线 ----
    def hist(interval, period):
        return _retry(lambda: t.history(interval=interval, period=period,
                                        auto_adjust=True),
                      what=f"{sym} kline {interval}")

    daily = hist("1d", "2y")
    bundle["kline_daily"] = _df_to_records(daily)
    bundle["kline_weekly"] = _df_to_records(hist("1wk", "5y"))
    bundle["kline_monthly"] = _df_to_records(hist("1mo", "max"))
    if FETCH_1M:
        m1 = hist("1m", "7d")
        bundle["kline_1m"] = _df_to_records(m1.tail(2500) if m1 is not None else None)
    bundle["indicators"] = calc_indicators(daily)

    # ---- 机构持股 ----
    try:
        ih = t.institutional_holders
        bundle["holders"] = {
            "institutional": _df_to_records(ih.tail(20) if ih is not None else None),
            "major": _df_to_records(getattr(t, "major_holders", None)),
        }
    except Exception as e:
        log(f"{sym} holders 失败: {e}")
        bundle["holders"] = {}

    return bundle


def load_symbols():
    path = os.path.abspath(SYMBOLS_FILE)
    with open(path, "r", encoding="utf-8") as f:
        return [ln.strip().upper() for ln in f
                if ln.strip() and not ln.startswith("#")]


def main():
    """用法: python collect_yahoo.py SHARD TOTAL [ONE_SYMBOL]
    例: python collect_yahoo.py 3 20   -> 全量列表按 20 片并行，本进程跑第 3 片"""
    args = sys.argv[1:]
    only = None
    shard, total = 0, 1
    if args and args[0].isdigit() and len(args) >= 2 and args[1].isdigit():
        shard, total = int(args[0]), int(args[1])
        if len(args) > 2:
            only = args[2].upper()
    elif args:
        only = args[0].upper()
    symbols = load_symbols()
    if only:
        symbols = [only]
    elif total > 1:
        symbols = symbols[shard::total]      # 分片并行
    log(f"Yahoo 采集分片 {shard}/{total}，本片 {len(symbols)} 只: {symbols[:5]}...")
    up = Uploader()
    ok, failed = 0, []
    for i, sym in enumerate(symbols, 1):
        try:
            bundle = collect_symbol(sym)
            body = json.dumps(_clean(bundle), ensure_ascii=False, default=str,
                              separators=(",", ":")).encode("utf-8")
            up.put(f"stocks/{sym}/bundle.json", body)
            ok += 1
            log(f"[{i}/{len(symbols)}] {sym} 上传成功 {len(body)//1024}KB")
        except Exception as e:
            failed.append(sym)
            log(f"[{i}/{len(symbols)}] {sym} 失败: {e}")
        time.sleep(REQ_GAP)
    log(f"=== 完成: 成功 {ok}, 失败 {len(failed)} {failed[:20]} ===")
    if failed:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "..", FAIL_DIR_NAME + ".txt"), "w") as f:
            f.write("\n".join(failed))


if __name__ == "__main__":
    main()
