#!/usr/bin/env python3
"""mocklite — 零配置本地 API Mock 服务器。

只使用 Python 标准库，把一个目录树变成一套可访问的 REST 接口：

    mocks/
      users/
        GET.json                       →  GET  /users
        POST.json                      →  POST /users
        {id}/
          GET.json                     →  GET    /users/{id}
          DELETE.json                  →  DELETE /users/{id}

响应模板里以下划线开头的字段是“指令”，不会出现在响应体里：

    {
      "_status":   201,                   # 自定义 HTTP 状态码，默认 200
      "_headers":  {"X-Trace-Id": "..."}, # 自定义响应头
      "_delay_ms": 300,                   # 响应前延迟（毫秒）
      "id":   1,                          # ← 真正返回的 JSON
      "name": "Alice"
    }

特性:
  - 一行命令起服务，自动扫描目录生成路由
  - CORS 默认开启（任意 Origin 都放行，含 OPTIONS 预检）
  - 延迟注入 / 状态码注入 / 自定义响应头
  - 线程池并发请求（标准库 ThreadingHTTPServer）
  - 路由未命中返回结构化 404（含 method / path / hint）

Examples
--------
    # 默认配置起服务：./mocks, 端口 7777
    python mocklite.py

    # 指定 mocks 目录 + 端口
    python mocklite.py ./fixtures -p 8080

    # 全局加 100ms 延迟（叠加到 _delay_ms）
    python mocklite.py ./mocks -d 100
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sys
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

__version__ = "1.0.0"

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
SUPPORTED_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")

# 匹配形如 GET.json / post.json 的文件名，捕获大写化的方法名
_METHOD_FILE_RE = re.compile(r"^(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\.json$", re.IGNORECASE)

# 响应模板里这些字段是“指令”，会被剥离，剩下的当作 body
_SPECIAL_PREFIX = "_"
_SPECIAL_KEYS = {"_status", "_headers", "_delay_ms"}

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, PUT, PATCH, DELETE, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Authorization",
    "Access-Control-Max-Age": "86400",
}


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Route:
    """一条路由：HTTP 方法 + URL pattern + fixture 路径。"""

    method: str
    pattern: str  # e.g. "/users/{id}" or "/users"
    fixture: Path  # absolute path to the .json file


@dataclass
class AppState:
    """进程级路由表与运行参数。BaseHTTPRequestHandler 通过类属性访问。"""

    routes: list[Route]
    default_delay_ms: int = 0
    seen_statuses: set[int] | None = None  # 用于启动横幅统计


# ---------------------------------------------------------------------------
# 路由扫描
# ---------------------------------------------------------------------------
def scan_routes(root: Path) -> list[Route]:
    """递归扫描 root，把 .json 资源文件转成 Route 列表。

    - 文件名必须是 ``<METHOD>.json``（大小写不敏感，输出统一大写）
    - 子目录名是 URL 段；除非以 ``{`` 开头 ``}`` 结尾，此时视为路径参数
    - 没有任何 URL 段时，pattern = "/"（可命中 GET /）
    """
    root = root.resolve()
    routes: list[Route] = []

    for dirpath, _dirs, files in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root)
        # URL 段：每个目录名就直接是路径的一段；根目录对应 "/"
        url_parts = [p for p in rel_dir.parts]
        prefix = "/" + "/".join(url_parts) if url_parts else ""

        for fn in files:
            m = _METHOD_FILE_RE.match(fn)
            if not m:
                continue
            method = m.group(1).upper()
            routes.append(Route(method=method, pattern=prefix or "/", fixture=Path(dirpath) / fn))

    # 排序：先按 method，再按 pattern（更具体的在前，方便读）
    routes.sort(key=lambda r: (r.method, r.pattern, str(r.fixture)))
    return routes


def match_route(method: str, request_path: str, routes: Iterable[Route]) -> Route | None:
    """按段对段匹配第一条同 method + path pattern 的路由。

    ``/users/42`` 能命中 ``/users/{id}``，但不能命中 ``/users/{id}/comments``。
    """
    req_parts = _split_path(request_path)

    for r in routes:
        if r.method != method:
            continue
        pat_parts = _split_path(r.pattern)
        if _parts_match(pat_parts, req_parts):
            return r
    return None


def _split_path(path: str) -> list[str]:
    """把 path 切成非空段。根路径返回 []（与 pattern="/" 对齐）。"""
    if path in ("", "/"):
        return []
    return [seg for seg in path.split("/") if seg]


def _parts_match(pat: list[str], req: list[str]) -> bool:
    """段级匹配：``{xxx}`` 匹配任何非空段。"""
    if len(pat) != len(req):
        return False
    for p, r in zip(pat, req):
        if p.startswith("{") and p.endswith("}") and len(p) >= 3:
            continue
        if p != r:
            return False
    return True


# ---------------------------------------------------------------------------
# 响应模板
# ---------------------------------------------------------------------------
def _load_fixture(path: Path) -> tuple[int, dict, int, object]:
    """读取一个 fixture .json，返回 (status, extra_headers, delay_ms, body)。

    - 不是 dict：当个原始 payload 处理 (200, {}, 0, payload)
    - 字段以下划线开头视为指令（剥掉），剩下的原样返回
    """
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)

    if not isinstance(data, dict):
        return 200, {}, 0, data

    status = int(data.get("_status", 200)) if isinstance(data.get("_status"), int) else 200
    extra = data.get("_headers") or {}
    if not isinstance(extra, dict):
        extra = {}
    extra = {str(k): str(v) for k, v in extra.items()}

    delay = int(data.get("_delay_ms", 0)) if isinstance(data.get("_delay_ms"), int) else 0
    body = {k: v for k, v in data.items() if not k.startswith(_SPECIAL_PREFIX)}
    return status, extra, delay, body


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------
class MockHandler(BaseHTTPRequestHandler):
    """每次请求解析 method + path → 找路由 → 套响应模板 → 回 JSON。"""

    # 进程级状态，由 main() 在启动前注入
    state: AppState = AppState(routes=[])

    # 关闭 BaseHTTPRequestHandler 默认的 stderr 输出，改成更整齐的单行日志
    def log_message(self, fmt: str, *args) -> None:  # type: ignore[override]
        try:
            msg = fmt % args
        except Exception:  # noqa: BLE001
            msg = str(args)
        ts = time.strftime("%H:%M:%S")
        sys.stderr.write(f"[{ts}] {self.address_string()} {msg}\n")
        sys.stderr.flush()

    # ---------- 工具方法 ----------
    def _write_json(self, status: int, payload: object, extra_headers: dict | None = None) -> None:
        """把 payload 用 UTF-8 JSON 写入；HEAD/204/304 时 body 为空。"""
        if payload is None or status in (204, 304):
            body_bytes = b""
        else:
            body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body_bytes)))
        for k, v in CORS_HEADERS.items():
            self.send_header(k, v)
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body_bytes:
            self.wfile.write(body_bytes)

    def _route_hint(self, method: str, path: str) -> list[str]:
        """给 404 拼一个提示，列出相似 method 的可用路由，方便前端定位。"""
        same = sorted({r.pattern for r in self.state.routes if r.method == method})
        all_paths = sorted({f"{r.method} {r.pattern}" for r in self.state.routes})
        return {"similar_paths": same[:8], "all_routes": all_paths[:40]}

    # ---------- 调度器 ----------
    def _dispatch(self) -> None:
        parsed = urlparse(self.path)
        req_path = parsed.path or "/"
        method = self.command.upper()

        # OPTIONS 预检：直接放行，不需要匹配 fixture
        if method == "OPTIONS":
            self._write_json(200, {"ok": True, "method": "OPTIONS", "path": req_path})
            return

        # HEAD 按 HTTP 语义复用 GET 的响应体（但不发 body）
        lookup_method = "GET" if method == "HEAD" else method
        route = match_route(lookup_method, req_path, self.state.routes)
        if route is None:
            hint = self._route_hint(method, req_path)
            self._write_json(
                404,
                {
                    "error": "no_route",
                    "method": method,
                    "path": req_path,
                    "hint": hint,
                },
            )
            return

        try:
            status, extra_headers, delay_ms, body = _load_fixture(route.fixture)
        except FileNotFoundError:
            self._write_json(500, {"error": "fixture_missing", "fixture": str(route.fixture)})
            return
        except json.JSONDecodeError as exc:
            self._write_json(500, {"error": "fixture_invalid_json", "detail": str(exc),
                                   "fixture": str(route.fixture)})
            return

        delay = max(delay_ms, self.state.default_delay_ms)
        if delay > 0:
            # 延迟不会让 connection close，对 ThreadingHTTPServer 来说很安全
            time.sleep(delay / 1000.0)

        # HEAD：不发 body（即使 status=200），跟 RFC 7231 一致
        payload = None if method == "HEAD" else body
        self._write_json(status, payload, extra_headers)

    # ---------- methods ----------
    def do_GET(self) -> None:     self._dispatch()
    def do_POST(self) -> None:    self._dispatch()
    def do_PUT(self) -> None:     self._dispatch()
    def do_PATCH(self) -> None:   self._dispatch()
    def do_DELETE(self) -> None:  self._dispatch()
    def do_HEAD(self) -> None:    self._dispatch()
    def do_OPTIONS(self) -> None: self._dispatch()


# ---------------------------------------------------------------------------
# 启动横幅
# ---------------------------------------------------------------------------
def _print_banner(args: argparse.Namespace, routes: list[Route], host: str, port: int) -> None:
    by_method: dict[str, list[str]] = {}
    for r in routes:
        by_method.setdefault(r.method, []).append(r.pattern)

    print(f"MockLite v{__version__} · {len(routes)} route(s) from {args.mocks_dir}")
    for m in SUPPORTED_METHODS:
        if m not in by_method:
            continue
        print(f"  {m:6s}  " + "\n  " + " " * 8 + "  ".join(sorted(by_method[m])))
    extra = f"  +global_delay={args.delay_ms}ms" if args.delay_ms else ""
    print(f"\nListen on http://{host}:{port}{extra}    (Ctrl+C to stop)")


def _pick_bind_host(host: str, port: int) -> tuple[str, int]:
    """0.0.0.0 → 让系统挑可用的对外 IP；其它原样返回。"""
    if host == "0.0.0.0":
        # 仅作打印用，实际绑仍是 0.0.0.0
        return host, port
    return host, port


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mocklite",
        description="零配置的本地 API Mock 服务器：把 mocks 目录变成 REST 接口。",
    )
    p.add_argument("mocks_dir", nargs="?", default="./mocks",
                   help="Mocks 目录（默认 ./mocks）")
    p.add_argument("-p", "--port", type=int, default=7777,
                   help="监听端口（默认 7777）")
    p.add_argument("--host", default="127.0.0.1",
                   help="监听地址（默认 127.0.0.1；想暴露到局域网可用 0.0.0.0）")
    p.add_argument("-d", "--delay-ms", type=int, default=0,
                   help="全局最小延迟（毫秒），会叠加到每个 fixture 的 _delay_ms 上")
    p.add_argument("--quiet", action="store_true",
                   help="启动时不打印路由表（仍会打 access log 到 stderr）")
    p.add_argument("--version", action="version", version=f"mocklite {__version__}")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    mocks_root = Path(args.mocks_dir).expanduser().resolve()
    if not mocks_root.is_dir():
        print(f"error: mocks dir not found: {mocks_root}", file=sys.stderr)
        return 2

    try:
        routes = scan_routes(mocks_root)
    except OSError as exc:
        print(f"error: cannot scan {mocks_root}: {exc}", file=sys.stderr)
        return 2

    if not routes and not args.quiet:
        print(f"warn: no routes discovered under {mocks_root}", file=sys.stderr)

    # 端口占用早失败：起 server 前 bind 一下
    host, port = _pick_bind_host(args.host, args.port)
    test_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        test_sock.bind((host, port))
    except OSError as exc:
        test_sock.close()
        print(f"error: cannot bind {host}:{port} ({exc})", file=sys.stderr)
        return 2
    finally:
        test_sock.close()

    MockHandler.state = AppState(routes=routes, default_delay_ms=max(0, args.delay_ms))

    if not args.quiet:
        _print_banner(args, routes, host, port)
        print()

    httpd = ThreadingHTTPServer((host, port), MockHandler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutdown (Ctrl+C)")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
