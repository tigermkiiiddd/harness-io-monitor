/* Harness I/O — 前端。只读后端快照、调后端动作，不存权威态。
 * 数据源：window.hioSource（设计稿 = mock.js；施工后 = 后端桥）。 */
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const NS = "http://www.w3.org/2000/svg";
  const RANGES = [["1m", "1 分钟"], ["5m", "5 分钟"], ["15m", "15 分钟"], ["1h", "1 小时"], ["6h", "6 小时"], ["24h", "24 小时"], ["7d", "7 天"]];
  const DISK_COLORS = ["#7c9cff", "#46c98a", "#f0b545", "#e86ab8", "#5cc8e8", "#ff8a5c"];

  const ui = {
    range: "5m",
    expanded: new Set(),
    showIde: false,
    overlay: false,
    drawer: false,
    snap: null,
  };

  // ---------- 格式化 ----------
  function bytes(n) {
    if (n == null) return "—";
    const u = ["B", "KB", "MB", "GB", "TB", "PB"]; let i = 0;
    while (Math.abs(n) >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return i === 0 ? `${Math.round(n)} B` : `${n >= 100 ? n.toFixed(0) : n >= 10 ? n.toFixed(1) : n.toFixed(2)} ${u[i]}`;
  }
  function rateParts(bps) {
    const s = bytes(bps); const k = s.lastIndexOf(" ");
    return [s.slice(0, k), s.slice(k + 1) + "/s"];
  }
  const rate = (bps) => rateParts(bps).join(" ");
  // 寿命下降：小数位自适应，保留 2 位有效数字
  function pct(v) {
    if (v == null) return "—";
    if (v <= 1e-8) return "0%";
    // 短区间内下降量常在 1e-5% 量级，按 2 位有效数字给全，不截成「<0.0001%」
    const d = Math.max(0, Math.min(8, 1 - Math.floor(Math.log10(v))));
    return v.toFixed(d) + "%";
  }
  const declineText = (v) => (v == null ? "—" : v <= 1e-8 ? "0%" : "−" + pct(v));
  const hhmm = (ts, withSec) => {
    const d = new Date(ts * 1000);
    const p = (x) => String(x).padStart(2, "0");
    return withSec ? `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}` : `${p(d.getHours())}:${p(d.getMinutes())}`;
  };
  const tickLabel = (ts, span) => span > 2 * 86400
    ? `${new Date(ts * 1000).getMonth() + 1}/${new Date(ts * 1000).getDate()}` : hhmm(ts, span <= 300);

  // ---------- SVG 工具 ----------
  function el(tag, attrs, parent) {
    const n = document.createElementNS(NS, tag);
    for (const k in attrs) n.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(n);
    return n;
  }
  function sizeOf(svg) {
    const r = svg.getBoundingClientRect();
    svg.setAttribute("viewBox", `0 0 ${Math.max(1, r.width)} ${Math.max(1, r.height)}`);
    return [r.width, r.height];
  }
  function niceMax(v) {
    if (v <= 0) return 1;
    const p = Math.pow(10, Math.floor(Math.log10(v))); const m = v / p;
    return (m <= 1 ? 1 : m <= 2 ? 2 : m <= 5 ? 5 : 10) * p;
  }
  function linePath(xs, ys) {
    let d = "";
    for (let i = 0; i < xs.length; i++) d += (i ? "L" : "M") + xs[i].toFixed(1) + " " + ys[i].toFixed(1);
    return d;
  }

  // ---------- 磁盘图标 ----------
  function diskIcon(d, cls) {
    const svg = el("svg", { viewBox: "0 0 18 18", class: "disk-ico " + cls });
    if (d.media === "HDD") {
      el("rect", { x: 2.5, y: 2.5, width: 13, height: 13, rx: 2.5, class: "body" }, svg);
      el("circle", { cx: 9, cy: 8.5, r: 3.4, class: "body" }, svg);
      el("circle", { cx: 9, cy: 8.5, r: 0.9, class: "fill" }, svg);
    } else {
      el("rect", { x: 2, y: 4.5, width: 14, height: 9, rx: 2, class: "body" }, svg);
      // 剩余寿命：内部填充条长度
      const rem = d.remainingPct == null ? 0 : d.remainingPct / 100;
      el("rect", { x: 4, y: 6.5, width: (10 * rem).toFixed(2), height: 5, rx: 1, class: "fill" }, svg);
    }
    return svg;
  }

  // ---------- 渲染：顶栏 ----------
  function renderTop(s) {
    const [v, u] = rateParts(s.totals.rateBps);
    $("rate").innerHTML = `${v}<span class="unit">${u}</span>`;
    $("today").textContent = bytes(s.totals.todayBytes);
    $("active").textContent = s.totals.activeAgents;
    $("status").textContent = s.notice || (s.paused ? "已暂停" : "");
    document.body.classList.toggle("paused", !!s.paused);
    drawTotalSpark($("rateSpark"), s, 60);
  }

  function totalSeries(s) {
    const n = s.series.t.length; const out = new Array(n).fill(0);
    for (const id in s.series.byAgent) s.series.byAgent[id].forEach((v, i) => (out[i] += v));
    return out;
  }
  function drawTotalSpark(svg, s, lastN) {
    const [w, h] = sizeOf(svg); svg.replaceChildren();
    let ys = totalSeries(s); if (lastN) ys = ys.slice(-lastN);
    if (ys.length < 2 || w < 10) return;
    const max = Math.max(...ys, 1);
    const xs = ys.map((_, i) => (i / (ys.length - 1)) * w);
    const yy = ys.map((v) => h - 2 - (v / max) * (h - 4));
    const d = linePath(xs, yy);
    el("path", { d: d + `L${w} ${h}L0 ${h}Z`, fill: "var(--accent)", "fill-opacity": 0.12 }, svg);
    el("path", { d, fill: "none", stroke: "var(--accent)", "stroke-width": 1.5, "stroke-linejoin": "round" }, svg);
  }

  function renderDiskChips(container, s) {
    container.replaceChildren();
    s.disks.forEach((d) => {
      const c = document.createElement("span");
      c.className = "chip" + (d.rangeWearPct == null ? " dim" : "");
      c.title = `${d.name}\n剩余寿命 ${d.remainingPct == null ? "—" : d.remainingPct + "%"}`
        + (d.rangeWearPct == null ? "\n无寿命数据" : `\n本区间寿命 −${pct(d.rangeWearPct)}`);
      c.appendChild(diskIcon(d, "h-" + d.health));
      const life = document.createElement("span"); life.className = "life";
      life.textContent = d.remainingPct == null ? "—" : d.remainingPct + "%";
      c.appendChild(life);
      if (d.rangeWearPct != null) {
        const t = document.createElement("span"); t.className = "decline"; t.textContent = declineText(d.rangeWearPct);
        c.appendChild(t);
      }
      container.appendChild(c);
    });
  }

  // ---------- 渲染：排行 ----------
  function renderAgents(s) {
    const list = $("agents");
    const maxRange = Math.max(1, ...s.agents.map((a) => a.rangeBytes));
    const rows = [...s.agents].sort((a, b) => (b.live - a.live) || (b.rangeBytes - a.rangeBytes) || (b.lifetimeBytes - a.lifetimeBytes));
    const frag = document.createDocumentFragment();
    rows.forEach((a) => {
      const li = document.createElement("li");
      const open = ui.expanded.has(a.id);
      li.className = (a.live ? "" : "stopped ") + (open ? "open" : "");
      const hot = a.rateBps > 50 * 1048576 ? "very-hot" : a.rateBps > 10 * 1048576 ? "hot" : "";
      li.innerHTML = `
        <div class="agent-row" data-id="${a.id}">
          <div class="agent">
            <svg class="chev" viewBox="0 0 10 10"><path d="M3 1.5 7 5 3 8.5z"/></svg>
            <i class="dot" style="background:${a.color}"></i>
            <span class="name">${a.name}</span>
            <span class="meta">${a.live ? a.procs + " 进程" : "已停止"}</span>
          </div>
          <span class="num rate ${hot}">${a.live ? rate(a.rateBps) : "—"}</span>
          <div class="range-cell"><span class="num">${bytes(a.rangeBytes)}</span>
            <span class="bar"><i style="width:${(a.rangeBytes / maxRange) * 100}%;background:${a.color}"></i></span></div>
          <span class="num">${bytes(a.lifetimeBytes)}</span>
        </div>`;
      if (open && a.processes) {
        const ul = document.createElement("ul"); ul.className = "procs";
        a.processes.forEach((p) => {
          const r = document.createElement("li"); r.className = "proc" + (p.live ? "" : " gone");
          r.innerHTML = `<span class="p-name">${p.live ? "●" : "○"} ${p.exe}<span class="pid">#${p.pid}</span></span>
            <span class="tag">${p.live ? "" : "退出 " + hhmm(p.exitedAt)}</span>
            <span class="num">${bytes(p.writeBytes)}</span><span></span>`;
          ul.appendChild(r);
        });
        li.appendChild(ul);
      }
      frag.appendChild(li);
    });
    list.replaceChildren(frag);
  }

  // ---------- 渲染：堆叠面积图 ----------
  function stacked(s) {
    const ids = s.agents.filter((a) => s.series.byAgent[a.id] && s.series.byAgent[a.id].some((v) => v > 0)).map((a) => a.id);
    const n = s.series.t.length; const base = new Array(n).fill(0); const layers = [];
    ids.forEach((id) => {
      const lo = base.slice(); const vals = s.series.byAgent[id];
      vals.forEach((v, i) => (base[i] += v));
      layers.push({ id, lo, hi: base.slice() });
    });
    return { layers, top: base };
  }
  function renderChart(s) {
    const svg = $("chart"); const [w, h] = sizeOf(svg); svg.replaceChildren();
    if (w < 40 || h < 40) return;
    const padL = 54, padR = 6, padT = 6, padB = 20;
    const pw = w - padL - padR, ph = h - padT - padB;
    const { layers, top } = stacked(s);
    const max = niceMax(Math.max(...top, 1));
    const t0 = s.series.t[0], t1 = s.series.t[s.series.t.length - 1];
    const X = (t) => padL + ((t - t0) / (t1 - t0)) * pw;
    const Y = (v) => padT + ph - (v / max) * ph;
    for (let k = 0; k <= 2; k++) {
      const v = (max * k) / 2, y = Y(v);
      el("line", { x1: padL, x2: w - padR, y1: y, y2: y, class: "grid" }, svg);
      el("text", { x: padL - 8, y: y + 3, "text-anchor": "end", class: "axis" }, svg).textContent = rate(v).replace(".00", "");
    }
    for (let k = 0; k <= 4; k++) {
      const tt = t0 + ((t1 - t0) * k) / 4;
      el("text", { x: X(tt), y: h - 4, "text-anchor": k === 0 ? "start" : k === 4 ? "end" : "middle", class: "axis" }, svg)
        .textContent = tickLabel(tt, s.range.span);
    }
    const xs = s.series.t.map(X);
    const color = Object.fromEntries(s.agents.map((a) => [a.id, a.color]));
    layers.forEach((L) => {
      const up = linePath(xs, L.hi.map(Y));
      const down = linePath(xs.slice().reverse(), L.lo.map(Y).reverse()).replace(/^M/, "L");
      el("path", { d: up + down + "Z", fill: color[L.id], "fill-opacity": 0.22 }, svg);
      el("path", { d: up, fill: "none", stroke: color[L.id], "stroke-width": 1.4, "stroke-linejoin": "round" }, svg);
    });
    const cross = el("line", { y1: padT, y2: padT + ph, class: "cross", visibility: "hidden" }, svg);
    svg.onmousemove = (e) => {
      const r = svg.getBoundingClientRect(); const mx = e.clientX - r.left;
      if (mx < padL) return hideTip();
      const i = Math.max(0, Math.min(xs.length - 1, Math.round(((mx - padL) / pw) * (xs.length - 1))));
      cross.setAttribute("x1", xs[i]); cross.setAttribute("x2", xs[i]); cross.setAttribute("visibility", "visible");
      const tip = $("tip");
      const rowsHtml = s.agents.filter((a) => (s.series.byAgent[a.id] || [])[i] > 0)
        .sort((a, b) => s.series.byAgent[b.id][i] - s.series.byAgent[a.id][i])
        .map((a) => `<div class="r"><i style="background:${a.color}"></i>${a.name}<b>${rate(s.series.byAgent[a.id][i])}</b></div>`).join("");
      tip.innerHTML = `<div class="t">${hhmm(s.series.t[i], true)} · 合计 ${rate(top[i])}</div>${rowsHtml}`;
      tip.hidden = false;
      const tw = tip.offsetWidth;
      tip.style.left = (xs[i] + 12 + tw > w ? xs[i] - tw - 12 : xs[i] + 12) + "px";
      tip.style.top = "8px";
    };
    svg.onmouseleave = hideTip;
    function hideTip() { $("tip").hidden = true; cross.setAttribute("visibility", "hidden"); }

    $("legend").innerHTML = layers.map((L) => {
      const a = s.agents.find((x) => x.id === L.id);
      return `<span><i style="background:${a.color}"></i>${a.name}</span>`;
    }).join("");
  }

  // ---------- 渲染：磁盘抽屉 ----------
  function drawDiskCurves(svg, disks, span, big) {
    const [w, h] = sizeOf(svg); svg.replaceChildren();
    const curves = disks.filter((d) => d.curve);
    if (!curves.length || w < 20 || h < 10) return;
    const padL = big ? 62 : 0, padB = big ? 20 : 0, padT = big ? 6 : 3, padR = big ? 6 : 0;
    const pw = w - padL - padR, ph = h - padT - padB;
    const max = big ? niceMax(Math.max(...curves.map((d) => d.curve.wear[d.curve.wear.length - 1]), 1e-6))
      : Math.max(curves[0].curve.wear[curves[0].curve.wear.length - 1], 1e-9);
    const t0 = curves[0].curve.t[0], t1 = curves[0].curve.t[curves[0].curve.t.length - 1];
    const X = (t) => padL + ((t - t0) / (t1 - t0)) * pw;
    const Y = (v) => padT + ph - (v / max) * ph;
    if (big) {
      for (let k = 0; k <= 2; k++) {
        const v = (max * k) / 2, y = Y(v);
        el("line", { x1: padL, x2: w - padR, y1: y, y2: y, class: "grid" }, svg);
        el("text", { x: padL - 8, y: y + 3, "text-anchor": "end", class: "axis" }, svg).textContent = k ? "−" + pct(v) : "0";
      }
      for (let k = 0; k <= 4; k++) {
        const tt = t0 + ((t1 - t0) * k) / 4;
        el("text", { x: X(tt), y: h - 4, "text-anchor": k === 0 ? "start" : k === 4 ? "end" : "middle", class: "axis" }, svg)
          .textContent = tickLabel(tt, span);
      }
    } else {
      el("line", { x1: 0, x2: w, y1: h - 0.5, y2: h - 0.5, class: "grid" }, svg);
    }
    curves.forEach((d) => {
      const c = d._color;
      const xs = d.curve.t.map(X), ys = d.curve.wear.map(Y);
      const p = linePath(xs, ys);
      if (!big) el("path", { d: p + `L${xs[xs.length - 1]} ${padT + ph}L${xs[0]} ${padT + ph}Z`, fill: c, "fill-opacity": 0.12 }, svg);
      el("path", { d: p, fill: "none", stroke: c, "stroke-width": big ? 1.8 : 1.5, "stroke-linejoin": "round" }, svg);
    });
  }

  function renderDrawer(s) {
    const rangeName = RANGES.find((r) => r[0] === ui.range)[1];
    $("drawerSub").textContent = `近 ${rangeName} · 寿命下降估算`;
    s.disks.forEach((d, i) => (d._color = DISK_COLORS[i % DISK_COLORS.length]));
    const list = $("diskList"); list.replaceChildren();
    s.disks.forEach((d) => {
      const card = document.createElement("div"); card.className = "disk-card";
      card.appendChild(diskIcon(d, "h-" + d.health));
      const mid = document.createElement("div");
      mid.innerHTML = `<div class="dn"><i class="swatch" style="background:${d._color}"></i>${d.name}</div>
        <div class="dm">${d.media} · ${bytes(d.sizeBytes)} · 剩余寿命 ${d.remainingPct == null ? "—" : d.remainingPct + "%"}</div>`;
      card.appendChild(mid);
      const right = document.createElement("div"); right.className = "dv";
      right.innerHTML = d.rangeWearPct == null
        ? `<span class="decline" style="color:var(--text-3)">—</span><span class="perday">无寿命数据</span>`
        : `<span class="decline">${declineText(d.rangeWearPct)}</span><span class="perday">≈ ${pct(d.perDayPct)} / 天</span>`;
      card.appendChild(right);
      if (d.curve) {
        const sv = el("svg", { class: "spark", preserveAspectRatio: "none" }); card.appendChild(sv);
        requestAnimationFrame(() => drawDiskCurves(sv, [d], s.range.span, false));
      }
      const facts = document.createElement("div"); facts.className = "facts";
      facts.innerHTML = `<span>累计写入<b>${bytes(d.writtenBytes)}</b></span>
        <span>温度<b>${d.tempC == null ? "—" : d.tempC + "°C"}</b></span>
        <span>通电<b>${d.powerOnHours == null ? "—" : d.powerOnHours.toLocaleString() + " h"}</b></span>
        <span>介质错误<b>${d.mediaErrors == null ? "—" : d.mediaErrors}</b></span>`;
      card.appendChild(facts);
      list.appendChild(card);
    });
    if (ui.overlay) requestAnimationFrame(() => drawDiskCurves($("diskBig"), s.disks, s.range.span, true));
  }

  // ---------- 渲染：极简 ----------
  function renderMini(s) {
    const [v, u] = rateParts(s.totals.rateBps);
    $("miniRate").innerHTML = `${v}<span class="unit">${u}</span>`;
    $("miniToday").textContent = "今日 " + bytes(s.totals.todayBytes);
    drawTotalSpark($("miniSpark"), s, 90);
    $("miniAgents").innerHTML = s.agents.filter((a) => a.live).sort((a, b) => b.rateBps - a.rateBps).slice(0, 3)
      .map((a) => `<li><i class="dot" style="background:${a.color}"></i><span class="name">${a.name}</span><span class="num">${rate(a.rateBps)}</span></li>`).join("");
    renderDiskChips($("miniDisks"), s);
  }

  // ---------- 主循环 ----------
  function pull() {
    const s = window.hioSource.snapshot(ui.range, { expanded: [...ui.expanded] });
    if (s) ui.snap = s;   // 宿主模式下 snapshot 返回 null（快照由后端推送），不覆盖已有快照
    render();
  }
  // 「免授权启动」勾选状态以后端为准（任务是否存在）；null = 非管理员，置灰。刚点过的 1.5 秒内不回写，免得闪回。
  let taskClickedAt = 0;
  function syncTaskOpt(s) {
    const box = $("optTask");
    const state = s.taskLaunch;
    box.disabled = state == null;
    $("optTaskRow").title = state == null ? "需要以管理员身份运行才能设置" : "开启后从快捷方式启动不再弹出 UAC（不会开机自启）";
    if (Date.now() - taskClickedAt > 1500) box.checked = !!state;
  }
  function render() {
    const s = ui.snap; if (!s) return;
    syncTaskOpt(s);
    if (document.documentElement.dataset.mode === "mini") { renderMini(s); return; }
    renderTop(s);
    renderDiskChips($("diskChips"), s);
    renderAgents(s);
    renderChart(s);
    if (ui.drawer) renderDrawer(s);
  }

  // ---------- 交互 ----------
  function initRanges() {
    const seg = $("ranges");
    seg.innerHTML = RANGES.map(([k, n]) => `<button data-k="${k}" class="${k === ui.range ? "on" : ""}">${n}</button>`).join("");
    seg.onclick = (e) => {
      const b = e.target.closest("button"); if (!b) return;
      ui.range = b.dataset.k;
      seg.querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b));
      sendView();
      pull();
    };
  }
  function setDrawer(open) {
    ui.drawer = open;
    document.body.classList.toggle("drawer-open", open);
    $("drawer").setAttribute("aria-hidden", String(!open));
    if (open && ui.snap) renderDrawer(ui.snap);   // 宿主模式下首帧快照未到时先不渲染，等推送进来由 render 补上
  }
  // 极简模式的内容高度（CSS 像素）：子元素实际高度之和 + 行距 + 上下 padding。
  // 不用 .mini.scrollHeight：.mini 被 grid 行拉满，读到的是窗口高度，会越拉越高。
  function miniHeight() {
    const m = $("mini");
    const gap = parseFloat(getComputedStyle(m).rowGap) || 0;
    const kids = [...m.children];
    const body = kids.reduce((s, c) => s + c.offsetHeight, 0) + gap * Math.max(0, kids.length - 1);
    const pad = getComputedStyle(document.querySelector(".app"));
    return Math.ceil(body + parseFloat(pad.paddingTop) + parseFloat(pad.paddingBottom));
  }
  // 内容高度变了才告诉后端改窗口高（后端把它换算成窗口高并加上标题栏）
  let lastMiniH = 0;
  function fitMini() {
    const h = miniHeight();
    if (h !== lastMiniH && window.hioSource.setMode) {
      lastMiniH = h;
      window.hioSource.setMode("mini", h);
    }
  }
  // 把当前视图参数交给后端（区间 / 展开 / 显示 IDE）。设计稿 mock 没有 setView，自动跳过。
  function sendView() {
    if (window.hioSource.setView) window.hioSource.setView(ui.range, [...ui.expanded], ui.showIde);
  }
  function setMode(mode) {
    document.documentElement.dataset.mode = mode;
    render();
    if (mode === "mini") {
      lastMiniH = 0;
      fitMini();
    } else if (window.hioSource.setMode) {
      window.hioSource.setMode("full", 0);
    }
  }

  $("agents").onclick = (e) => {
    const row = e.target.closest(".agent-row"); if (!row) return;
    const id = row.dataset.id;
    ui.expanded.has(id) ? ui.expanded.delete(id) : ui.expanded.add(id);
    sendView();
    pull();
  };
  $("diskChips").onclick = () => setDrawer(true);
  $("diskChips").onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") setDrawer(true); };
  $("miniDisks").onclick = () => { setMode("full"); setDrawer(true); };
  $("btnCloseDrawer").onclick = () => setDrawer(false);
  $("scrim").onclick = () => setDrawer(false);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") { setDrawer(false); $("menu").hidden = true; } });
  $("overlaySeg").onclick = (e) => {
    const b = e.target.closest("button"); if (!b) return;
    ui.overlay = b.dataset.v === "overlay";
    $("overlaySeg").querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b));
    $("drawer").classList.toggle("overlay", ui.overlay);
    if (!ui.snap) return;
    renderDrawer(ui.snap);
    if (ui.overlay) setTimeout(() => ui.snap && drawDiskCurves($("diskBig"), ui.snap.disks, ui.snap.range.span, true), 340);
  };
  $("btnMenu").onclick = (e) => { e.stopPropagation(); $("menu").hidden = !$("menu").hidden; };
  document.addEventListener("click", (e) => { if (!e.target.closest(".menu")) $("menu").hidden = true; });
  $("btnPause").onclick = () => window.hioSource.togglePause && window.hioSource.togglePause();
  $("btnMini").onclick = () => setMode("mini");
  $("btnFull").onclick = () => setMode("full");
  $("btnPin").onclick = () => {
    const on = !$("btnPin").classList.contains("on");
    $("btnPin").classList.toggle("on", on);
    window.hioSource.setOnTop && window.hioSource.setOnTop(on);
  };
  $("btnExport").onclick = () => window.hioSource.exportCsv && window.hioSource.exportCsv(ui.range);
  $("btnReset").onclick = () => window.hioSource.reset && window.hioSource.reset();
  $("optIde").onchange = (e) => { ui.showIde = e.target.checked; sendView(); };
  $("optTask").onchange = (e) => {
    taskClickedAt = Date.now();
    window.hioSource.setTaskLaunch && window.hioSource.setTaskLaunch(e.target.checked);
  };
  $("optInterval").onchange = (e) => window.hioSource.setInterval && window.hioSource.setInterval(parseFloat(e.target.value));

  let rz; window.addEventListener("resize", () => { clearTimeout(rz); rz = setTimeout(render, 60); });

  initRanges();
  pull();
  // 设计稿预览：每秒拉 mock。接入 pywebview 后 hioSource.live 为真，停止拉取，改由后端推送 onSnapshot。
  setInterval(() => { if (!window.hioSource.live) pull(); }, 1000);
  window.hio = {
    onSnapshot(s) {
      ui.snap = s;
      render();
      if (document.documentElement.dataset.mode === "mini") fitMini();
    },
    setMode,
  };
})();
