# 运维脚本

本目录放置项目的运维 / 守护辅助脚本。**不会影响**项目核心模块。

## `run_with_watchdog.ps1` - 守护启动脚本

把 `python src/main.py` 跑在一个**自动重启的看门狗**里，用于长期无人值守运行。

### 为什么需要它

`src/main.py` 本身是 REPL，长期运行时曾发生过两类问题：

1. **被 Windows 以 OOM（commit limit exceeded）干掉** —— 不走 Python 异常，没 Traceback。
2. **PowerShell 窗口关闭/休眠/登出导致 stdin 异常** —— `input()` 抛 `EOFError`，主循环 `break`。

看门狗脚本把 stdin 重定向到 `NUL`，触发 `main.py` 里的 headless 分支；子进程死后 10s 自动拉起。

### 一次性准备

1. 确保装好依赖（会用到 `psutil` 驱动内存 watchdog）：

   ```powershell
   pip install -r requirements.txt
   ```

2. 按需调节环境变量（在 `.env` 或会话里 `$env:` 设置）：

   | 变量 | 默认值 | 说明 |
   | --- | --- | --- |
   | `HEADLESS_KEEP_ALIVE` | `true` | 非 tty stdin 场景保持后台常驻，**必须为 true** |
   | `MEMWD_ENABLED` | `true` | 是否启动内存监控线程 |
   | `MEMWD_INTERVAL_SEC` | `300` | 采样间隔（秒），最小 10 |
   | `MEMWD_SOFT_RSS_MB` | `1500` | RSS 超过此值开始采 tracemalloc 快照 |
   | `MEMWD_HARD_RSS_MB` | `3000` | RSS 超过此值主动退出（交给看门狗重启）|
   | `MEMWD_TRACE_TOP_N` | `30` | tracemalloc 快照的 top N |
   | `MEMWD_RESTART_EXIT_CODE` | `42` | 内存触发重启时的退出码 |

### 运行方式

**前台跑（推荐，日常用）：** 关闭 PowerShell 窗口会一起停。

```powershell
powershell -ExecutionPolicy Bypass -File scripts\run_with_watchdog.ps1
```

**后台跑：** 开一个隐藏的 PowerShell，关当前窗口也不影响。

```powershell
Start-Process powershell `
    -ArgumentList '-ExecutionPolicy','Bypass','-File','scripts\run_with_watchdog.ps1' `
    -WindowStyle Hidden
```

**想让机器重启后自动拉起：** 用 Windows 任务计划程序，"触发器 = 登录时/启动时"，"操作 = 运行上面那条 Start-Process"。

### 日志位置

| 文件 | 内容 |
| --- | --- |
| `workspace/.logs/watchdog.log` | 看门狗自身：启动、重启、退出码、哨兵文件内容 |
| `workspace/.logs/stdout.log` | Python 进程的标准输出（每次重启会覆盖） |
| `workspace/.logs/stderr.log` | Python 进程的标准错误（每次重启会覆盖） |
| `workspace/.logs/app.log` | 应用自身日志（追加写入） |
| `workspace/.logs/memory.log` | 内存 watchdog 的周期采样：RSS/VMS/线程/句柄 |
| `workspace/.logs/tracemalloc/trace-*.txt` | RSS 过高时的 Python 对象分配 top 30 |
| `workspace/.restart` | 内存超限时的哨兵文件，看门狗读一次就清理 |

### 脚本参数

```powershell
# 最长退避 60s、首轮退避 5s
powershell -File scripts\run_with_watchdog.ps1 -MaxBackoffSec 60 -InitialBackoffSec 5

# 指定 Python 路径
powershell -File scripts\run_with_watchdog.ps1 -PythonPath "C:\ProgramData\miniconda3\python.exe"
```

退避策略：
- 首次崩溃后等 `InitialBackoffSec` 秒（默认 10）。
- 继续崩溃则 `×2`，封顶 `MaxBackoffSec`。
- 进程跑满 60s 以上视为稳定，重置退避。
- 退出码为 `42`（内存 watchdog 主动退出）时，**始终用最短退避**。
- 退出码 `0`（手动 `/quit`）不再重启，看门狗结束。

### 排查常见问题

**看门狗启动后立刻循环退出？**
看 `workspace/.logs/stderr.log`，多半是依赖没装齐（psutil / chromadb / anthropic）。

**进程跑着跑着被内存 watchdog 杀了（退出码 42）？**
看同一时段的 `workspace/.logs/memory.log` 末尾几行 + `workspace/.logs/tracemalloc/` 最新的 `trace-*.txt`。
里面是按来源文件:行号排序的内存占用 top，能指向真正的泄漏点。

**想临时关掉内存 watchdog？**
```powershell
$env:MEMWD_ENABLED="false"
```
然后重启看门狗。

**想只观察、不自动重启？**
调大 `MEMWD_HARD_RSS_MB`（例如 `100000`），软阈值仍生效，只采快照不 kill。
