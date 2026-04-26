"""工作区文件与 Shell 工具

让 QA Agent 能读取本地文件、搜索内容、执行命令，
从而直接访问爬取的原始数据而不仅依赖 RAG 语义搜索。

安全策略：
  - 文件操作限定在项目根目录（WORKSPACE_ROOT）下
  - Bash 命令限定在项目根目录执行，可配置黑名单拦截危险命令
  - Edit-File 只允许修改 workspace/ 下的文件（防止破坏源码）
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
from pathlib import Path
from typing import List, Optional

_logger = logging.getLogger("workspace_tools")

# ── 沙箱边界 ────────────────────────────────────────────────────────────────
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_WORKSPACE_DIR = _PROJECT_ROOT / "workspace"
_PLAYWRIGHT_MCP_DIR = _PROJECT_ROOT / ".playwright-mcp"

# edit_file 允许修改的目录白名单
_EDIT_ALLOWED_DIRS = [_WORKSPACE_DIR, _PLAYWRIGHT_MCP_DIR]

# 跨平台 Shell 配置
_IS_WINDOWS = os.name == "nt"

# Bash 命令黑名单前缀（禁止 rm、格式化、提权等危险操作）
_BASH_DENY_PREFIXES = (
    "rm ", "rmdir", "del ", "format", "mkfs",
    "sudo", "runas", "shutdown", "reboot",
    "curl ", "wget ",  # 防止 SSRF；如需网络访问用 Playwright
    "pip install", "npm install",
    "python -c", "python3 -c",  # 防止任意代码注入
)


def _safe_path(path_str: str) -> Path:
    """将用户输入路径解析为绝对路径，并验证不越界。"""
    p = Path(path_str)
    if not p.is_absolute():
        p = _PROJECT_ROOT / p
    p = p.resolve()
    # 必须在项目根目录下
    if not str(p).startswith(str(_PROJECT_ROOT)):
        raise PermissionError(f"路径越界: {p} 不在项目根目录下")
    return p


def _safe_edit_path(path_str: str) -> Path:
    """Edit-File 更严格：只允许修改白名单目录下的文件。"""
    p = Path(path_str)
    if not p.is_absolute():
        # 尝试按白名单顺序解析
        for allowed_dir in _EDIT_ALLOWED_DIRS:
            candidate = allowed_dir / p
            if candidate.exists():
                p = candidate
                break
        else:
            p = _WORKSPACE_DIR / p
    p = p.resolve()
    for allowed_dir in _EDIT_ALLOWED_DIRS:
        if str(p).startswith(str(allowed_dir)):
            return p
    allowed_names = ", ".join(d.name for d in _EDIT_ALLOWED_DIRS)
    raise PermissionError(f"Edit-File 只允许修改 {allowed_names} 下的文件，拒绝: {p}")


# ── 工具实现 ─────────────────────────────────────────────────────────────────

def tool_read_file(path: str, offset: int = 0, limit: int = 200) -> str:
    """读取项目目录下的文件内容

    Args:
        path: 文件路径（相对于项目根目录或绝对路径）
        offset: 起始行号（0-indexed，默认从开头）
        limit: 最多读取行数（默认 200）
    """
    try:
        p = _safe_path(path)
    except PermissionError as e:
        return str(e)

    if not p.exists():
        return f"文件不存在: {path}"
    if p.is_dir():
        return f"路径是目录: {path}"

    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        total = len(lines)
        selected = lines[offset: offset + limit]
        numbered = [f"{i + 1}: {line}" for i, line in enumerate(selected, start=offset)]
        header = f"文件: {p.relative_to(_PROJECT_ROOT)} ({total} 行, 显示 {offset}-{offset + len(selected) - 1})"
        return header + "\n" + "\n".join(numbered)
    except Exception as e:
        return f"读取失败: {e}"


def tool_list_dir(path: str = ".", depth: int = 1) -> str:
    """列出目录内容

    Args:
        path: 目录路径（相对于项目根目录，默认 '.')
        depth: 递归深度（默认 1，只列当前层）
    """
    try:
        p = _safe_path(path)
    except PermissionError as e:
        return str(e)

    if not p.exists():
        return f"目录不存在: {path}"
    if not p.is_dir():
        return f"路径不是目录: {path}"

    lines = [f"目录: {p.relative_to(_PROJECT_ROOT)}"]
    try:
        for entry in sorted(p.rglob("*") if depth > 1 else p.iterdir()):
            if depth > 1:
                rel = entry.relative_to(p)
                if len(rel.parts) > depth:
                    continue
            if entry.is_dir():
                lines.append(f"  📁 {entry.name}/")
            else:
                size = entry.stat().st_size
                if size > 1024 * 1024:
                    size_str = f"{size / 1024 / 1024:.1f}MB"
                elif size > 1024:
                    size_str = f"{size / 1024:.1f}KB"
                else:
                    size_str = f"{size}B"
                lines.append(f"  📄 {entry.name} ({size_str})")
    except PermissionError:
        lines.append("  (权限不足，部分内容无法读取)")
    except Exception as e:
        lines.append(f"  (列出失败: {e})")

    return "\n".join(lines[:100])  # 最多 100 行


def tool_grep(pattern: str, path: str = ".", include: str = "*",
              case_insensitive: bool = True, max_matches: int = 50) -> str:
    """在文件中搜索文本模式

    Args:
        pattern: 搜索模式（支持正则表达式）
        path: 搜索目录（相对于项目根目录，默认 '.'）
        include: 文件名 glob 过滤（如 '*.py', '*.json'，默认 '*'）
        case_insensitive: 是否忽略大小写（默认 true）
        max_matches: 最多返回匹配数（默认 50）
    """
    try:
        p = _safe_path(path)
    except PermissionError as e:
        return str(e)

    if not p.exists():
        return f"路径不存在: {path}"

    flags = re.IGNORECASE if case_insensitive else 0
    try:
        regex = re.compile(pattern, flags)
    except re.error as e:
        return f"正则表达式无效: {e}"

    matches = []
    search_files = p.rglob(include) if p.is_dir() else [p]

    for fp in search_files:
        if not fp.is_file():
            continue
        # 跳过二进制文件和大文件
        try:
            if fp.stat().st_size > 5 * 1024 * 1024:  # 5MB
                continue
        except OSError:
            continue

        try:
            text = fp.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

        for i, line in enumerate(text.splitlines(), 1):
            if regex.search(line):
                rel = fp.relative_to(_PROJECT_ROOT) if fp.is_relative_to(_PROJECT_ROOT) else fp
                matches.append(f"{rel}:{i}: {line.strip()}")
                if len(matches) >= max_matches:
                    break
        if len(matches) >= max_matches:
            break

    if not matches:
        return f"未找到匹配: pattern='{pattern}' path={path}"
    return f"找到 {len(matches)} 处匹配:\n" + "\n".join(matches)


def _resolve_shell() -> list:
    """解析当前平台可用的最佳 Shell。

    Windows: 优先 pwsh (PowerShell 7+)，回退 powershell (Windows PowerShell)
    Linux/macOS: /bin/bash，回退 /bin/sh
    """
    if _IS_WINDOWS:
        import shutil
        for name in ("pwsh", "powershell"):
            if shutil.which(name):
                return [name, "-NoProfile", "-Command"]
        # 兜底 cmd
        return ["cmd", "/C"]
    else:
        for shell in ("/bin/bash", "/bin/sh"):
            if Path(shell).exists():
                return [shell, "-c"]
        return ["/bin/sh", "-c"]


_SHELL_CMD = _resolve_shell()
_SHELL_NAME = _SHELL_CMD[0]


def tool_bash(command: str, timeout: int = 30) -> str:
    """在项目根目录执行 Shell 命令

    Windows 上使用 PowerShell，Linux/macOS 上使用 Bash。
    危险命令（rm、sudo、pip install 等）会被拦截。

    Args:
        command: 要执行的命令
        timeout: 超时秒数（默认 30）
    """
    # 安全检查
    cmd_lower = command.strip().lower()
    for prefix in _BASH_DENY_PREFIXES:
        if cmd_lower.startswith(prefix):
            return f"命令被拒绝: '{prefix.strip()}' 类操作不允许"

    # 额外拦截管道中的危险命令
    dangerous_in_pipe = ["rm ", "del ", "sudo", "format", "shutdown",
                        "remove-item", "invoke-expression"]
    for d in dangerous_in_pipe:
        if d in cmd_lower and ("|" in cmd_lower or ";" in cmd_lower):
            return f"命令被拒绝: 管道/串联中包含危险操作 '{d.strip()}'"

    # PowerShell 特有危险命令拦截
    ps_dangerous = ["invoke-expression", "iex ", "remove-item",
                    "stop-process", "set-executionpolicy"]
    if _IS_WINDOWS:
        for d in ps_dangerous:
            if d in cmd_lower:
                return f"命令被拒绝: PowerShell 危险操作 '{d.strip()}'"

    try:
        result = subprocess.run(
            _SHELL_CMD + [command],
            cwd=str(_PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
        output = []
        if result.stdout:
            output.append(result.stdout[:5000])
        if result.stderr:
            output.append(f"[stderr]\n{result.stderr[:2000]}")
        if result.returncode != 0:
            output.append(f"[exit code: {result.returncode}]")

        combined = "\n".join(output) if output else "(无输出)"
        return combined
    except subprocess.TimeoutExpired:
        return f"命令超时 ({timeout}s): {command}"
    except Exception as e:
        return f"执行失败: {e}"


def tool_edit_file(path: str, old_string: str, new_string: str) -> str:
    """编辑 workspace/ 下的文件（查找替换）

    只允许修改 workspace/ 目录下的文件，防止破坏源码。

    Args:
        path: 文件路径（相对于 workspace/ 目录或绝对路径）
        old_string: 要替换的文本
        new_string: 替换后的文本
    """
    try:
        p = _safe_edit_path(path)
    except PermissionError as e:
        return str(e)

    if not p.exists():
        return f"文件不存在: {path}"

    try:
        content = p.read_text(encoding="utf-8")
    except Exception as e:
        return f"读取失败: {e}"

    if old_string not in content:
        return f"未找到要替换的文本"

    count = content.count(old_string)
    if count > 1:
        return f"找到 {count} 处匹配，为避免误改请提供更精确的上下文"

    new_content = content.replace(old_string, new_string, 1)
    p.write_text(new_content, encoding="utf-8")
    return f"已替换 1 处 (文件: {p.relative_to(_WORKSPACE_DIR)})"


# ── Tool Schema 定义 ─────────────────────────────────────────────────────────

WORKSPACE_TOOLS = [
    {
        "name": "read_file",
        "description": (
            "读取项目目录下的文件内容。可用于查看爬取的原始数据、"
            "日志、配置文件等。支持指定行号范围。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件路径（相对项目根目录或绝对路径）"},
                "offset": {"type": "integer", "description": "起始行号（0-indexed），默认 0", "default": 0},
                "limit": {"type": "integer", "description": "最多读取行数，默认 200", "default": 200},
            },
            "required": ["path"],
        },
    },
    {
        "name": "list_dir",
        "description": (
            "列出目录内容，查看项目下有哪些文件和子目录。"
            "用于发现可读取的文件。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "目录路径（相对项目根目录，默认 '.'）", "default": "."},
                "depth": {"type": "integer", "description": "递归深度（默认 1）", "default": 1},
            },
            "required": [],
        },
    },
    {
        "name": "grep",
        "description": (
            "在文件中搜索文本模式（支持正则表达式）。"
            "用于在爬取数据、日志等文件中查找特定内容。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "搜索模式（支持正则）"},
                "path": {"type": "string", "description": "搜索目录（相对项目根目录，默认 '.'）", "default": "."},
                "include": {"type": "string", "description": "文件名 glob 过滤（如 '*.json'），默认 '*'", "default": "*"},
                "case_insensitive": {"type": "boolean", "description": "忽略大小写，默认 true", "default": True},
                "max_matches": {"type": "integer", "description": "最多返回匹配数，默认 50", "default": 50},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "bash",
        "description": (
            f"在项目根目录执行 Shell 命令 (当前 Shell: {_SHELL_NAME})。"
            "可用于运行脚本、查看进程、处理数据等。"
            "危险命令（rm、sudo、pip install 等）会被拦截。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "要执行的 Shell 命令"},
                "timeout": {"type": "integer", "description": "超时秒数，默认 30", "default": 30},
            },
            "required": ["command"],
        },
    },
    {
        "name": "edit_file",
        "description": (
            "编辑 workspace/ 或 .playwright-mcp/ 目录下的文件（查找替换）。"
            "只允许修改这些目录下的文件，防止破坏源码。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件路径（相对 workspace/ 目录或绝对路径）"},
                "old_string": {"type": "string", "description": "要替换的文本"},
                "new_string": {"type": "string", "description": "替换后的文本"},
            },
            "required": ["path", "old_string", "new_string"],
        },
    },
]

WORKSPACE_HANDLERS = {
    "read_file": tool_read_file,
    "list_dir": tool_list_dir,
    "grep": tool_grep,
    "bash": tool_bash,
    "edit_file": tool_edit_file,
}
