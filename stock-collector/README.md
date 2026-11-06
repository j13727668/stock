# stock-collector — 股票数据采集站（GitHub Actions + R2 / 自建服务器）

SEC EDGAR（主）+ 雅虎财经（补充）→ 每只股票一个合并 JSON → Cloudflare R2 / 你自己的服务器 → Worker API → 前端直连。

覆盖全市场 **16502 只**（EDGAR 10409 + NASDAQ 目录补充 5643，含 ETF）。

---

## 目录结构

```
stock-collector/
├── .github/workflows/
│   ├── collect.yml              主采集（20 分片并行，EDGAR+雅虎合并）
│   └── edgar-only.yml           EDGAR 专用补齐（10 分片，超时 180 分钟）
├── collector/
│   ├── fetch_universe.py        全市场清单 + CIK 映射
│   ├── build_edgar_symbols.py   只生成 EDGAR 代码清单
│   ├── collect_symbol.py        ★主力：EDGAR+雅虎合并采集，1 只 1 个包
│   ├── collect_edgar.py         EDGAR 单独采集（补数用）
│   ├── collect_yahoo.py         雅虎单独采集（补数用）
│   ├── build_screener.py        全市场筛选索引 meta/screener.json
│   ├── pack.py                  整体打包 zip 归档
│   └── uploader.py              统一上传层（服务器 / R2 双通道）
├── worker/
│   ├── src/index.js             Cloudflare Worker（一个文件，粘贴即用）
│   └── wrangler.toml
├── requirements.txt
└── SYMBOLS.txt                  可选：自选核心池
```

## 数据流

```
GitHub Actions（20 分片）
   │  每只股票：EDGAR 2 请求 + 雅虎 8 请求
   ▼
合并成一个 JSON：{"symbol","updated_at","edgar":{...},"yahoo":{...}}
   │
   ├─→ 你的服务器  /www/wwwroot/stock-data/stocks/AAPL/AAPL.json
   └─→ R2          stocks/AAPL.json
                        │
                        ▼
              Cloudflare Worker（一个文件）
                        │  CORS 全开
                        ▼
                   你的网站前端
```

## 快速开始

### 1. 建仓库 + 配 Secrets

上传本目录全部文件（**保留 `.github/` 隐藏目录**），Settings → Secrets and variables → Actions：

| 类型 | 名称 | 值 |
|---|---|---|
| Secret | `INGEST_URL` | `https://你的域名/stock-ingest/ingest.php`（用服务器时） |
| Secret | `INGEST_SECRET` | 与 ingest.php 里 SECRET 一致 |
| Secret | `R2_ACCOUNT_ID` | Cloudflare Account ID |
| Secret | `R2_ACCESS_KEY_ID` | R2 API 令牌 Access Key |
| Secret | `R2_SECRET_ACCESS_KEY` | R2 API 令牌 Secret |
| Secret | `R2_BUCKET` | 桶名，如 `stock-data` |
| Variable | `R2_ENABLE` | `1` 写 R2 / `0` 只写服务器 |

> 两个通道至少配一个；都配 = 双写。

### 2. 跑第一次

Actions → **Collect All Stocks (merged per-symbol packs)** → Run workflow
- symbol 留空 = 全市场（20 分片，2~3 小时）
- 填 `AAPL` = 单只调试（1 分钟）

若只有 EDGAR 没跑完：Actions → **EDGAR Only (补齐用)** → Run workflow（10 分片）

### 3. 部署 Worker

Cloudflare → Workers & Pages → Create → 粘贴 `worker/src/index.js` → Deploy
→ Settings → Bindings → R2 bucket，**变量名 `STOCKS`**，选你的桶

> 代码会自动探测任意 R2 绑定，变量名不叫 STOCKS 也能跑。

### 4. 验证

```
/api/meta/universe        16502 只清单
/api/meta/screener        筛选索引（2.5MB）
/api/AAPL                 整包
/api/AAPL?part=filings    公告（8-K 标 is_major=1）
/api/AAPL?part=indicators MA/BOLL/MACD
```

## 本地/服务器命令

```bash
# 分片采集（GitHub 用）
python collector/collect_symbol.py 0 20

# 单只调试
python collector/collect_symbol.py 0 1 AAPL

# 只落本地不传（服务器定时任务）
python collector/collect_symbol.py --local 0 1

# 生成筛选索引并上传
python collector/build_screener.py --upload

# 整体打包归档
python collector/pack.py --split 500 --upload
```

## R2 写入成本

| 方式 | 每轮 PUT 次数 |
|---|---|
| 旧（edgar + bundle 分开） | 33004 |
| **新（每只一个合并包）** | **16502** |

免费额度 100 万次/月 → 够跑 **60 次全量**。日常增量更省。

## 数据字段

**edgar**（SEC 官方）：
- `company`：全名、交易所、SIC 行业、注册州、电话、EIN
- `filings`：最近 20 份公告，8-K/10-K/10-Q 标 `is_major:1`，带原文链接
- `financials`：XBRL 摘要（营收/净利/EPS/毛利/总资产/净资产/现金/经营现金流）

**yahoo**（雅虎财经）：
- `profile`：名称、行业、市值、PE、52 周高低、简介、员工数、官网
- `financials`：季度三大报表明细
- `actions`：分红历史 + 拆股历史
- `kline_1m / kline_daily / kline_weekly / kline_monthly`
- `indicators`：MA5/10/20/60、BOLL(20,2)、MACD(12,26,9)，最近 120 交易日
- `holders`：机构持股前 20 + 大户汇总

## 常见坑

1. **EDGAR job 超时**：每只 2 次请求要下 1~10MB，务必 10 分片 + 180 分钟超时（`edgar-only.yml` 已配好）
2. **雅虎偶发 429**：已带 3 次退避重试，单只失败记 `failed_*.txt`，下次自动补
3. **1 分线只有 7 天**：雅虎免费源上限
4. **私有仓 Actions 分钟数**：全市场每天跑约 3000 分钟，建议「EDGAR 每天 + 雅虎每周」
5. **R2 里不能只放大 zip**：Worker 免费版 CPU 10ms，解不了大包，必须每只一个 JSON
