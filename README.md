# mocklite

> 零配置的本地 API Mock 服务器 —— 把一个目录变成一套可访问的 REST 接口。

[![license](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![python](https://img.shields.io/badge/python-3.8%2B-green.svg)](https://www.python.org)
![deps](https://img.shields.io/badge/dependencies-zero-brightgreen.svg)

调试前端、给同事写对接文档、压测一个还没做完的接口？
`mocklite` 一行命令起服务 —— 把 `mocks/` 目录下的 JSON 资源按文件名和 HTTP 方法自动映射成 REST 接口。
**只使用 Python 标准库**，零依赖，跨 Windows / macOS / Linux。

- 🌱 **零配置**：默认 `./mocks`、端口 `7777`，扫一下就能用
- 🧬 **路由约定**：`<METHOD>.json` = HTTP 方法，`<dir>` = URL 段，`<{param}>` = 路径参数
- 🐢 **延迟注入**：`_delay_ms` 字段让响应变慢，模拟慢网络
- 💥 **错误注入**：`_status` / `_headers` 一键假装异常和自定义响应头
- 🌐 **CORS 默认开启**：OPTIONS 预检 + 任意 Origin 放行，前端 `fetch` 直接吃
- 🧵 **多线程**：`ThreadingHTTPServer` 并发处理请求，互不阻塞

---

## 安装

只需要 Python 3.8+，**没有第三方依赖**。

```bash
# 方式一：直接下载单文件
curl -O https://raw.githubusercontent.com/1fzx23/mocklite/main/mocklite.py
python mocklite.py --help

# 方式二：克隆仓库（仓库自带一组示例 fixtures）
git clone https://github.com/1fzx23/mocklite.git
cd mocklite
python mocklite.py

# 方式三：用 pip 安装（会装一个 mocklite 命令）
pip install .
mocklite
```

---

## 5 分钟上手

### 1. 写 fixtures

任何 `.json` 文件，按目录约定摆放：

```
mocks/
├── users/
│   ├── GET.json              →  GET    /users
│   ├── POST.json             →  POST   /users
│   └── {id}/
│       ├── GET.json          →  GET    /users/{id}
│       ├── DELETE.json       →  DELETE /users/{id}
│       └── orders/
│           └── GET.json      →  GET    /users/{id}/orders
└── posts/
    ├── GET.json
    ├── POST.json
    └── {postId}/
        └── comments/
            └── GET.json      →  GET    /posts/{postId}/comments
```

> 约定速记：文件 `<METHOD>.json`（大小写不敏感）确定 HTTP 方法；子目录名就是 URL 段；用 `{xxx}` 包起来表示路径参数。

### 2. 写响应模板

`mocks/users/GET.json`:

```json
{
  "_delay_ms": 80,
  "users": [
    {"id": 1, "name": "Alice"},
    {"id": 2, "name": "Bob"}
  ],
  "total": 2
}
```

请求：
```bash
curl http://127.0.0.1:7777/users
```

响应（80ms 后）：
```json
{"users": [{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}], "total": 2}
```

---

## 响应模板的“指令”字段

下划线开头的字段是**指令**，不会出现在响应体里：

| 字段          | 作用                                                | 默认值 |
| ------------- | --------------------------------------------------- | ------ |
| `_status`     | 自定义 HTTP 状态码（如 201、400、500）              | 200    |
| `_headers`    | 自定义响应头 dict                                   | `{}`   |
| `_delay_ms`   | 响应前延迟（毫秒），会叠加到全局 `-d` 上            | 0      |
| 其它 `_xxx`   | 同样会被剥离（保留给将来扩展用）                    | —      |

例：故意造一个 401：

`mocks/login/POST.json`:
```json
{
  "_status": 401,
  "_headers": {"WWW-Authenticate": "Bearer"},
  "error": "invalid_credentials",
  "message": "用户名或密码错误"
}
```

---

## 使用示例

```bash
# 默认：mocks 在 ./mocks，端口 7777
python mocklite.py

# 指定目录 + 端口
python mocklite.py ./fixtures -p 8080

# 全局额外加 100ms 延迟（叠加到 fixture 的 _delay_ms 上）
python mocklite.py ./mocks -d 100

# 绑定 0.0.0.0，局域网里别人也能调
python mocklite.py ./mocks --host 0.0.0.0 -p 9000

# 不打印路由表（仅服务日志）
python mocklite.py ./mocks --quiet
```

### 启动横幅示例

```
MockLite v1.0.0 · 6 route(s) from ./mocks
  GET     /posts/{postId}/comments
          /users
          /users/{id}
  POST    /posts
          /users
  DELETE  /users/{id}

Listen on http://127.0.0.1:7777    (Ctrl+C to stop)
```

---

## 命令参数

| 参数            | 说明                                                  |
| --------------- | ----------------------------------------------------- |
| `mocks_dir`     | mocks 目录，默认 `./mocks`                            |
| `-p, --port`    | 端口，默认 `7777`                                     |
| `--host`        | 绑定地址，默认 `127.0.0.1`（外网请用 `0.0.0.0`）      |
| `-d, --delay-ms`| 全局最小延迟（毫秒），与 fixture 的 `_delay_ms` 叠加   |
| `--quiet`       | 启动时不打印路由表                                    |
| `--version`     | 打印版本号                                            |

---

## 几个常见的使用场景

### 场景 1：前端联调

React/Vue 项目里，把所有 `fetch('/api/...')` 换成 `fetch('http://localhost:7777/...')`，
逐个 fixture 写真实结构的响应，调样式、调分页、调空状态都不用等服务端。

### 场景 2：离线开发

出差、没 VPN、调接口超时——把 `mocks/` 整个丢到 U 盘里，飞机上照样写。

### 场景 3：构造“服务异常”状态

`-d 8000` 全局慢响应，或者临时把某个 fixture 改成 `"_status": 503`，前端立刻看到 loading / error。

### 场景 4：作为 CI 跑前端集成测试

```yaml
# .github/workflows/fe-e2e.yml
- run: python mocklite.py ./e2e/fixtures -p 7777 &
- run: npm run test:e2e
```

---

## 路由未命中

没匹配的 path 返回结构化 404 + 可用路由提示，前端不用猜：

```json
{
  "error": "no_route",
  "method": "GET",
  "path": "/nope",
  "hint": {
    "similar_paths": ["/posts/{postId}/comments", "/users", "/users/{id}"],
    "all_routes": ["DELETE /users/{id}", "GET /posts/{postId}/comments", ...]
  }
}
```

---

## 工作原理

1. **扫描**：启动时 `os.walk` 递归遍历 mocks 目录，发现 `<METHOD>.json` 文件就登记成 `Route(method, pattern, fixture)`
2. **匹配**：请求到来 → `(method, path)` 按段对段匹配，`{xxx}` 段匹配任意单段
3. **加载**：命中后读 fixture.json，按 `_` 前缀剥离指令字段，剩下的就是响应 body
4. **响应**：套默认 CORS 头 → `time.sleep(_delay_ms)` → `send_response(_status)` → 序列化为 JSON

整个响应过程**只在内存里**，数据完全静态；改 fixture 需要重启服务（开发期推荐 `--watch`，未提供，先用外部 rerun）。

---

## 测试

```bash
python tests/test_mocklite.py
```

覆盖：路由扫描、路径参数、特殊字段剥离、延迟注入、CORS 预检、404 路由提示。

---

## 跟同类工具的关系

| 工具          | 语言        | 关键差异                                                     |
| ------------- | ----------- | ------------------------------------------------------------ |
| `json-server` | Node.js     | 需要 npm install                                             |
| `MockServer`  | Java        | 需要 JRE                                                      |
| `WireMock`    | Java        | 企业级，功能多但学习曲线陡                                    |
| **mocklite**  | **Python**  | **零依赖、单文件、约定优先**                                  |

---

## License

[MIT](LICENSE) © 1fzx23
