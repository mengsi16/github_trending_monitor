<#
.SYNOPSIS
    GitHub Trending Monitor 看门狗启动脚本。

.DESCRIPTION
    - 将 Python 主进程的 stdin 重定向到 NUL，触发 HEADLESS_KEEP_ALIVE 分支，
      不再阻塞在 input() 上。
    - 子进程死亡后（任何退出码）自动重启，采用指数退避避免狂飙。
    - 识别退出码 42：由 src/tools/memory_watchdog.py 主动触发的重启，
      立即以最短退避重启。
    - 识别 workspace/.restart 哨兵文件：由内存超限时写入，记录到
      watchdog.log 供事后排查。
    - 退出码 0（手动 /quit 或 Ctrl+C 正常退出）不再重启，看门狗结束。

.PARAMETER MaxBackoffSec
    最大重启等待秒数。默认 300（5 分钟）。

.PARAMETER InitialBackoffSec
    首次失败后的等待秒数。默认 10。

.PARAMETER PythonPath
    指定 Python 可执行文件路径。默认使用 PATH 中的 python。

.EXAMPLE
    # 前台运行，关闭 PowerShell 窗口会一起停
    powershell -ExecutionPolicy Bypass -File scripts/run_with_watchdog.ps1

.EXAMPLE
    # 后台运行，窗口关了不死（需要独立 PowerShell 窗口）
    Start-Process powershell -ArgumentList '-ExecutionPolicy','Bypass',
        '-File','scripts/run_with_watchdog.ps1' -WindowStyle Hidden
#>
[CmdletBinding()]
param(
    [int]$MaxBackoffSec = 300,
    [int]$InitialBackoffSec = 10,
    [string]$PythonPath = "python"
)

$ErrorActionPreference = "Stop"

# 项目根目录 = 脚本所在目录的上一级
$ProjectDir = Split-Path -Parent $PSScriptRoot
if (-not $ProjectDir -or -not (Test-Path $ProjectDir)) {
    Write-Error "无法定位项目根目录 (PSScriptRoot=$PSScriptRoot)"
    exit 1
}

$LogDir          = Join-Path $ProjectDir "workspace\.logs"
$WdLog           = Join-Path $LogDir "watchdog.log"
$StdoutLog       = Join-Path $LogDir "stdout.log"
$StderrLog       = Join-Path $LogDir "stderr.log"
$RestartSentinel = Join-Path $ProjectDir "workspace\.restart"
# PowerShell 的 Start-Process -RedirectStandardInput 不识别 Windows 的 NUL 设备名，
# 改用一个真实存在的空文件作为 stdin，Python 读到即 EOF，同样触发 headless 分支。
$EmptyStdin      = Join-Path $LogDir ".empty_stdin"

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
# 总是重建为 0 字节文件（跨 PowerShell 5.x / 7.x 都成立），避免残留内容被当成 stdin 输入
[System.IO.File]::WriteAllBytes($EmptyStdin, [byte[]]@())

function Write-WatchdogLog {
    param([string]$Message)
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $line = "[$ts] $Message"
    Add-Content -Path $WdLog -Value $line -Encoding UTF8
    Write-Host $line
}

function Get-NextBackoff {
    param([int]$Current, [int]$Exit)
    if ($Exit -eq 42) {
        # 内存超限触发的重启：保持短间隔
        return $InitialBackoffSec
    }
    $next = $Current * 2
    if ($next -gt $MaxBackoffSec) { return $MaxBackoffSec }
    if ($next -lt $InitialBackoffSec) { return $InitialBackoffSec }
    return $next
}

Write-WatchdogLog "=== watchdog started project=$ProjectDir python=$PythonPath ==="
Write-WatchdogLog "    MaxBackoffSec=$MaxBackoffSec InitialBackoffSec=$InitialBackoffSec"

$backoff = $InitialBackoffSec
$consecutiveQuick = 0

while ($true) {
    # 清理上一轮可能遗留的哨兵文件
    if (Test-Path $RestartSentinel) {
        try {
            $sentinelContent = Get-Content $RestartSentinel -Raw -ErrorAction SilentlyContinue
            if ($sentinelContent) {
                Write-WatchdogLog "发现 restart sentinel: $($sentinelContent.Trim())"
            }
            Remove-Item $RestartSentinel -Force
        } catch {
            Write-WatchdogLog "清理 sentinel 失败: $_"
        }
    }

    Write-WatchdogLog "启动 python src/main.py (stdin=empty_file)"
    $started = Get-Date

    try {
        # -u 关闭 stdout/stderr 缓冲，避免日志卡在 4KB 块缓冲里不落盘
        $proc = Start-Process -FilePath $PythonPath `
            -ArgumentList @("-u", "src/main.py") `
            -WorkingDirectory $ProjectDir `
            -RedirectStandardInput $EmptyStdin `
            -RedirectStandardOutput $StdoutLog `
            -RedirectStandardError  $StderrLog `
            -PassThru -NoNewWindow
    } catch {
        Write-WatchdogLog "Start-Process 失败: $_"
        Start-Sleep -Seconds $backoff
        $backoff = Get-NextBackoff -Current $backoff -Exit -1
        continue
    }

    Wait-Process -Id $proc.Id
    $code = $proc.ExitCode
    $uptime = (Get-Date) - $started
    $uptimeStr = "{0:hh\:mm\:ss}" -f $uptime

    # 看哨兵
    $reason = "exit=$code uptime=$uptimeStr"
    if (Test-Path $RestartSentinel) {
        $sentinel = (Get-Content $RestartSentinel -Raw -ErrorAction SilentlyContinue).Trim()
        $reason += " sentinel='$sentinel'"
    }

    if ($code -eq 0) {
        Write-WatchdogLog "python 正常退出 $reason，看门狗停止"
        break
    }

    # 连续快速失败计数：若 uptime < 60s，视为"启动即死"，递增退避
    if ($uptime.TotalSeconds -lt 60) {
        $consecutiveQuick++
    } else {
        $consecutiveQuick = 0
        $backoff = $InitialBackoffSec  # 跑得久说明稳定，重置退避
    }

    $next = Get-NextBackoff -Current $backoff -Exit $code
    Write-WatchdogLog "python 异常退出 $reason，${next}s 后重启 (quick=$consecutiveQuick)"

    # 连续 10 次快速崩溃，写一条响亮的告警但仍继续
    if ($consecutiveQuick -ge 10) {
        Write-WatchdogLog "!!! 警告：已连续 10 次启动后 <60s 即死，请检查 src/main.py 或依赖"
    }

    Start-Sleep -Seconds $next
    $backoff = $next
}

Write-WatchdogLog "=== watchdog stopped ==="
