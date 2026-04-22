"""MCP (Model Context Protocol) 客户端工具加载器

通过 stdio 子进程协议连接 MCP 服务器，将其工具动态加载为
标准 (TOOLS, HANDLERS) 格式，与现有 Agent Harness 无缝集成。

支持的 MCP 服务器示例:
  - @playwright/mcp: 浏览器自动化
  - @modelcontextprotocol/server-filesystem: 文件系统访问
  - 任何符合 MCP 标准的服务器

配置方式:
  1. 在项目根目录放置 mcp_servers.json
  2. 或设置环境变量 MCP_CONFIG_PATH 指向配置文件路径

mcp_servers.json 示例:
  {
    "mcpServers": {
      "playwright": {
        "command": "npx",
        "args": ["@playwright/mcp@latest"],
        "env": {}
      }
    }
  }
"""

import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_logger = logging.getLogger(__name__)

# ── 配置文件搜索路径 ──────────────────────────────────────────────────────────
_DEFAULT_CONFIG_NAMES = ["mcp_servers.json", ".mcp.json"]
_PROJECT_ROOT = Path(__file__).parent.parent.parent  # src/tools/mcp.py -> project root


def _find_config_path() -> Optional[Path]:
    """查找 MCP 配置文件"""
    # 优先使用环境变量
    env_path = os.getenv("MCP_CONFIG_PATH")
    if env_path:
        p = Path(env_path)
        return p if p.exists() else None

    # 从项目根目录搜索
    for name in _DEFAULT_CONFIG_NAMES:
        p = _PROJECT_ROOT / name
        if p.exists():
            return p
    return None


def _load_config() -> Dict[str, Any]:
    """加载 MCP 配置文件，返回 mcpServers 字典"""
    path = _find_config_path()
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("mcpServers", {})
    except Exception as e:
        _logger.warning(f"[MCP] 无法读取配置文件 {path}: {e}")
        return {}


# ── MCP stdio 子进程客户端 ────────────────────────────────────────────────────

class MCPClient:
    """轻量级 MCP stdio 客户端（JSON-RPC 2.0）

    生命周期: start() → call() → stop()
    线程安全: 使用 Lock 保护请求/响应序列号
    """

    def __init__(self, name: str, command: str, args: List[str],
                 env: Dict[str, str] = None, timeout: float = 30.0):
        self.name = name
        self.command = command
        self.args = args
        self.env = env or {}
        self.timeout = timeout

        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._next_id = 1
        self._started = False

    # ── 进程管理 ──────────────────────────────────────────────────────────────

    @staticmethod
    def _resolve_command(command: str) -> str:
        """在 Windows 上将 npx/npm/node 等解析为完整可执行路径。
        Windows 的 Popen 无法直接执行 .CMD 文件，需要传入完整路径。
        """
        if os.name != "nt":
            return command
        import shutil
        # 先尝试原始名称（可能已带扩展名或是绝对路径）
        resolved = shutil.which(command)
        if resolved:
            return resolved
        # 再尝试加 .cmd 后缀（Windows Node.js 工具链惯例）
        resolved = shutil.which(command + ".cmd")
        if resolved:
            return resolved
        return command

    def start(self) -> bool:
        """启动 MCP 服务器子进程，返回是否成功"""
        if self._started:
            return True
        try:
            merged_env = {**os.environ, **self.env}
            resolved_cmd = self._resolve_command(self.command)
            self._proc = subprocess.Popen(
                [resolved_cmd] + self.args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=merged_env,
                text=True,
                bufsize=1,
            )
            # 发送 initialize 握手
            init_result = self._rpc("initialize", {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "github-trending-monitor", "version": "1.0.0"},
            })
            if init_result is None:
                self.stop()
                return False
            # 发送 initialized 通知
            self._notify("notifications/initialized", {})
            self._started = True
            _logger.info(f"[MCP] 服务器 '{self.name}' 启动成功")
            return True
        except Exception as e:
            _logger.error(f"[MCP] 服务器 '{self.name}' 启动失败: {e}")
            return False

    def stop(self):
        """停止子进程"""
        if self._proc:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=5)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None
        self._started = False

    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    # ── JSON-RPC 通信 ─────────────────────────────────────────────────────────

    def _rpc(self, method: str, params: Dict) -> Optional[Any]:
        """发送 JSON-RPC 请求并等待响应"""
        with self._lock:
            req_id = self._next_id
            self._next_id += 1

        request = json.dumps({
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params,
        }) + "\n"

        try:
            self._proc.stdin.write(request)
            self._proc.stdin.flush()

            deadline = time.time() + self.timeout
            while time.time() < deadline:
                line = self._proc.stdout.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # 过滤通知消息（无 id）
                if "id" not in msg:
                    continue
                if msg.get("id") == req_id:
                    if "error" in msg:
                        _logger.warning(f"[MCP] RPC 错误 {method}: {msg['error']}")
                        return None
                    return msg.get("result")
            _logger.warning(f"[MCP] RPC 超时: {method}")
            return None
        except Exception as e:
            _logger.error(f"[MCP] RPC 通信失败 {method}: {e}")
            return None

    def _notify(self, method: str, params: Dict):
        """发送 JSON-RPC 通知（无需响应）"""
        notification = json.dumps({
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }) + "\n"
        try:
            self._proc.stdin.write(notification)
            self._proc.stdin.flush()
        except Exception as e:
            _logger.debug(f"[MCP] 发送通知失败 {method}: {e}")

    # ── MCP 操作 ──────────────────────────────────────────────────────────────

    def list_tools(self) -> List[Dict]:
        """获取服务器提供的工具列表"""
        result = self._rpc("tools/list", {})
        if result is None:
            return []
        return result.get("tools", [])

    def call_tool(self, tool_name: str, arguments: Dict) -> str:
        """调用 MCP 工具，返回文本结果"""
        result = self._rpc("tools/call", {
            "name": tool_name,
            "arguments": arguments,
        })
        if result is None:
            return f"[MCP] 工具 '{tool_name}' 调用失败或超时"

        # 解析 content 列表
        content_list = result.get("content", [])
        if not content_list:
            return "(无返回内容)"

        parts = []
        for item in content_list:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(item.get("text", ""))
                elif item.get("type") == "image":
                    parts.append(f"[图片: {item.get('mimeType', 'unknown')}]")
                else:
                    parts.append(str(item))
            else:
                parts.append(str(item))

        text = "\n".join(parts)
        if result.get("isError"):
            return f"[MCP 错误] {text}"
        return text


# ── MCP 加载器 ───────────────────────────────────────────────────────────────

class MCPLoader:
    """管理多个 MCP 客户端，动态生成 TOOLS / HANDLERS"""

    def __init__(self):
        self._clients: Dict[str, MCPClient] = {}
        self._tools: List[Dict] = []
        self._handlers: Dict[str, Any] = {}
        self._loaded = False

    def load(self) -> Tuple[List[Dict], Dict]:
        """加载所有配置的 MCP 服务器，返回 (tools, handlers)"""
        if self._loaded:
            return self._tools, self._handlers

        servers = _load_config()
        if not servers:
            _logger.debug("[MCP] 未找到 mcp_servers.json，跳过 MCP 加载")
            self._loaded = True
            return [], {}

        for server_name, server_cfg in servers.items():
            self._load_server(server_name, server_cfg)

        # 添加 MCP 管理工具
        self._tools.append({
            "name": "list_mcp_servers",
            "description": "列出当前已加载的 MCP 服务器及其工具",
            "input_schema": {"type": "object", "properties": {}, "required": []},
        })
        self._handlers["list_mcp_servers"] = self._tool_list_servers

        self._loaded = True
        total = len(self._tools) - 1  # 减去管理工具本身
        _logger.info(f"[MCP] 共加载 {len(self._clients)} 个服务器，{total} 个工具")
        return self._tools, self._handlers

    def _load_server(self, server_name: str, cfg: Dict):
        """启动一个 MCP 服务器并注册其工具"""
        command = cfg.get("command", "")
        args = cfg.get("args", [])
        env = cfg.get("env", {})

        if not command:
            _logger.warning(f"[MCP] 服务器 '{server_name}' 缺少 command 配置，跳过")
            return

        client = MCPClient(
            name=server_name,
            command=command,
            args=args,
            env=env,
            timeout=cfg.get("timeout", 30.0),
        )

        if not client.start():
            _logger.warning(f"[MCP] 服务器 '{server_name}' 启动失败，跳过")
            return

        self._clients[server_name] = client

        # 获取工具列表并注册
        mcp_tools = client.list_tools()
        registered = 0
        for tool_def in mcp_tools:
            tool_name = tool_def.get("name", "")
            if not tool_name:
                continue

            # 为避免工具名冲突，添加服务器前缀（可选）
            # 如果不同服务器有同名工具，使用 server_name__tool_name 格式
            qualified_name = (
                tool_name
                if tool_name not in self._handlers
                else f"{server_name}__{tool_name}"
            )

            description = tool_def.get("description", f"MCP tool: {tool_name}")
            input_schema = tool_def.get("inputSchema", {
                "type": "object", "properties": {}, "required": []
            })

            self._tools.append({
                "name": qualified_name,
                "description": f"[MCP:{server_name}] {description}",
                "input_schema": input_schema,
            })

            # 闭包捕获 client 和 tool_name
            def make_handler(c: MCPClient, tn: str):
                def handler(**kwargs) -> str:
                    if not c.is_alive():
                        return f"[MCP] 服务器 '{c.name}' 已断开"
                    return c.call_tool(tn, kwargs)
                return handler

            self._handlers[qualified_name] = make_handler(client, tool_name)
            registered += 1

        _logger.info(f"[MCP] 服务器 '{server_name}' 注册了 {registered} 个工具")

    def _tool_list_servers(self) -> str:
        """列出所有已加载的 MCP 服务器"""
        if not self._clients:
            return "当前没有已加载的 MCP 服务器"

        lines = ["已加载的 MCP 服务器:"]
        for name, client in self._clients.items():
            status = "运行中" if client.is_alive() else "已断开"
            tools_for_server = [
                t["name"] for t in self._tools
                if t["name"].startswith(f"{name}__") or
                   f"[MCP:{name}]" in t.get("description", "")
            ]
            lines.append(f"  - {name} [{status}]: {len(tools_for_server)} 个工具")
            for tn in tools_for_server[:5]:
                lines.append(f"      • {tn}")
            if len(tools_for_server) > 5:
                lines.append(f"      ... 共 {len(tools_for_server)} 个")
        return "\n".join(lines)

    def stop_all(self):
        """停止所有 MCP 服务器"""
        for client in self._clients.values():
            client.stop()
        self._clients.clear()

    @property
    def server_count(self) -> int:
        return len(self._clients)


# ── 模块级单例（延迟加载）────────────────────────────────────────────────────

_loader = MCPLoader()


def load_mcp() -> Tuple[List[Dict], Dict]:
    """加载所有 MCP 服务器，返回 (MCP_TOOLS, MCP_HANDLERS)。
    首次调用时初始化，后续调用幂等返回缓存结果。
    """
    return _loader.load()


# 导出符号（在 __init__.py import 时触发加载）
MCP_TOOLS, MCP_HANDLERS = load_mcp()
