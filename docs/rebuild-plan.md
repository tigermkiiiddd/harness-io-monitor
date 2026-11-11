# 界面重建施工说明（2026-11-11）

设计稿：`ui/index.html` + `ui/app.css` + `ui/app.js` + `ui/mock.js`（假数据）。
**设计稿就是成品界面**，施工只做「接真数据」和「换外壳」，不改视觉、不改交互、不加控件。
有拿不准的视觉/交互问题，停下写进报告，不要自己发挥。

## 用户要的（原话要点）

1. 界面丑、占空间、不自适应、没有极简模式 → 设计稿已解决，照搬。
2. 运行中周期性弹出空白 PowerShell 窗口，体验极差 → **程序运行期间不得启动任何子进程**（PowerShell / cmd / wmic 一律不准）。
3. 磁盘预览 = 小图标 + 剩余寿命 + 区间寿命下降；详情里每盘一条小曲线；切「叠加」后合成一张大图。
4. 冗余信息清掉：只显示剩余寿命（不显示已用寿命）、调试信息（采样耗时/下次落库）不常驻、不显示「(+N 退出)」。
5. **不做开机自启、不拆后台进程。** 程序结构维持：一个进程，打开即采集，关窗收进托盘继续采集，托盘菜单「退出」才真退出。

## 技术栈

- 外壳：**pywebview**（Windows 下走 Edge WebView2，本机已装）。`pip install pywebview`。
- 前端：`ui/` 下纯 HTML/CSS/JS，无构建、无 CDN（离线桌面程序）。
- 后端：保留 `core/`（sampler / store / classify / winproc / nvme / smart / tray / single / dataguard）。
- tkinter 整个退役：`desktop.py`、`desktop.py.bak` 删除，入口改为 `app.py`。

## 数据流

```
Sampler 线程 ──► SnapshotBuilder（Python，算好所有展示值）──► 每 1 秒
    window.evaluate_js("window.hio && window.hio.onSnapshot(<json>)")
JS ──► window.pywebview.api.<method>() 调后端动作（切区间/展开/暂停/导出/清除/极简/置顶）
```

- 前端不存权威状态，不自己算业务数字；所有数字由 Python 算好下发。
- `ui/app.js` 里的 `setInterval(pull, 1000)` 和 `window.hioSource` 只在**没有** `window.pywebview` 时使用（设计稿预览）；在 pywebview 里改为后端推送。
- `ui/mock.js` 保留，只在浏览器直接打开设计稿时加载（pywebview 下不加载或不生效）。

## Snapshot 契约（JSON，字段名固定）

```jsonc
{
  "ts": 1794380000.0,            // 秒
  "paused": false,
  "admin": true,
  "notice": "",                  // 平时空串。只在需要注意时给一句：已暂停 / 需要管理员权限才能读取磁盘寿命 / 读盘失败: …
  "range": { "key": "5m", "span": 300 },     // key ∈ 1m 5m 15m 1h 6h 24h 7d
  "totals": { "rateBps": 0, "todayBytes": 0, "activeAgents": 0 },
  "series": { "t": [/* ≤240 个时间点，升序 */], "byAgent": { "<agentId>": [/* 与 t 对齐的 bytes/s */] } },
  "agents": [{
    "id": "z-code", "name": "Z Code", "color": "#7c9cff", "isIde": false,
    "live": true, "rateBps": 0, "rangeBytes": 0, "lifetimeBytes": 0, "procs": 33,
    "processes": null            // 仅在该 id 处于展开状态时给数组，否则 null
    // processes 元素: { "pid": 1, "exe": "node.exe", "live": true, "writeBytes": 0, "exitedAt": null }
  }],
  "disks": [{
    "id": "1", "name": "INTEL SSDPEKNW010T8", "media": "NVMe", "sizeBytes": 0,
    "remainingPct": 63,          // 100 - wear；读不到 = null
    "rangeWearPct": 0.000061,    // 本区间寿命下降估算（百分点）；无法估算 = null
    "perDayPct": 0.017,          // rangeWearPct 折算到每天；null 同上
    "writtenBytes": 0, "rangeWrittenBytes": 0,
    "tempC": 51, "powerOnHours": 21880, "spare": 100, "mediaErrors": 0,
    "health": "ok",              // ok / warn / crit / unknown
    "curve": { "t": [], "wear": [] }   // 区间内累计寿命下降曲线（≤120 点，从 0 起单调不减）；无数据 = null
  }]
}
```

### 字段口径（从旧 `desktop.py` 移植，语义不变）

- `totals.rateBps` = `snap.total_bps()`；`todayBytes` = `store.sum_since(今日零点)` 求和；`activeAgents` = 有存活进程且非 IDE（除非开了显示 IDE）的 agent 数。
- `agents`：照搬旧 `_refresh_tree` 的并集逻辑（本次运行活跃的 + 数据库里有历史的已停止 agent）。`rangeBytes` = `store.sum_since(now - span).get(id, agg.dw)`；`lifetimeBytes` = `max(agg.lifetime_write, history.write_bytes)`；`procs` = 存活进程数。IDE 规则按 `showIde` 过滤。
- `processes`：照搬旧展开逻辑（存活进程 + 内存里的已退出 + 数据库里的已退出，合计 ≤ 40 条，按写入量降序）。
- `series`：内存历史 + 数据库历史（旧 `_load_history_from_db` / `_push_history` 的口径），重采样到 ≤ 240 点覆盖整个区间，`byAgent` 每条数组与 `t` 等长。
- `disks.health`：`wear >= 80` → crit；`>= 60` → warn；其余 ok；wear 读不到 → unknown（与旧 `_disk_color` 同阈值）。
- **寿命下降估算**（核心，务必按此实现并写单测）：
  - `bytesPerPct = data_written_bytes / wear`，仅当 `wear >= 1` 且 `data_written_bytes > 0` 时成立。剩余寿命 100%（`wear == 0`）时 `rangeWearPct = perDayPct = 0`、曲线为 0 线（不显示「—」）；读不到寿命（机械盘等）时 `rangeWearPct = perDayPct = curve = null`。
  - 磁盘写入历史 = 内存里每秒的 `snap.disk_written[device_id]` + 数据库 `disk_health` 表的 `written` 列（跨重启连续）。
  - 区间起点值 = 区间开始后第一个点；`rangeWrittenBytes = 最新值 - 起点值`（负数按 0）。
  - `rangeWearPct = rangeWrittenBytes / bytesPerPct`；`perDayPct = rangeWearPct / 实际覆盖秒数 * 86400`（覆盖秒数 = 最新点时间 - 起点时间，< 60 秒时 perDayPct 给 null）。
  - `curve.wear[i] = (written_i - 起点值) / bytesPerPct`。

## 后端动作（`js_api`，挂在 `window.pywebview.api`）

| 方法 | 行为 |
|---|---|
| `set_view(range_key, expanded_ids, show_ide)` | 更新视图参数并立即推一次快照 |
| `toggle_pause()` | 暂停/继续采样，立即推一次 |
| `set_interval(seconds)` | 采样间隔 0.5/1/2/5 |
| `export_csv(range_key)` | 弹系统「另存为」（pywebview `create_file_dialog(SAVE_DIALOG)`），内容同旧 `_export` |
| `reset()` | 系统确认框（pywebview `create_confirmation_dialog`）→ 先 `dataguard.backup_db()` 再清，同旧 `_reset` |
| `set_mode("mini"/"full")` | mini：记住当前窗口尺寸位置，缩到 360 宽 × 前端报的内容高度；full：恢复 |
| `set_on_top(bool)` | 窗口置顶 |

前端 `ui/app.js` 里调用 `window.hioSource.xxx` 的地方，改成在 pywebview 下映射到上表方法（写一个小的 `ui/bridge.js` 实现 `window.hioSource` 接口即可，`app.js` 渲染代码不动）。

## 外壳要求（`app.py`）

- 启动顺序沿用旧 `main()`：单实例锁 → 非管理员自动提权一次（`smart.elevate_and_run`，**提权重启用 pythonw.exe，不能用 python.exe**，否则会冒控制台窗口）→ 起 Sampler → 开窗口。
- 窗口：标题 `Harness I/O`，默认 1180×720，最小 360×160；背景色与 `--bg` 一致（`#0e0f11`），避免白屏闪烁；Windows 深色标题栏（`DwmSetWindowAttribute` 属性 20）。
- 关闭按钮 = 收进托盘（窗口 hide），托盘左键/「显示主窗口」恢复，托盘「退出」= flush + 停采样 + 关库 + 退出。
- 托盘：复用 `core/tray.py`，但它原来靠 tkinter 主循环泵消息；现在要**在独立线程里创建窗口并跑自己的 GetMessage 循环**，回调里切回 pywebview 窗口操作。
- `launch.bat` 继续用 pythonw；`install_shortcut.py` 指向 `app.py`；README 更新：依赖改为 `pip install pywebview`，删掉「零依赖」说法，更新截图示意与说明。

## 免授权启动（第二期，B 完工后施工）

用户诉求：每次打开都弹 UAC 体验很差；**但不要开机自启**。

机制：
1. 主程序（已提权运行时）启动后检查任务计划程序里有没有任务 `HarnessIOMonitor`，且其动作仍指向**当前**的 `pythonw.exe` + `app.py` 绝对路径；没有或路径变了 → 静默（重新）登记。登记要求：
   - **无任何触发器**（不按时间、不按登录、不按开机）——只能被手动 run；
   - 运行级别 = 最高权限；仅在用户登录时运行（交互式，窗口出现在用户桌面）；
   - 动作 = `pythonw.exe "<repo>\app.py" --from-task`，起始目录 = 仓库根；
   - 允许按需运行；不限执行时长（关掉默认的 72 小时停止）；电池模式也照常运行；已在运行时新实例「不启动新实例」。
   - 登记用任务计划 COM（`Schedule.Service`）或 `schtasks.exe /create /xml`。若走 schtasks 子进程，必须 `creationflags=CREATE_NO_WINDOW`，且只在登记那一下调用，不得周期性调用。
2. 新启动器 `launcher.pyw`（由 pythonw 运行，快捷方式和 `launch.bat` 都指向它）：
   - 已有主程序实例 → 激活已有窗口（复用 `core/single.py` 的锁/激活逻辑），退出；
   - 任务存在 → 触发运行该任务（COM `RegisteredTask.Run` 或 `schtasks /run /tn HarnessIOMonitor` + `CREATE_NO_WINDOW`），退出。**全程无窗口、无 UAC**；
   - 任务不存在 / 触发失败 → 退回原路径：直接启动 `app.py`（它会自提权，弹一次 UAC，提权后再登记任务）。
3. 菜单「更多」里加一项「免授权启动」开关（勾选=任务存在）。取消 = 删除任务；再勾 = 重新登记（当前已提权，可直接做）。这是唯一新增的界面控件，样式沿用现有 `.menu-item` 复选框，与「显示 IDE 进程」同款。
4. `--from-task` 启动的实例跳过自提权逻辑（本来就是最高权限）。
5. 卸载说明写进 README：菜单取消勾选，或在任务计划程序里删除 `HarnessIOMonitor`。

验收：登记后，从启动器启动 → 不出现 UAC、不出现任何控制台/黑窗、主程序 `is_admin()` 为真；任务属性里触发器列表为空。

## 不准做

- 不准在运行期间启动任何子进程（验收会全局搜 `subprocess` / `Popen` / `os.system` / `powershell`）。
- 不准改设计稿的视觉与交互；不准加新控件。
- 不准做开机自启 / 系统服务；计划任务只允许上面「免授权启动」那一个无触发器的按需任务。
- 不准用脚本批量改源码，逐处编辑。
- 不准 git commit（由主会话统一提交）。
