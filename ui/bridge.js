/* Harness I/O — 后端桥（pywebview 宿主）。
 * 只在 pywebview 宿主里接管 window.hioSource：动作映射到 Python js_api（window.pywebview.api.*），
 * 快照由后端每秒推送进 window.hio.onSnapshot，本文件不主动拉取。
 * 浏览器直接打开 ui/index.html（无宿主）时这里什么都不做，继续用 mock.js 的假数据。
 * 渲染代码、CSS、HTML 结构都不在这里。 */
(function () {
  "use strict";

  // WebView2 宿主才有 chrome.webview；pywebview 跑在它上面。普通浏览器没有这个对象。
  function inHost() {
    return !!(window.chrome && window.chrome.webview) || !!window.pywebview;
  }
  function api() {
    return window.pywebview && window.pywebview.api;
  }

  // api 在 pywebviewready 之后才注入：之前的动作先排队，就绪后按序补发
  var queue = [];
  function invoke(name, args) {
    var a = api();
    if (!a || typeof a[name] !== "function") {
      queue.push([name, args || []]);
      return;
    }
    try {
      var p = a[name].apply(a, args || []);
      if (p && p.catch) p.catch(function (e) { console.error("[hio]", name, e); });
    } catch (e) {
      console.error("[hio]", name, e);
    }
  }
  function flush() {
    var pending = queue;
    queue = [];
    pending.forEach(function (q) { invoke(q[0], q[1]); });
  }

  // 与 app.js 约定的 hioSource 形状（同 mock.js）
  var source = {
    live: true,                                   // app.js 见到它为真，就不再 pull mock
    snapshot: function () { return null; },       // 快照由后端推送，这里不返回数据
    setView: function (range, expanded, showIde) {
      invoke("set_view", [range, expanded || [], !!showIde]);
    },
    togglePause: function () { invoke("toggle_pause"); },
    setInterval: function (seconds) { invoke("set_interval", [seconds]); },
    exportCsv: function (range) { invoke("export_csv", [range]); },
    reset: function () { invoke("reset"); },
    setMode: function (mode, contentH) { invoke("set_mode", [mode, contentH || 0]); },
    setOnTop: function (on) { invoke("set_on_top", [!!on]); },
    setTaskLaunch: function (on) { invoke("set_task_launch", [!!on]); }
  };

  function install() {
    window.hioSource = source;
    flush();
  }

  if (inHost()) install();
  window.addEventListener("pywebviewready", install);
})();
