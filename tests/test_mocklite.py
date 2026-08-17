#!/usr/bin/env python3
"""mocklite smoke tests.

策略：
  - 用临时目录构造一份 mocks，写得函数会在每个测试里独立准备 fixtures
  - subprocess 启动 mocklite.py 监听随机空闲端口
  - 在子进程内通过 urllib.request 发起 HTTP 请求，验证状态、头、body、延迟
  - 测试结束统一关子进程，不污染全局

需要的依赖：仅 Python 3.8+ 标准库（确保在零依赖项目里也能直接跑）
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MOCKLITE = PROJECT_ROOT / "mocklite.py"

# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def _free_port() -> int:
    """找一个未被占用的端口。"""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def running_server(mocks_dir: Path):
    """起一个 mocklite 子进程，yield base_url；退出时回收。"""
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, str(MOCKLITE), str(mocks_dir), "-p", str(port), "--quiet"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(PROJECT_ROOT),
    )
    base = f"http://127.0.0.1:{port}"

    # 等服务就绪：用 socket 连通性探测，避免 ``GET /`` 在 mocklite 里是 404 的歧义
    deadline = time.time() + 3.0
    ready = False
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                ready = True
                break
        except OSError:
            time.sleep(0.05)
    if not ready:
        proc.kill()
        raise RuntimeError(f"server did not start within 3s; stderr={proc.stderr.read()}")

    try:
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()


def _write_fixture(root: Path, *parts: str, body: dict) -> Path:
    """写入一个 fixture，返回其路径。"""
    fp = root.joinpath(*parts)
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    return fp


def _http(base: str, method: str, path: str, body: bytes | None = None,
          headers: dict | None = None, timeout: float = 2.0):
    req = urllib.request.Request(base + path, data=body, method=method,
                                 headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        # 把错误响应也带回来
        return exc.code, dict(exc.headers or {}), exc.read()


# ---------------------------------------------------------------------------
# 测试
# ---------------------------------------------------------------------------
def test_route_scanning_and_dispatch() -> None:
    """基础路由：扫描 /users/GET.json → GET /users 返回对应 body。"""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "mocks"
        _write_fixture(root, "users", "GET.json", body={"users": [{"id": 1}], "total": 1})
        _write_fixture(root, "users", "POST.json", body={"_status": 201, "id": 99, "ok": True})
        with running_server(root) as base:
            # GET
            code, _, raw = _http(base, "GET", "/users")
            assert code == 200, code
            data = json.loads(raw)
            assert data == {"users": [{"id": 1}], "total": 1}, data
            # POST + 自定义 status
            code, _, raw = _http(base, "POST", "/users")
            assert code == 201, code
            assert json.loads(raw) == {"id": 99, "ok": True}


def test_path_parameter_matching() -> None:
    """``{id}`` 路径参数必须匹配任意非空段。"""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "mocks"
        _write_fixture(root, "items", "{id}", "GET.json", body={"id": 0, "echo": True})
        with running_server(root) as base:
            code, _, raw = _http(base, "GET", "/items/123")
            assert code == 200
            assert json.loads(raw) == {"id": 0, "echo": True}
            code, _, raw = _http(base, "GET", "/items/abc-def")
            assert code == 200
            # 太深一层不应命中
            code, _, _ = _http(base, "GET", "/items/123/sub")
            assert code == 404, code


def test_special_keys_strip() -> None:
    """``_status / _headers / _delay_ms`` 是指令，body 里看不到。"""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "mocks"
        _write_fixture(root, "x", "GET.json", body={
            "_status": 418,
            "_headers": {"X-Tea": "yes"},
            "_delay_ms": 0,
            "_comment": "under score prefix",
            "visible": True,
        })
        with running_server(root) as base:
            t0 = time.time()
            code, headers, raw = _http(base, "GET", "/x")
            elapsed = time.time() - t0
            assert code == 418, code
            assert headers.get("X-Tea") == "yes"
            assert headers.get("Access-Control-Allow-Origin") == "*"
            assert json.loads(raw) == {"visible": True}
            assert elapsed < 1.0  # delay 0 不应该慢


def test_delay_injection() -> None:
    """_delay_ms 与全局 -d 都会让响应变慢，且叠加。"""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "mocks"
        _write_fixture(root, "slow", "GET.json", body={"_delay_ms": 200, "ok": True})
        with running_server(root) as base:
            t0 = time.time()
            code, _, raw = _http(base, "GET", "/slow", timeout=3.0)
            elapsed = time.time() - t0
            assert code == 200 and json.loads(raw) == {"ok": True}
            assert elapsed >= 0.18, f"expected >= 180ms, got {elapsed:.3f}s"


def test_cors_preflight() -> None:
    """OPTIONS 必须返回 200 + 完整 CORS 响应头。"""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "mocks"
        _write_fixture(root, "x", "GET.json", body={"ok": True})
        with running_server(root) as base:
            code, headers, raw = _http(base, "OPTIONS", "/x")
            assert code == 200, code
            assert headers.get("Access-Control-Allow-Origin") == "*"
            assert "OPTIONS" in headers.get("Access-Control-Allow-Methods", "")
            assert json.loads(raw) == {"ok": True, "method": "OPTIONS", "path": "/x"}


def test_404_includes_hint() -> None:
    """未匹配路由返回结构化 404，含可用路由提示。"""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "mocks"
        _write_fixture(root, "users", "GET.json", body={})
        with running_server(root) as base:
            code, _, raw = _http(base, "GET", "/nope")
            assert code == 404
            data = json.loads(raw)
            assert data["error"] == "no_route"
            assert data["method"] == "GET"
            assert data["path"] == "/nope"
            assert "all_routes" in data["hint"]


def main() -> int:
    tests = [
        test_route_scanning_and_dispatch,
        test_path_parameter_matching,
        test_special_keys_strip,
        test_delay_injection,
        test_cors_preflight,
        test_404_includes_hint,
    ]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"  ok    {t.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"  FAIL  {t.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"  ERROR {t.__name__}: {type(exc).__name__}: {exc}")

    print(f"\n{mocklite_result(len(tests), failures)}")
    return 0 if failures == 0 else 1


def mocklite_result(total: int, failed: int) -> str:
    if failed == 0:
        return f"[ok] {total} test(s) passed."
    return f"[fail] {failed}/{total} test(s) failed."


if __name__ == "__main__":
    sys.exit(main())
