/* A股量化交易平台前端：WebSocket 实时行情 + lightweight-charts K 线 + 交易面板 + 回测 */
(() => {
  "use strict";
  const $ = (s) => document.querySelector(s);
  const $$ = (s) => Array.from(document.querySelectorAll(s));
  const LC = window.LightweightCharts;

  const state = {
    ticks: {}, names: {}, symbols: [], orders: new Map(), trades: [], positions: [],
    sel: null, interval: "1m", dir: "BUY", forming: null, kill: false, ws: null, retry: 0,
  };

  // ------------------------------------------------------------- utils
  const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
  const fmt = (v, d = 2) => (v === null || v === undefined || isNaN(v)) ? "--" : Number(v).toLocaleString("zh-CN", { minimumFractionDigits: d, maximumFractionDigits: d });
  const pct = (v, d = 2) => (v === null || v === undefined || isNaN(v)) ? "--" : `${v > 0 ? "+" : ""}${Number(v).toFixed(d)}%`;
  const cls = (v) => (v > 0 ? "up" : v < 0 ? "down" : "");
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const hhmmss = (iso) => (iso ? iso.slice(11, 19) : "");
  const isoToTs = (iso) => Math.floor(Date.parse(iso + "Z") / 1000); // 北京时间按 UTC 显示
  const dirText = (d) => (d === "BUY" ? "买入" : "卖出");
  const STATUS = { SUBMITTED: "已报", PARTIAL: "部成", FILLED: "已成", CANCELLED: "已撤", REJECTED: "废单" };
  function toast(msg) {
    const t = $("#toast"); t.textContent = msg; t.classList.add("show");
    clearTimeout(toast._t); toast._t = setTimeout(() => t.classList.remove("show"), 2600);
  }
  async function api(path, body) {
    const res = await fetch(path, body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || res.statusText);
    return data;
  }
  /** 与后端 calendar.bar_minute 一致：集合竞价并入 9:30，11:30/15:00 并入前一分钟 */
  function barMinute(iso) {
    const h = +iso.slice(11, 13), m = +iso.slice(14, 16), t = h * 60 + m;
    let mm = t;
    if (t >= 565 && t < 570) mm = 570;          // 9:25~9:30 -> 9:30
    else if (t === 690) mm = 689;                // 11:30 -> 11:29
    else if (t === 900) mm = 899;                // 15:00 -> 14:59
    else if (!((t >= 570 && t < 690) || (t >= 780 && t < 900))) return null;
    const day = isoToTs(iso.slice(0, 10) + "T00:00:00");
    return day + mm * 60;
  }

  // ------------------------------------------------------------- charts
  function chartTheme() {
    return {
      layout: { background: { type: "solid", color: css("--surface-1") }, textColor: css("--text-secondary"), fontSize: 11 },
      grid: { vertLines: { color: css("--grid") }, horzLines: { color: css("--grid") } },
      rightPriceScale: { borderColor: css("--border") },
      timeScale: { borderColor: css("--border") },
    };
  }
  const kchart = LC.createChart($("#kchart"), {
    ...chartTheme(), autoSize: true,
    crosshair: { mode: LC.CrosshairMode.Normal },
    timeScale: { timeVisible: true, secondsVisible: false, borderColor: css("--border") },
    localization: { locale: "zh-CN" },
  });
  const candle = kchart.addCandlestickSeries({ borderVisible: false });
  const vol = kchart.addHistogramSeries({ priceFormat: { type: "volume" }, priceScaleId: "vol", lastValueVisible: false, priceLineVisible: false });
  kchart.priceScale("vol").applyOptions({ scaleMargins: { top: 0.8, bottom: 0 } });
  candle.priceScale().applyOptions({ scaleMargins: { top: 0.06, bottom: 0.24 } });
  function paintCandles() {
    candle.applyOptions({ upColor: css("--up"), downColor: css("--down"), wickUpColor: css("--up"), wickDownColor: css("--down") });
  }
  paintCandles();
  const volColor = (b) => (b.close >= b.open ? css("--up-bg").replace(/[\d.]+\)$/, "0.45)") : css("--down-bg").replace(/[\d.]+\)$/, "0.45)"));
  let lastBars = [];
  function showOhlc(b) {
    if (!b) { $("#ohlc").textContent = "--"; return; }
    $("#ohlc").innerHTML = `开 <span>${fmt(b.open)}</span>　高 <span class="up">${fmt(b.high)}</span>　低 <span class="down">${fmt(b.low)}</span>　收 <span class="${cls(b.close - b.open)}">${fmt(b.close)}</span>　量 ${fmt((b.volume || 0) / 100, 0)} 手`;
  }
  kchart.subscribeCrosshairMove((p) => {
    const c = p && p.seriesData ? p.seriesData.get(candle) : null;
    const v = p && p.seriesData ? p.seriesData.get(vol) : null;
    showOhlc(c ? { ...c, volume: v ? v.value : 0 } : lastBars[lastBars.length - 1]);
  });

  async function loadBars() {
    if (!state.sel) return;
    const sym = state.sel, iv = state.interval;
    try {
      const bars = await api(`/api/bars?symbol=${sym}&interval=${iv}&limit=800`);
      if (sym !== state.sel || iv !== state.interval) return;
      lastBars = bars;
      candle.setData(bars.map(({ time, open, high, low, close }) => ({ time, open, high, low, close })));
      vol.setData(bars.map((b) => ({ time: b.time, value: b.volume, color: volColor(b) })));
      state.forming = null;
      showOhlc(bars[bars.length - 1]);
      if (!bars.length) $("#ohlc").textContent = iv === "1d" ? "暂无日线数据：在「策略回测」页下载真实日线后可查看" : "等待行情生成 K 线…";
    } catch (e) { toast(`加载 K 线失败：${e.message}`); }
  }
  function upsertBar(b) {
    const last = lastBars[lastBars.length - 1];
    if (last && b.time < last.time) return;
    if (last && last.time === b.time) lastBars[lastBars.length - 1] = b; else lastBars.push(b);
    candle.update({ time: b.time, open: b.open, high: b.high, low: b.low, close: b.close });
    vol.update({ time: b.time, value: b.volume, color: volColor(b) });
    showOhlc(b);
  }
  /** 用实时 Tick 更新正在形成的 1 分钟 K 线，收盘时由后端 bar 事件校正 */
  function formingFromTick(t) {
    if (state.interval !== "1m" || t.symbol !== state.sel) return;
    const time = barMinute(t.dt);
    if (time === null) return;
    const last = lastBars[lastBars.length - 1];
    if (last && last.time > time) return;
    let f = state.forming;
    if (!f || f.time !== time) {
      f = (last && last.time === time)
        ? { ...last, cumBase: t.volume - (last.volume || 0) }   // 接着服务端返回的未完成 K 线
        : { time, open: t.last, high: t.last, low: t.last, close: t.last, volume: 0, cumBase: f ? f.cumVol : t.volume };
      state.forming = f;
    }
    f.high = Math.max(f.high, t.last); f.low = Math.min(f.low, t.last); f.close = t.last;
    f.volume = Math.max(t.volume - f.cumBase, 0); f.cumVol = t.volume;
    upsertBar({ time: f.time, open: f.open, high: f.high, low: f.low, close: f.close, volume: f.volume });
  }

  // ------------------------------------------------------------- rendering
  function renderWatch(changed) {
    const body = $("#watch-body");
    const syms = state.symbols.length ? state.symbols : Object.keys(state.ticks);
    $("#watch-count").textContent = `${syms.length} 只`;
    if (!changed || body.children.length !== syms.length) {
      body.innerHTML = syms.map((s) => `<tr data-s="${s}"><td><b>${s}</b><span class="nm"></span></td><td class="r num px">--</td><td class="r num chg">--</td></tr>`).join("");
    }
    for (const tr of body.children) {
      const s = tr.dataset.s, t = state.ticks[s];
      tr.classList.toggle("sel", s === state.sel);
      tr.querySelector(".nm").textContent = state.names[s] || (t && t.name) || "";
      if (!t) continue;
      const chg = t.pre_close ? (t.last / t.pre_close - 1) * 100 : 0;
      const px = tr.querySelector(".px"), c = tr.querySelector(".chg");
      const prev = parseFloat(px.dataset.v || "0");
      px.textContent = fmt(t.last); px.dataset.v = t.last; px.classList.remove("up", "down"); if (cls(chg)) px.classList.add(cls(chg));
      c.textContent = pct(chg); c.className = `r num chg ${cls(chg)}`;
      if (changed && changed.has(s) && prev && prev !== t.last) {
        px.classList.remove("flash-up", "flash-down"); void px.offsetWidth;
        px.classList.add(t.last > prev ? "flash-up" : "flash-down");
      }
    }
  }
  function renderQuote() {
    const t = state.ticks[state.sel];
    $("#sym-code").textContent = state.sel || "";
    $("#sym-name").textContent = state.names[state.sel] || (t && t.name) || state.sel || "--";
    if (!t) { $("#sym-last").textContent = "--"; $("#sym-chg").textContent = ""; $("#book").innerHTML = `<div class="muted small">等待行情…</div>`; return; }
    const chg = t.pre_close ? (t.last / t.pre_close - 1) * 100 : 0;
    $("#sym-last").textContent = fmt(t.last); $("#sym-last").className = `num big ${cls(chg)}`;
    $("#sym-chg").textContent = `${t.last - t.pre_close >= 0 ? "+" : ""}${fmt(t.last - t.pre_close)}  ${pct(chg)}`; $("#sym-chg").className = `num ${cls(chg)}`;
    const asks = (t.ask_prices || []).map((p, i) => [p, t.ask_volumes[i]]);
    const bids = (t.bid_prices || []).map((p, i) => [p, t.bid_volumes[i]]);
    const maxV = Math.max(1, ...asks.map((a) => a[1] || 0), ...bids.map((b) => b[1] || 0));
    const row = (k, p, v, side) => `<div class="lv ${side}" data-p="${p || ""}"><span class="muted">${k}</span><span class="${cls(p - t.pre_close)}">${p ? fmt(p) : "--"}</span><span class="v">${v ? fmt(v / 100, 0) : "--"}</span><span class="bar" style="width:${((v || 0) / maxV) * 60}%"></span></div>`;
    if (!asks.length && !bids.length) {
      $("#book").innerHTML = `<div class="muted small">当前行情源无盘口（全市场快照模式）</div>`;
    } else {
      $("#book").innerHTML = [4, 3, 2, 1, 0].map((i) => row(`卖${"一二三四五"[i]}`, ...(asks[i] || [0, 0]), "ask")).join("") + `<div class="sep"></div>` +
        [0, 1, 2, 3, 4].map((i) => row(`买${"一二三四五"[i]}`, ...(bids[i] || [0, 0]), "bid")).join("");
    }
    $("#limits").innerHTML = `<span>涨停 <b class="up num">${fmt(t.limit_up)}</b></span><span>跌停 <b class="down num">${fmt(t.limit_down)}</b></span>`;
  }
  function renderAccount(a) {
    if (!a) return;
    $("#a-equity").textContent = fmt(a.equity); $("#a-cash").textContent = fmt(a.cash);
    $("#a-frozen").textContent = fmt(a.frozen_cash); $("#a-mv").textContent = fmt(a.market_value);
    const dp = $("#a-daypnl"); dp.textContent = fmt(a.day_pnl); dp.className = `num ${cls(a.day_pnl)}`;
    const r = $("#a-ret"); r.textContent = pct(a.total_return_pct); r.className = `num ${cls(a.total_return_pct)}`;
  }
  function renderPositions() {
    $("#pos-body").innerHTML = state.positions.length ? state.positions.map((p) =>
      `<tr><td>${p.symbol}</td><td>${esc(state.names[p.symbol] || "")}</td><td class="r">${p.volume}</td><td class="r">${p.available}</td><td class="r">${fmt(p.cost, 3)}</td><td class="r">${fmt(p.last_price)}</td><td class="r">${fmt(p.market_value)}</td><td class="r ${cls(p.unrealized_pnl)}">${fmt(p.unrealized_pnl)}</td></tr>`).join("")
      : `<tr><td colspan="8" class="muted">暂无持仓</td></tr>`;
  }
  function renderOrders() {
    const list = Array.from(state.orders.values()).sort((a, b) => (b.id > a.id ? 1 : -1)).slice(0, 200);
    $("#ord-body").innerHTML = list.length ? list.map((o) =>
      `<tr><td>${hhmmss(o.updated || o.created)}</td><td>${o.symbol}</td><td class="${o.direction === "BUY" ? "up" : "down"}">${dirText(o.direction)}</td><td class="r">${fmt(o.price)}</td><td class="r">${o.volume}</td><td class="r">${o.traded}</td><td><span class="tag ${o.status}">${STATUS[o.status] || o.status}</span></td><td>${esc(o.strategy)}</td><td class="muted">${esc(o.reason)}</td><td>${["SUBMITTED", "PARTIAL"].includes(o.status) ? `<button class="btn small" data-cancel="${o.id}">撤单</button>` : ""}</td></tr>`).join("")
      : `<tr><td colspan="10" class="muted">暂无委托</td></tr>`;
  }
  function renderTrades() {
    $("#trd-body").innerHTML = state.trades.length ? state.trades.slice(0, 200).map((t) =>
      `<tr><td>${hhmmss(t.dt)}</td><td>${t.symbol}</td><td class="${t.direction === "BUY" ? "up" : "down"}">${dirText(t.direction)}</td><td class="r">${fmt(t.price)}</td><td class="r">${t.volume}</td><td class="r">${fmt(t.commission + t.stamp_duty + t.transfer_fee)}</td><td>${esc(t.strategy)}</td></tr>`).join("")
      : `<tr><td colspan="7" class="muted">暂无成交</td></tr>`;
  }
  function addLog(l) {
    const ul = $("#log-list");
    const li = document.createElement("li");
    li.className = l.level || "info";
    li.innerHTML = `<span class="src">[${esc(l.source)}]</span>${esc(l.message)}`;
    ul.prepend(li);
    while (ul.children.length > 300) ul.lastChild.remove();
  }
  function renderStatus(st) {
    if (!st) return;
    const f = st.feed || {}, pill = $("#feed-pill");
    const labels = { running: "行情正常", closed: "休市", rate_limited: "被限流", error: "行情异常", idle: "未启动" };
    pill.className = `pill ${f.state === "running" ? "ok" : f.state === "closed" || f.state === "idle" ? "warn" : "bad"}`;
    $("#feed-text").textContent = `${f.source || ""} · ${labels[f.state] || f.state || ""}${f.latency_ms ? ` · ${f.latency_ms}ms` : ""}`;
    if (f.symbols) state.symbols = f.symbols;
    if (st.risk) setKill(st.risk.kill_switch, st.risk.kill_reason);
  }
  function setKill(on, reason) {
    state.kill = on;
    const b = $("#kill-btn");
    b.classList.toggle("on", on); b.textContent = on ? "解除熔断" : "一键熔断";
    b.title = on ? `熔断中：${reason || ""}` : "立即撤销全部挂单并禁止新开委托";
  }

  // ------------------------------------------------------------- websocket
  function connect() {
    const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
    state.ws = ws;
    ws.onopen = () => { state.retry = 0; };
    ws.onclose = () => {
      $("#feed-pill").className = "pill bad"; $("#feed-text").textContent = "与服务器断开，重连中…";
      setTimeout(connect, Math.min(1000 * 2 ** state.retry++, 15000));
    };
    ws.onmessage = (ev) => {
      const msg = JSON.parse(ev.data);
      switch (msg.type) {
        case "snapshot": {
          const d = msg.data;
          Object.assign(state.names, d.names || {});
          d.ticks.forEach((t) => (state.ticks[t.symbol] = t));
          state.orders = new Map(d.orders.map((o) => [o.id, o]));
          state.trades = d.trades; state.positions = d.positions;
          renderStatus(d.status);
          if (!state.sel) select(state.symbols[0] || Object.keys(state.ticks)[0]);
          renderWatch(); renderQuote(); renderAccount(d.account); renderPositions(); renderOrders(); renderTrades();
          $("#log-list").innerHTML = ""; d.logs.forEach(addLog);
          break;
        }
        case "ticks": {
          const changed = new Set();
          for (const t of msg.data) {
            if (t.name) state.names[t.symbol] = state.names[t.symbol] || t.name;
            state.ticks[t.symbol] = t; changed.add(t.symbol); formingFromTick(t);
          }
          if (msg.now) $("#clock").textContent = msg.now.replace("T", " ");
          renderWatch(changed);
          if (changed.has(state.sel)) renderQuote();
          break;
        }
        case "bar":
          if (msg.data.symbol === state.sel && msg.data.interval === state.interval) {
            upsertBar({ time: msg.data.time, open: msg.data.open, high: msg.data.high, low: msg.data.low, close: msg.data.close, volume: msg.data.volume });
            if (state.forming && state.forming.time === msg.data.time) state.forming = null;
          }
          break;
        case "order": state.orders.set(msg.data.id, msg.data); renderOrders(); break;
        case "trade": state.trades.unshift(msg.data); renderTrades(); break;
        case "account": renderAccount(msg.data); break;
        case "position": state.positions = msg.data; renderPositions(); break;
        case "log": addLog(msg.data); break;
        case "status": renderStatus(msg.data.engine); break;
      }
    };
  }
  setInterval(async () => { try { renderStatus(await api("/api/status")); renderWatch(); } catch (_) { /* 断线时忽略 */ } }, 5000);

  // ------------------------------------------------------------- interactions
  function select(sym) {
    if (!sym) return;
    state.sel = sym; state.forming = null;
    $("#o-symbol").value = sym;
    const t = state.ticks[sym];
    if (t && !$("#o-price").value) $("#o-price").value = t.last;
    renderWatch(); renderQuote(); loadBars(); updateHint();
  }
  $("#watch-body").addEventListener("click", (e) => { const tr = e.target.closest("tr"); if (tr) { select(tr.dataset.s); $("#o-price").value = state.ticks[tr.dataset.s]?.last ?? ""; } });
  $("#watch-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const sym = $("#watch-input").value.trim();
    try { const r = await api("/api/watchlist", { symbol: sym }); state.symbols = r.symbols; $("#watch-input").value = ""; renderWatch(); select(sym.padStart(6, "0")); }
    catch (err) { toast(err.message); }
  });
  $("#interval-seg").addEventListener("click", (e) => {
    const b = e.target.closest(".seg-b"); if (!b) return;
    $$("#interval-seg .seg-b").forEach((x) => x.classList.toggle("active", x === b));
    state.interval = b.dataset.interval; loadBars();
  });
  $$(".subtab").forEach((b) => b.addEventListener("click", () => {
    $$(".subtab").forEach((x) => x.classList.toggle("active", x === b));
    $$(".pane").forEach((p) => p.classList.toggle("active", p.id === `pane-${b.dataset.pane}`));
  }));
  $$(".tab").forEach((b) => b.addEventListener("click", () => {
    $$(".tab").forEach((x) => x.classList.toggle("active", x === b));
    $("#view-monitor").classList.toggle("hidden", b.dataset.view !== "monitor");
    $("#view-backtest").classList.toggle("hidden", b.dataset.view !== "backtest");
  }));
  $("#book").addEventListener("click", (e) => { const lv = e.target.closest(".lv"); if (lv && lv.dataset.p) $("#o-price").value = lv.dataset.p; });
  $("#dir-seg").addEventListener("click", (e) => {
    const b = e.target.closest(".seg-b"); if (!b) return;
    state.dir = b.dataset.dir;
    $$("#dir-seg .seg-b").forEach((x) => x.classList.toggle("active", x === b));
    const s = $("#o-submit"); s.textContent = dirText(state.dir); s.classList.toggle("buy", state.dir === "BUY"); s.classList.toggle("sell", state.dir === "SELL");
    updateHint();
  });
  $("#o-type").addEventListener("change", () => { $("#o-price").disabled = $("#o-type").value === "MARKET"; });
  function updateHint() {
    const p = state.positions.find((x) => x.symbol === $("#o-symbol").value);
    $("#o-hint").textContent = state.dir === "SELL" ? `可卖 ${p ? p.available : 0} 股（T+1：今日买入次日可卖）` : "主板/创业板 100 股整数倍，科创板 ≥200 股";
  }
  $("#o-symbol").addEventListener("input", updateHint);
  $("#o-submit").classList.add("buy");
  $("#order-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const type = $("#o-type").value;
    const body = { symbol: $("#o-symbol").value.trim(), direction: state.dir, volume: parseInt($("#o-volume").value, 10), order_type: type };
    if (type === "LIMIT") body.price = parseFloat($("#o-price").value);
    try {
      const o = await api("/api/order", body);
      toast(o.status === "REJECTED" ? `委托被拒：${o.reason}` : `已报单 ${o.id}：${dirText(o.direction)} ${o.symbol} ${o.volume} 股`);
    } catch (err) { toast(err.message); }
  });
  $("#ord-body").addEventListener("click", async (e) => {
    const b = e.target.closest("[data-cancel]"); if (!b) return;
    try { await api(`/api/order/${b.dataset.cancel}/cancel`, {}); } catch (err) { toast(err.message); }
  });
  $("#cancel-all").addEventListener("click", async () => { try { const r = await api("/api/cancel_all", {}); toast(`已撤 ${r.cancelled} 笔`); } catch (err) { toast(err.message); } });
  $("#kill-btn").addEventListener("click", async () => {
    if (!state.kill && !confirm("确认一键熔断？将撤销全部挂单并禁止新开委托。")) return;
    try { const r = await api("/api/killswitch", { on: !state.kill }); setKill(r.kill_switch, r.kill_reason); } catch (err) { toast(err.message); }
  });

  // ------------------------------------------------------------- backtest
  const eqchart = LC.createChart($("#eqchart"), { ...chartTheme(), autoSize: true, localization: { locale: "zh-CN" } });
  const money = { type: "price", precision: 0, minMove: 1 };
  const eqLine = eqchart.addLineSeries({ lineWidth: 2, title: "策略", priceLineVisible: false, priceFormat: money });
  const bmLine = eqchart.addLineSeries({ lineWidth: 2, title: "基准", priceLineVisible: false, priceFormat: money });
  const ddchart = LC.createChart($("#ddchart"), { ...chartTheme(), autoSize: true, localization: { locale: "zh-CN" } });
  const ddArea = ddchart.addAreaSeries({ lineWidth: 2, invertFilledArea: true, priceFormat: { type: "percent" }, priceLineVisible: false });
  function paintBacktest() {
    eqLine.applyOptions({ color: css("--series-1") });
    bmLine.applyOptions({ color: css("--series-2") });
    ddArea.applyOptions({ lineColor: css("--series-1"), topColor: "rgba(0,0,0,0)", bottomColor: css("--series-1") + "55" });
  }
  paintBacktest();
  let eqData = [];
  eqchart.subscribeCrosshairMove((p) => {
    const e = p && p.seriesData ? p.seriesData.get(eqLine) : null, b = p && p.seriesData ? p.seriesData.get(bmLine) : null;
    const last = eqData[eqData.length - 1];
    const show = e ? { d: p.time, e: e.value, b: b ? b.value : null } : last ? { d: last.date, e: last.equity, b: last.benchmark } : null;
    $("#eq-readout").innerHTML = show ? `${show.d}　策略 <b>${fmt(show.e)}</b>　基准 ${fmt(show.b)}` : "--";
  });
  ddchart.timeScale().subscribeVisibleLogicalRangeChange((r) => r && eqchart.timeScale().setVisibleLogicalRange(r));
  eqchart.timeScale().subscribeVisibleLogicalRangeChange((r) => r && ddchart.timeScale().setVisibleLogicalRange(r));

  async function loadStrategies() {
    try {
      const r = await api("/api/strategies");
      $("#bt-strategy").innerHTML = r.builtin.map((s) => `<option value="${s}">${s}</option>`).join("");
    } catch (_) { /* ignore */ }
  }
  const DEFAULT_PARAMS = { dual_ma: '{"fast": 5, "slow": 20, "position_pct": 0.2}', breakout: '{"entry": 20, "exit": 10, "atr_stop": 2.0, "position_pct": 0.25}' };
  $("#bt-strategy").addEventListener("change", () => { $("#bt-params").value = DEFAULT_PARAMS[$("#bt-strategy").value] || "{}"; });
  function tile(k, v, c = "") { return `<div class="tile"><div class="k">${k}</div><div class="v ${c}">${v}</div></div>`; }
  $("#bt-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    let params;
    try { params = JSON.parse($("#bt-params").value || "{}"); } catch (_) { toast("策略参数不是合法 JSON"); return; }
    const body = {
      strategy: $("#bt-strategy").value, symbols: $("#bt-symbols").value.split(/[,，\s]+/).filter(Boolean),
      start: $("#bt-start").value || null, end: $("#bt-end").value || null, params,
      initial_cash: parseFloat($("#bt-cash").value), synthetic: $("#bt-synth").checked,
    };
    const btn = $("#bt-run"); btn.disabled = true; $("#bt-msg").textContent = "回测中…";
    try {
      const r = await api("/api/backtest", body);
      const m = r.metrics;
      $("#bt-msg").textContent = r.summary;
      $("#bt-tiles").innerHTML = [
        tile("总收益", pct(m.total_return * 100), cls(m.total_return)),
        tile("基准收益", pct(m.benchmark_return * 100), cls(m.benchmark_return)),
        tile("年化收益", pct(m.annual_return * 100), cls(m.annual_return)),
        tile("最大回撤", pct(m.max_drawdown * 100)),
        tile("夏普比率", fmt(m.sharpe)),
        tile("胜率", `${(m.win_rate * 100).toFixed(1)}%`),
        tile("成交笔数", m.trades),
        tile("总费用", fmt(m.total_fees)),
      ].join("");
      eqData = r.equity;
      eqLine.setData(r.equity.map((x) => ({ time: x.date, value: x.equity })));
      bmLine.setData(r.equity.map((x) => ({ time: x.date, value: x.benchmark })));
      ddArea.setData(r.equity.map((x) => ({ time: x.date, value: x.drawdown * 100 })));
      eqchart.timeScale().fitContent(); ddchart.timeScale().fitContent();
      $("#eq-readout").innerHTML = eqData.length ? `${eqData[eqData.length - 1].date}　策略 <b>${fmt(eqData[eqData.length - 1].equity)}</b>　基准 ${fmt(eqData[eqData.length - 1].benchmark)}` : "--";
      $("#bt-trade-count").textContent = `${r.trades.length} 笔`;
      $("#bt-trades").innerHTML = r.trades.slice().reverse().map((t) =>
        `<tr><td>${t.dt.slice(0, 10)}</td><td>${t.symbol}</td><td class="${t.direction === "BUY" ? "up" : "down"}">${dirText(t.direction)}</td><td class="r">${fmt(t.price)}</td><td class="r">${t.volume}</td><td class="r">${fmt(t.commission + t.stamp_duty + t.transfer_fee)}</td></tr>`).join("");
    } catch (err) { $("#bt-msg").textContent = `回测失败：${err.message}`; }
    finally { btn.disabled = false; }
  });
  $("#bt-download").addEventListener("click", async () => {
    const symbols = $("#bt-symbols").value.split(/[,，\s]+/).filter(Boolean);
    const start = ($("#bt-start").value || "2020-01-01").replaceAll("-", "");
    const btn = $("#bt-download"); btn.disabled = true; $("#bt-msg").textContent = "正在通过 akshare 下载日线（每只间隔 1 秒，避免限流）…";
    try {
      const r = await api("/api/history/download", { symbols, start });
      const ok = Object.entries(r.result).map(([s, n]) => `${s}: ${n < 0 ? "失败" : n + " 条"}`).join("，");
      $("#bt-msg").textContent = `下载完成：${ok}`;
    } catch (err) { $("#bt-msg").textContent = `下载失败：${err.message}`; }
    finally { btn.disabled = false; }
  });

  // ------------------------------------------------------------- theme
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
    [kchart, eqchart, ddchart].forEach((c) => c.applyOptions(chartTheme()));
    paintCandles(); paintBacktest(); loadBars();
  });

  loadStrategies();
  connect();
})();
