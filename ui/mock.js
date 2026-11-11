/* 设计稿用的假数据源。施工时由真实后端替换：
 * 保持 window.hioSource = { snapshot(rangeKey) -> Snapshot, ... } 这一形状。
 * Snapshot 契约见 docs/ui-contract.md。 */
(function () {
  const MB = 1048576, GB = 1073741824, TB = GB * 1024;
  const AGENTS = [
    { id: "z-code", name: "Z Code", color: "#7c9cff", base: 1.6 * MB, life: 48.9 * GB, procs: 33 },
    { id: "claude-desktop", name: "Claude Desktop", color: "#ff8a5c", base: 1.1 * MB, life: 822.9 * MB, procs: 19 },
    { id: "claude-code", name: "Claude Code", color: "#e8a26a", base: 0.15 * MB, life: 15.6 * MB, procs: 27 },
    { id: "chatgpt", name: "ChatGPT", color: "#46c98a", base: 0.05 * MB, life: 577.3 * MB, procs: 17 },
    { id: "codex", name: "Codex CLI", color: "#b48cff", base: 0.002 * MB, life: 1.8 * 1024, procs: 2 },
    { id: "antigravity", name: "Antigravity", color: "#5cc8e8", base: 0, life: 68.8 * GB, procs: 0 },
    { id: "qoder", name: "Qoder", color: "#e86ab8", base: 0, life: 1.4 * GB, procs: 0 },
  ];
  const DISKS = [
    { id: "0", name: "Samsung 990 PRO 2TB", media: "NVMe", size: 2 * TB, wear: 4, written: 61 * TB, temp: 44, hours: 6120, spare: 100, merr: 0, w: 0.55 },
    { id: "1", name: "INTEL SSDPEKNW010T8", media: "NVMe", size: 1 * TB, wear: 37, written: 118 * TB, temp: 51, hours: 21880, spare: 100, merr: 0, w: 0.3 },
    { id: "2", name: "WD_BLACK SN850X", media: "NVMe", size: 4 * TB, wear: 17, written: 205 * TB, temp: 47, hours: 9310, spare: 100, merr: 0, w: 0.15 },
    { id: "3", name: "ST4000DM004", media: "HDD", size: 4 * TB, wear: null, written: null, temp: 38, hours: 30111, spare: null, merr: null, w: 0 },
  ];

  function noise(t, seed) {
    return (Math.sin(t / 7 + seed) + Math.sin(t / 2.3 + seed * 3) * 0.6 + Math.sin(t / 31 + seed * 5) * 0.8) / 2.4;
  }
  function spike(t, seed) { const p = Math.sin(t / 3.1 + seed * 11); return p > 0.992 ? 3.5 : 1; }

  function rateAt(a, i, t) {
    if (!a.base) return 0;
    return Math.max(0, a.base * (1 + noise(t, i)) * spike(t, i));
  }

  let mockTaskLaunch = false;     // 设计稿预览：「免授权启动」开关的假状态

  window.hioSource = {
    snapshot(rangeKey, opts) {
      const span = { "1m": 60, "5m": 300, "15m": 900, "1h": 3600, "6h": 21600, "24h": 86400, "7d": 604800 }[rangeKey] || 300;
      const now = Date.now() / 1000;
      const N = 160, step = span / N;
      const t = []; const byAgent = {};
      AGENTS.forEach(a => (byAgent[a.id] = []));
      for (let k = 0; k <= N; k++) {
        const ts = now - span + k * step; t.push(ts);
        AGENTS.forEach((a, i) => byAgent[a.id].push(rateAt(a, i, ts)));
      }
      const agents = AGENTS.map((a, i) => {
        const arr = byAgent[a.id];
        const rangeBytes = arr.reduce((s, v) => s + v * step, 0);
        const live = a.procs > 0;
        return {
          id: a.id, name: a.name, color: a.color, isIde: false, live,
          rateBps: live ? rateAt(a, i, now) : 0,
          rangeBytes, lifetimeBytes: a.life + rangeBytes, procs: a.procs,
          processes: opts && opts.expanded && opts.expanded.includes(a.id) ? [
            { pid: 90536, exe: "node.exe", live: true, writeBytes: rangeBytes * 0.62 },
            { pid: 81548, exe: "python.exe", live: true, writeBytes: rangeBytes * 0.21 },
            { pid: 77120, exe: "rg.exe", live: false, writeBytes: 8.0 * MB, exitedAt: now - 640 },
          ] : null,
        };
      });
      const total = agents.reduce((s, a) => s + a.rateBps, 0);
      const disks = DISKS.map((d, i) => {
        if (d.wear == null) {
          return { id: d.id, name: d.name, media: d.media, sizeBytes: d.size, remainingPct: null,
            rangeWearPct: null, perDayPct: null, writtenBytes: null, rangeWrittenBytes: null,
            tempC: d.temp, powerOnHours: d.hours, spare: null, mediaErrors: null, health: "unknown", curve: null };
        }
        const bytesPerPct = d.written / d.wear;
        let acc = 0; const wear = [];
        for (let k = 0; k <= N; k++) {
          acc += total * d.w * (1 + noise(t[k], i + 9) * 0.5) * step; wear.push(acc / bytesPerPct);
        }
        const rangeWearPct = wear[wear.length - 1];
        const remaining = 100 - d.wear;
        return {
          id: d.id, name: d.name, media: d.media, sizeBytes: d.size,
          remainingPct: remaining,
          rangeWearPct, perDayPct: rangeWearPct / span * 86400,
          writtenBytes: d.written, rangeWrittenBytes: acc,
          tempC: d.temp, powerOnHours: d.hours, spare: d.spare, mediaErrors: d.merr,
          health: remaining <= 20 ? "crit" : remaining <= 40 ? "warn" : "ok",
          curve: { t, wear },
        };
      });
      return {
        ts: now, paused: false, admin: true, notice: "",
        range: { key: rangeKey, span },
        totals: { rateBps: total, todayBytes: 75.4 * GB, activeAgents: agents.filter(a => a.live).length },
        series: { t, byAgent },
        agents, disks,
        taskLaunch: mockTaskLaunch,
      };
    },
    setTaskLaunch(on) { mockTaskLaunch = !!on; },
  };
})();
