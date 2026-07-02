"""mcp — MCP (Model Context Protocol) client。 stdio transport のみの最小実装。

設定は `.lispy-mcp.json` (cwd から上方探索、 LISPY_MCP で明示指定可):

    {"servers": {
       "fs": {"command": "npx",
              "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
              "env": {}}}}

server ごとに 1 プロセスを spawn して initialize → tools/list し、 各 tool を
`mcp__<server>__<tool>` の名前で lispy の tool layer に載せる (agent からは他の tool と
同じ tool_call。 pre/post hook・中断チェックも同じ経路で効く)。

プロセスは module-level に cache され、 env (main / fork / spawn child) を跨いで共有。
終了時に atexit で terminate する。 SSE / HTTP transport は未対応 (要るときに足す)。
"""
from __future__ import annotations

import atexit
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent

PROTOCOL_VERSION = "2024-11-05"
REQUEST_TIMEOUT = int(os.environ.get("LISPY_MCP_TIMEOUT", "60"))


class MCPServer:
    """1 つの MCP server プロセス。 JSON-RPC 2.0 / newline-delimited JSON over stdio。"""

    def __init__(self, name: str, command: str, args: list[str], env_vars: dict[str, str]):
        self.name = name
        self.tools: list[dict] = []
        self.proc = subprocess.Popen(
            [command, *args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            env={**os.environ, **{str(k): str(v) for k, v in (env_vars or {}).items()}},
        )
        self._id = 0
        self._id_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._responses: dict[int, dict] = {}
        self._cond = threading.Condition()
        threading.Thread(target=self._reader, daemon=True).start()

    # --- wire ---

    def _send(self, obj: dict) -> None:
        line = json.dumps(obj, ensure_ascii=False)
        with self._write_lock:
            assert self.proc.stdin is not None
            self.proc.stdin.write(line + "\n")
            self.proc.stdin.flush()

    def _reader(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            # response だけ拾う。 server 発の request / notification (sampling 等) は未対応で無視
            if isinstance(msg, dict) and "id" in msg and ("result" in msg or "error" in msg):
                with self._cond:
                    self._responses[msg["id"]] = msg
                    self._cond.notify_all()

    def request(self, method: str, params: dict | None = None, timeout: float = REQUEST_TIMEOUT) -> dict:
        with self._id_lock:
            self._id += 1
            rid = self._id
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
        deadline = time.time() + timeout
        with self._cond:
            while rid not in self._responses:
                remaining = deadline - time.time()
                if remaining <= 0:
                    raise TimeoutError(f"mcp {self.name}: {method} timeout ({timeout}s)")
                if self.proc.poll() is not None:
                    raise RuntimeError(f"mcp {self.name}: process exited ({self.proc.returncode})")
                self._cond.wait(min(remaining, 0.5))
            msg = self._responses.pop(rid)
        if "error" in msg:
            err = msg["error"] or {}
            raise RuntimeError(f"mcp {self.name}: {err.get('message', 'unknown error')}")
        return msg.get("result") or {}

    def notify(self, method: str, params: dict | None = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    # --- protocol ---

    def initialize(self) -> None:
        self.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "lispy", "version": "0.1"},
        }, timeout=20)
        self.notify("notifications/initialized")
        self.tools = list(self.request("tools/list", timeout=20).get("tools") or [])

    def call_tool(self, tool: str, arguments: dict | None) -> str:
        result = self.request("tools/call", {"name": tool, "arguments": arguments or {}})
        parts: list[str] = []
        for c in result.get("content") or []:
            if isinstance(c, dict) and c.get("type") == "text":
                parts.append(str(c.get("text", "")))
            else:
                parts.append(json.dumps(c, ensure_ascii=False))
        text = "\n".join(parts) or "(empty mcp result)"
        if result.get("isError"):
            text = f"(mcp tool error) {text}"
        return text

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()


# ---------------------------------------------------------------------------
# config / registry
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, dict[str, MCPServer]] = {}  # config path -> {server name: MCPServer}


def _find_config() -> Path | None:
    explicit = os.environ.get("LISPY_MCP", "").strip()
    if explicit:
        p = Path(explicit).expanduser()
        return p if p.exists() else None
    d = Path(os.getcwd())
    for parent in [d, *d.parents]:
        f = parent / ".lispy-mcp.json"
        if f.exists():
            return f
    return None


def connect_all() -> dict[str, MCPServer]:
    """設定にある server 全部に接続 (済みなら cache)。 個別の失敗は warn して skip。"""
    cfg_path = _find_config()
    if cfg_path is None:
        return {}
    key = str(cfg_path.resolve())
    if key in _REGISTRY:
        return _REGISTRY[key]
    servers: dict[str, MCPServer] = {}
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"  (mcp config parse error: {cfg_path}: {e})", file=sys.stderr)
        _REGISTRY[key] = servers
        return servers
    for name, spec in (cfg.get("servers") or {}).items():
        if not isinstance(spec, dict) or not spec.get("command"):
            continue
        try:
            srv = MCPServer(
                name=str(name),
                command=str(spec["command"]),
                args=[str(a) for a in spec.get("args") or []],
                env_vars=spec.get("env") or {},
            )
            srv.initialize()
            servers[str(name)] = srv
            print(f"  (mcp: {name} connected, {len(srv.tools)} tools)", file=sys.stderr)
        except Exception as e:
            print(f"  (mcp: {name} connect failed: {type(e).__name__}: {e})", file=sys.stderr)
    _REGISTRY[key] = servers
    return servers


def tool_layer() -> tuple[dict[str, Any], list[dict]]:
    """lispy の _build_tool_layer に merge する (tools, schema)。
    handler は (args, env) -> str — 他の tool と同じ signature。"""
    tools: dict[str, Any] = {}
    schema: list[dict] = []
    for sname, srv in connect_all().items():
        for t in srv.tools:
            tname = str(t.get("name", ""))
            if not tname:
                continue
            fq = f"mcp__{sname}__{tname}"[:64]
            desc = f"[mcp:{sname}] " + str(t.get("description") or "")

            def handler(args: dict, env: Any, _srv: MCPServer = srv, _tn: str = tname) -> str:
                try:
                    return _srv.call_tool(_tn, args)
                except Exception as e:
                    return f"(mcp error: {type(e).__name__}: {e})"

            tools[fq] = handler
            schema.append({
                "type": "function",
                "function": {
                    "name": fq,
                    "description": desc[:1000],
                    "parameters": t.get("inputSchema") or {"type": "object", "properties": {}},
                },
            })
    return tools, schema


def info() -> str:
    """(mcp-list) — 接続中の server と tool 一覧。"""
    servers = connect_all()
    if not servers:
        return "(mcp: 設定なし — .lispy-mcp.json を cwd 上方に置くか LISPY_MCP で指定)"
    out = []
    for name, srv in servers.items():
        alive = "up" if srv.proc.poll() is None else f"dead ({srv.proc.returncode})"
        out.append(f"{name} [{alive}]: " + ", ".join(t.get("name", "?") for t in srv.tools))
    return "\n".join(out)


def close_all() -> None:
    for servers in _REGISTRY.values():
        for srv in servers.values():
            try:
                srv.close()
            except Exception:
                pass


atexit.register(close_all)
