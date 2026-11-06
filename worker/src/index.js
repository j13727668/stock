// stock-data-api v3 — 读 R2 上的「每只股票一个合并小包」，出统一 API
//
// 存储布局（新）:  stocks/AAPL.json  = { symbol, updated_at, edgar:{...}, yahoo:{...} }
// 兼容布局（旧）:  stocks/AAPL/edgar.json + stocks/AAPL/bundle.json
//
// 路由：
//   GET /api/AAPL                        -> 整包
//   GET /api/AAPL?part=profile           -> 雅虎子项 (profile/financials/actions/kline_*/indicators/holders)
//   GET /api/AAPL?part=filings           -> EDGAR 子项 (company/filings/financials)
//   GET /api/AAPL/edgar                  -> 只看 EDGAR 部分
//   GET /api/AAPL/yahoo?part=indicators  -> 只看雅虎部分（可再 part）
//   GET /api/meta/universe               -> 全市场清单
//   GET /api/meta/screener               -> 全市场筛选索引（前端本地筛选用）
// CORS 全开。

const YAHOO_KEYS = ["profile", "financials", "actions", "kline_1m", "kline_daily",
  "kline_weekly", "kline_monthly", "indicators", "holders"];
const EDGAR_KEYS = ["company", "filings", "financials"];

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const R2 = env.STOCKS
      || Object.values(env).find(v => v && typeof v.get === "function" && typeof v.put === "function");
    if (!R2) return json({ error: "R2 binding missing", envKeys: Object.keys(env) }, 500);

    if (request.method === "OPTIONS") return new Response(null, { headers: cors() });
    if (request.method !== "GET") return json({ error: "method not allowed" }, 405);

    // 全市场清单
    if (url.pathname === "/api/meta/universe") {
      const u = await R2.get("meta/universe.json");
      if (!u) return json({ error: "universe not ready" }, 404);
      const h = cors();
      h.set("Content-Type", "application/json; charset=utf-8");
      h.set("Cache-Control", "public, max-age=3600");
      return new Response(u.body, { headers: h });
    }

    // 全市场筛选索引（约 2.5MB，边缘缓存 30 分钟，回源极少）
    if (url.pathname === "/api/meta/screener") {
      const s = await R2.get("meta/screener.json");
      if (!s) return json({ error: "screener not ready", hint: "run build_screener.py --upload" }, 404);
      const h = cors();
      h.set("Content-Type", "application/json; charset=utf-8");
      h.set("Cache-Control", "public, max-age=1800");
      return new Response(s.body, { headers: h });
    }

    // 股票路由：/api/{SYM} 或 /api/{SYM}/edgar 或 /api/{SYM}/yahoo
    const m = url.pathname.match(/^\/api\/([A-Za-z0-9.\-]{1,10})(?:\/(edgar|yahoo))?$/);
    if (!m) return json({ error: "usage: /api/:symbol[?part=xxx] | /api/:symbol/edgar" }, 404);
    const symbol = m[1].toUpperCase();
    const scope = m[2] || null;
    const part = url.searchParams.get("part");

    // 1) 优先读合并包
    let data = null;
    const obj = await R2.get(`stocks/${symbol}.json`);
    if (obj) {
      data = await obj.json();
    } else {
      // 2) 回退旧布局
      if (scope === "edgar" || (part && EDGAR_KEYS.includes(part) && part !== "financials")) {
        const e = await R2.get(`stocks/${symbol}/edgar.json`);
        if (!e) return notFound(symbol, "edgar");
        data = { symbol, updated_at: null, edgar: await e.json(), yahoo: {} };
      } else {
        const b = await R2.get(`stocks/${symbol}/bundle.json`);
        if (!b) return notFound(symbol, "yahoo");
        data = { symbol, updated_at: null, edgar: {}, yahoo: await b.json() };
      }
    }

    // 整包直通
    if (!part && !scope) return json(data);

    // 按 scope 取部分
    if (scope) {
      const sub = data[scope];
      if (!sub) return json({ symbol, [scope]: null, note: "no data for this source" }, 200);
      if (!part) return json({ symbol, updated_at: data.updated_at, [scope]: sub });
      if (!(part in sub)) return json({ error: "unknown part", part, scope }, 404);
      return json({ symbol, updated_at: data.updated_at, [part]: sub[part] });
    }

    // 按 part 查（financials 两边都有，默认 EDGAR 摘要，?from=yahoo 取明细）
    if (part === "financials") {
      const from = url.searchParams.get("from") === "yahoo" ? "yahoo" : "edgar";
      return json({ symbol, updated_at: data.updated_at, from,
                    financials: data[from]?.financials });
    }
    if (EDGAR_KEYS.includes(part)) {
      return json({ symbol, updated_at: data.updated_at, [part]: data.edgar?.[part] });
    }
    if (YAHOO_KEYS.includes(part)) {
      return json({ symbol, updated_at: data.updated_at, [part]: data.yahoo?.[part] });
    }
    return json({ error: "unknown part", part,
                  available: { edgar: EDGAR_KEYS, yahoo: YAHOO_KEYS } }, 404);
  },
};

function notFound(symbol, source) {
  return json({ error: "not found", symbol, source }, 404);
}

function cors() {
  const h = new Headers();
  h.set("Access-Control-Allow-Origin", "*");
  h.set("Access-Control-Allow-Methods", "GET, OPTIONS");
  h.set("Access-Control-Allow-Headers", "*");
  h.set("Access-Control-Max-Age", "86400");
  return h;
}

function json(obj, status = 200) {
  const h = cors();
  h.set("Content-Type", "application/json; charset=utf-8");
  h.set("Cache-Control", "public, max-age=300");
  return new Response(JSON.stringify(obj), { status, headers: h });
}
