# Harness I/O Monitor

**按 AI agent 客户端统计磁盘读写的 Windows 桌面程序。**

Windows 任务管理器只能告诉你「某个 node.exe 在写盘」。但当你同时跑着 Claude Code、Codex、
Cursor、Z Code、Qoder 时，你真正想知道的是 **哪个 agent 写了多少** —— 这个工具回答的就是这个问题。

它把散落在几十个进程里的 I/O 归到所属的 agent 名下，实时显示，并持久化历史。

```
┌─ Harness I/O Monitor ──────────────────────── 今日 12.4 GB ──┐
│  写入速率  38 MB/s    下次落库 14:32    采样 34ms             │
├───────────────────────────────────────────────────────────────┤
│  Harness            速率      生命周期累计  区间增量  进程     │
│  Z Code           38 MB/s      642.7 GB    1.2 GB   53       │
│  Claude Code       0 B/s        31.7 GB     210 MB  80       │
│  Qoder            12 MB/s        8.0 GB      53 MB  19       │
│  Claude Desktop    0 B/s         7.3 GB       3 MB  22       │
│    ● python.exe #90536                    8.0 MB             │
│    ○ python.exe #81548   已退出 20:52     8.0 MB             │
├───────────────────────────────────────────────────────────────┤
│  [暂停]  间隔 1s▾   区间 24h▾   ☐显示IDE   [导出CSV] [清除]   │
└───────────────────────────────────────────────────────────────┘
```

---

## 为什么需要它

现有工具都不解决这个问题：

| 工具 | 局限 |
|---|---|
| 任务管理器 / Process Explorer | 按 PID 显示。一个 agent 常有几十个进程，要自己加 |
| iotop / ETW 类工具 | 同样按进程，不认 agent |
| 各种 agent 用量统计工具 | 统计 token 和成本，不统计磁盘 |

而且 Electron 类 agent（Claude / ChatGPT / Qoder / OpenCode Desktop）的写入分散在
主进程 + 多个 renderer / gpu / utility 子进程里，必须按进程树归集才有意义。

---

## 特性

- **按 agent 归集** — 进程树继承，Electron 的几十个子进程全部归到主应用
- **精确计数** — 用内核 `GetProcessIoCounters`，是进程生命周期真实累计值，不靠速率积分
- **能识别 dev 版本** — 自动解析 junction / symlink，按仓库根目录匹配从源码运行的 agent
- **已退出进程保留** — 标 `○` 灰色显示，退出后仍留一行，能看到它最终写了多少
- **历史持久化** — SQLite。内存持续更新、20 分钟落一次库、关闭时强制 flush
- **界面** — pywebview 外壳 + 纯 HTML/CSS/JS 设计稿（Windows 下走 Edge WebView2），支持极简模式与磁盘寿命面板
- **依赖** — 标准库之外只需 `pip install pywebview`
- **不联网** — 不发送任何数据

---

## 快速开始

**要求**：Windows 10/11 + Python 3.9+

```bash
git clone <repo-url>
cd harness-io-monitor

pip install pywebview  # 1. 界面依赖（Windows 自带 Edge WebView2，无需另装）

python probe.py        # 2. 探测本机装了哪些 agent
python app.py         # 3. 启动
```

或者双击 `launch.bat`（经 pythonw 运行启动器 `launcher.pyw`，无控制台窗口）。

创建桌面快捷方式（指向 `launcher.pyw`，经 pythonw 启动）：

```bash
python install_shortcut.py
```

关闭窗口 = 收进托盘继续采样；托盘右键「退出」才真正退出。

> 管理员权限不是必需的，但用管理员运行能看到更多系统进程，也才能读磁盘寿命。非管理员启动时程序会请求提权一次。

### 免授权启动（不再每次弹 UAC）

第一次启动时会弹一次 UAC。提权成功后，程序在「任务计划程序」里登记一个按需任务 `HarnessIOMonitor`：

- **没有任何触发器**：不按时间、不在登录或开机时运行，**不会开机自启**，只能被启动器手动触发；
- 以最高权限、仅在你登录时交互运行，动作是 `pythonw.exe "<仓库>\app.py" --from-task`；
- 不限运行时长，用电池时照常运行，已在运行时不会再开新实例。

之后从快捷方式或 `launch.bat` 启动时，启动器直接触发这个任务：不弹 UAC，也不出现黑窗。仓库搬了位置时，启动器会发现任务已经失效，改为直接启动（再弹一次 UAC），程序随后自动重新登记。

**关闭 / 卸载**：在菜单「更多」里取消勾选「免授权启动」，任务会被删除，之后也不会再自动登记，想恢复时重新勾选即可。也可以在任务计划程序里手动删除 `HarnessIOMonitor`，但只要程序再以管理员身份启动，就会把它重新登记回来；想永久关闭请用菜单开关。

---

## 支持哪些 agent

内置 18 条规则，开箱识别：

**桌面应用** — Z Code、Qoder、ChatGPT Desktop、Claude Desktop、OpenCode Desktop、
DeepSeek Harness、Antigravity

**CLI** — Claude Code、Codex CLI、Gemini CLI、Qwen Code、Kimi Code、Aider、Goose

**IDE 整体**（默认隐藏，避免淹没榜单）— Cursor、Windsurf、VS Code、Zed

### 加自己的

复制 `harnesses.example.json` 为 `harnesses.json` 后编辑：

```json
{
  "my-agent": {
    "display": "My Agent",
    "color": "#FF8800",
    "exe": ["myagent"],
    "path_re": ["\\\\Programs\\\\MyAgent\\\\"],
    "repo_paths": ["D:\\code\\my-agent"],
    "data_dirs": ["~/.myagent"]
  }
}
```

| 字段 | 说明 |
|---|---|
| `exe` | 可执行文件名（不含 .exe，小写） |
| `path_re` | 安装路径正则 |
| `cmd_re` | 命令行正则 |
| `repo_paths` | **源码仓库根目录**，dev 版本靠这个识别 |
| `inherit_tree` | 是否把子进程归属过来，默认 `true` |
| `show_by_default` | 是否默认显示 |
| `is_ide` | 标记为 IDE 整体（写入无法与 IDE 本身拆分） |

`__order__` 可调匹配优先级。**顺序很重要**：具体的规则要排前面，否则会被泛规则抢走
（例如 Claude Desktop 自带 CLI，`claude-code` 必须排在 `claude-desktop` 前面）。

---

## 原理

### 数据来源

Windows 的 `GetProcessIoCounters` 返回**进程自启动以来的累计读写字节数**。
这是真实累计值而非速率，所以精度不受采样间隔影响。

### 两个关键设计

**1. dev 版本靠 junction 还原**

开发版 agent 的入口可能是
`C:\...\AppData\Roaming\npm\node_modules\@deepseek-ai\dsh` 这样的 junction，
实际指向 `D:\code\deepseek-harness\apps\cli`，而进程命令行里只出现前者。

`core/pathreal.py` 用 ctypes 读 reparse point 还原真实路径，再按仓库根目录判定。
不解析就会漏掉整个 dev 树。

**2. shell 不是父链边界**

agent 常经由多层 bash 启动子进程（Claude Code 里跑 `node dist/server.js` 会过 3 层 bash）。
把 bash 当边界会让父链提前断开，整棵子树掉进「未归因」。

只有 GUI 启动器（explorer / 终端窗口）才是边界，shell 一律允许向上穿透。

### 持久化策略

| 时机 | 写什么 |
|---|---|
| 启动后 1.5 秒 | 基线：所有存活进程的 lifetime 累计 |
| 每 20 分钟 | 区间增量快照 |
| 关闭程序 | 强制 flush + 已退出进程的最终读数 |

内存持续每秒更新，磁盘低频写入。掉电最多丢 20 分钟的区间增量，基线不丢。

`proc_runs` 用 `(pid, create_time)` 做唯一键 —— 同一进程跨重启只 UPSERT 一行，
lifetime 不会重复累加。

### 两列数字的区别

| 列 | 含义 |
|---|---|
| **生命周期累计** | 该 agent 名下所有被观测进程，各自从启动到退出（或至今）写了多少 |
| **区间增量** | 选定时间窗内实际新增的写入 |

生命周期累计只能覆盖**被观测过的**进程。监控未运行时退出的进程，数字随进程消失而丢失
—— Windows 没有历史 I/O 查询 API。

---

## 精度验证

`verify.py` 用独立路径交叉验证同一个 64 MB 写入：

```
A. GetProcessIoCounters (整棵树)  64.00 MB
D. 文件实际大小                    64.00 MB
监控程序自身写入增量                0.0 KB
```

误差 0.00 MB。

`test_integration.py` 覆盖 5 个场景：

```
1. 单次 50MB 写入                    -> 50.0 MB    PASS
2. 启停 3 次各写 10MB                -> 30.0 MB    PASS
3. 退出发生在两次采样之间             ->  8.0 MB    PASS（已知限制）
4. 进程退出后区间累计不倒退           -> 12.0 MB    PASS
5. 生命周期短于采样间隔（已知限制）   -> 记录行为
```

---

## 命令

```bash
python probe.py                          # 探测本机 agent 与数据目录
python core/classify.py                  # 归因自检：各 agent 汇总
python core/classify.py --unmatched      # 列出未归因的高写入进程
python core/classify.py --trace 12345    # 追踪某 pid 的归因过程
python verify.py                         # 交叉验证计数精度
python test_integration.py               # 集成测试
```

---

## 文件结构

```
harness-io-monitor/
├── app.py                  主程序（pywebview 窗口 + 托盘 + 快照推送）
├── launcher.pyw            启动器（激活已有实例 / 触发免授权启动任务 / 退回直接启动）
├── probe.py                环境探测
├── verify.py               精度交叉验证
├── test_integration.py     集成测试
├── migrate_agents.py       辅助：把 agent 数据目录迁到别的盘（junction）
├── install_shortcut.py     创建桌面快捷方式
├── launch.bat              双击启动（pythonw → launcher.pyw）
├── harnesses.example.json  归因规则模板
├── ui/                     界面：设计稿 index.html / app.css / app.js，bridge.js（pywebview 桥），mock.js（浏览器预览用假数据）
├── tests/                  pytest 单测（test_snapshot / test_diskinfo / test_schtask）
└── core/
    ├── winproc.py          ctypes 进程枚举与 IO 计数器
    ├── pathreal.py         junction / symlink 真实路径还原
    ├── classify.py         归因规则
    ├── store.py            SQLite 持久化
    ├── sampler.py          采样循环
    ├── snapshot.py         快照构建（寿命下降估算、区间重采样，纯逻辑可单测）
    ├── smart.py / nvme.py  磁盘健康读取
    ├── tray.py             托盘图标（独立线程消息循环）
    ├── schtask.py          免授权启动的按需计划任务（任务计划 COM，ctypes）
    └── single.py           单实例锁 + 「显示主窗口」信号
```

---

## 已知限制

| 限制 | 说明 |
|---|---|
| **逻辑 I/O ≠ SSD 实际写入** | 计数器含页缓存命中。SSD 真实写入需看 SMART，两者可差 2–5 倍 |
| **采样间隔内完成的生命周期会漏** | 进程从启动到退出全程短于一个采样间隔（如 `rg.exe`）可能整段错过。降低间隔可缓解 |
| **退出前最后一段会漏** | 进程在两次采样之间退出时，它退出前写的那部分读不回来 |
| **无管理员权限时看不到受保护进程** | 界面会提示当前权限 |
| **只排除监控自身** | 避免自我污染。不排除启动它的父进程 |
| **IDE 内嵌 agent 无法拆分** | Cursor 里 agent 的写入与 IDE 索引写入同属一个进程树 |

### 为什么进程退出后读不到最终值

`GetProcessIoCounters` 需要有效进程句柄。进程对象被系统回收后，`OpenProcess`
可能因 PID 被复用而打开到**另一个**进程，读到的数字毫无意义。
所以已退出进程只能用最后一次被采样到的读数结算。

---

## 许可

MIT
