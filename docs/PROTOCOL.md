# Protocol Specification

`flow_upload.http.register_upload_routes(...)` 注册的两条路由对外的精确协议规格。
**跨语言客户端**（浏览器 JS / Python httpx / curl / 任何能发 HTTP 的语言）都依赖
此文档作为合约；只要本文档覆盖的字段 / 错误码 / 帧形状不变，客户端实现就不该挂。

---

## 1. POST `{route_prefix}` —— Multipart 上传

默认 `route_prefix = /api/file_to_url`，由 `register_upload_routes(route_prefix=...)`
覆盖。下文以 `/api/file_to_url` 为例。

### 1.1 请求

```
POST /api/file_to_url[?progress_id=<uuid>][&expected_bytes=<int>]
Content-Type: multipart/form-data; boundary=...
Authorization: Bearer <token>            # 仅当 progress_id 存在时**强制**

--<boundary>
Content-Disposition: form-data; name="folder"

<可选目录提示，UTF-8，<= 65536 bytes>
--<boundary>
Content-Disposition: form-data; name="<extra_form_field_1>"

<UTF-8，<= 2048 bytes>
... 其它在 register_upload_routes(extra_form_fields=...) 声明的字段 ...
--<boundary>
Content-Disposition: form-data; name="file"; filename="my-file.bin"
Content-Type: application/octet-stream

<binary>
--<boundary>--
```

#### 1.1.1 Query 参数

| 名称 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `progress_id` | UUID v1-v5（任意合法 UUID 字符串） | ❌ | 启用流式 + Redis 双通道进度。同一 UUID **必须**也传给 SSE 路由 |
| `expected_bytes` | int (1 ≤ x ≤ `max_bytes`) | ❌ | 文件总字节数；用于 SSE 第一帧就给出 bytes_total，让进度条立刻显示比例。仅在 `progress_id` 同时存在时生效 |

#### 1.1.2 Multipart Form 字段

| 字段名 | 类型 | 必填 | 长度上限 | 说明 |
|---|---|---|---|---|
| `folder` | text | ❌ | 65536 bytes（UTF-8） | 业务策略可读取的子目录提示；空时 `ObjectKeyContext.form_fields["folder"] = ""` |
| `<extra_form_fields[]>` | text | ❌ | 2048 bytes（UTF-8 each） | 由 `register_upload_routes(extra_form_fields=[...])` 声明；未声明的字段名会被静默忽略 |
| `file` | file | ✅ | `max_bytes`（默认 20 GiB） | 文件二进制内容 |

**字段顺序约定**：所有 text 域**必须**排在 `file` 之前（`folder` 在最前最佳）。
策略闭包在文件 part 第一个字节到达时就开始决定 COS key（`(folder_prefix, object_name)`），
此时它需要 form 字段已就绪。**违反顺序会导致策略闭包看到空字符串**，行为退化但不会 raise。

#### 1.1.3 Authorization

- 当 `progress_id` 存在时，`auth_resolver(request)` 必被调用，失败抛 401/403
- 当 `progress_id` 不存在时（兼容上传分支），调用 `auth_resolver_optional(request)`；失败时不阻断，仅 `ObjectKeyContext.user_id = None`

`auth_resolver` 的具体实现由调用方注入，本协议不规定 token 格式。
推荐 `Authorization: Bearer <token>` 风格。

### 1.2 响应

#### 1.2.1 200 OK

```json
{ "url": "https://<bucket>.cos.<region>.myqcloud.com/<folder>/<object_name>" }
```

URL 是公网 HTTPS 直链；浏览器可直接 GET 拿到内容（COS 桶应配置公开读，或自行
增加签名 URL 包装层）。

#### 1.2.2 错误响应

所有错误返回 `application/json`：`{"detail": "<message>"}`。

| HTTP | `detail`（字面量） | 触发条件 |
|---|---|---|
| 400 | `"Expected multipart/form-data"` | 请求 `Content-Type` 不是 multipart |
| 400 | `"Missing multipart boundary"` | multipart 头里没有 boundary 参数 |
| 400 | `"Missing file field in multipart body"` | 没有任何 file 域 |
| 400 | `"Invalid progress_id"` | `progress_id` 非合法 UUID 字符串 |
| 400 | `"folder field too large"` | `folder` 域超过 65536 bytes |
| 400 | `"<field_name> field too large"` | extra_form_field 超过 2048 bytes |
| 400 | `"multipart parse error: <details>"` | multipart 解析过程抛非 ValueError 异常 |
| 401 | `"Authorization required"` 或 `auth_resolver` 自定义文案 | 无 / 无效 Authorization |
| 403 | `"progress_id not owned by current user"` | `progress_id` 已被另一 user_id owner |
| 403 | `"Invalid token"` 或 `auth_resolver` 自定义文案 | token 解析失败 |
| 413 | `"File too large (max <max_bytes> bytes)"` | 文件域超过 `max_bytes` |
| 422 | FastAPI 自动 | `expected_bytes` 不在 `[1, max_bytes]` 区间 |
| 500 | 任意异常 message | 上游 / COS / Redis 故障 |
| 503 | `"Redis unavailable"` | 仅在调用方在 `auth_resolver` 里这么实现时（推荐做法） |

`auth_resolver` 自身可以在不满足业务前置条件时抛 `HTTPException(...)`，
detail 文案完全由调用方控制；本协议只承诺「至少 401/403/413/400 这些通用码」。

### 1.3 文件上限与超时

- `max_bytes`：上限由调用方 `register_upload_routes(max_bytes=...)` 决定。当超出时
  返回 413 并把进度 state 标记 error = `"too_large"`
- 上传超时：本路由内部对单个上传**没有超时硬上限**（COS 工作线程 join 86400 秒）；
  实际超时由 ASGI / nginx / uvicorn 的 `keepalive` 配置决定。生产部署见
  [OPERATIONS.md](OPERATIONS.md#超时调优)

---

## 2. GET `{route_prefix}/progress` —— SSE 进度

### 2.1 请求

```
GET /api/file_to_url/progress?progress_id=<uuid>
Authorization: Bearer <token>
Accept: text/event-stream
```

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `progress_id` | UUID 字符串 | ✅ | 必须与 POST 请求的 `progress_id` 完全一致 |

`Authorization` **必填**：`auth_resolver(request)` 调用，失败 401/403。

### 2.2 响应

```
HTTP/1.1 200 OK
Content-Type: text/event-stream
Cache-Control: no-store, no-transform
Connection: keep-alive
X-Accel-Buffering: no
```

帧格式：

```
data: <single-line JSON>\n\n
data: <single-line JSON>\n\n
...
```

每帧一个 JSON 对象，下行 0.12 秒一帧（默认；由 `sse_keepalive_interval_sec` 控制）。

### 2.3 SSE 帧字段

```ts
interface ProgressFrame {
  server_receive: {
    bytes_done: number;          // 已入网到服务端的字节数（单调递增）
    bytes_total: number | null;  // POST 时 expected_bytes 设了就有；否则 null 直到入网完成
    done: boolean;               // 入网是否结束
  };
  cos_upload: {
    bytes_done: number;          // 已确认上传到 COS 的字节数（单调递增；含未刷的内存缓冲，避免 < part_size 时长期 0）
    bytes_total: number | null;  // 同 server_receive.bytes_total，入网完成后会被同步为最终字节数
    done: boolean;               // COS 上传是否结束
  };
  done: boolean;                 // 整体终态：仅当成功 finish_ok 或失败 finish_err 后为 true
  error: string | null;          // 失败原因（finish_err 写入）；最长 800 字符；正常路径恒为 null
  _sse_tick: number;             // 单调递增计数；仅 SSE 帧有此字段，方便客户端去重
}
```

**终止条件**：`done == true` 时本帧是最后一帧，服务端关闭连接。

**字段约束**：
- `bytes_done` 单调非递减；任意时刻 `bytes_done <= bytes_total`（如果 `bytes_total` 已知）
- `cos_upload.bytes_total` 在入网完成后通过 `server_receive_done(pid, total)` 同步设值
- `error` 仅可能在 `done == true` 时为非 null

### 2.4 Owner 模型

每个 `progress_id` 在 Redis 上有一个 owner（`user_id` 字符串），由这条规则确立：

1. **POST 是首次见到该 progress_id**：当前请求的 `auth_resolver` 解析出的 user_id 成为 owner
2. **GET 早于 POST 到达**（典型场景：浏览器 EventSource 比 XHR 快几毫秒）：本 GET 路由在没有 owner 时会**惰性登记**当前 user_id 为 owner
3. **后续任何请求（POST 或 GET）**：必须由同一 user_id 发起，否则返回 403 `"progress_id not owned by current user"`

owner 在 Redis 上的 TTL 与进度 state 一致（默认 7200 秒）。

### 2.5 SSE 错误响应

SSE 路由可能在**握手阶段**返回 HTTP 错误码（之后才进入 `text/event-stream` 流）：

| HTTP | `detail` | 触发条件 |
|---|---|---|
| 400 | `"Invalid progress_id"` | UUID 非法 |
| 400 | `"progress_id required"` | 空字符串 |
| 401/403 | (auth_resolver 自定文案) | token 解析失败 |
| 403 | `"progress_id not owned by current user"` | 跨用户访问 |

进入流之后的失败由帧内 `error` 字段携带，HTTP 状态码不会改变。

### 2.6 客户端实现指南

#### 2.6.1 浏览器（XHR + EventSource，可同时跑）

```javascript
const progressId = crypto.randomUUID();
const file = inputEl.files[0];

// 1) 先开 SSE 监听（即使 POST 还没发也能惰性登记 owner）
const sse = new EventSource(
  `/api/file_to_url/progress?progress_id=${progressId}`,
  { withCredentials: true }
);
sse.onmessage = (ev) => {
  const f = JSON.parse(ev.data);
  console.log("server_receive", f.server_receive.bytes_done);
  console.log("cos_upload",     f.cos_upload.bytes_done);
  if (f.done) {
    if (f.error) console.error("upload failed:", f.error);
    sse.close();
  }
};

// 注意：浏览器原生 EventSource 不支持自定义 header，Authorization 拿不到。
// 解决：要么把 token 放 cookie；要么用 fetch + ReadableStream 自己实现 SSE 解析。

// 2) 发 POST（XHR 才能拿到 upload progress）
const xhr = new XMLHttpRequest();
xhr.open("POST", `/api/file_to_url?progress_id=${progressId}&expected_bytes=${file.size}`);
xhr.setRequestHeader("Authorization", `Bearer ${token}`);
xhr.upload.onprogress = (e) => {
  if (e.lengthComputable) console.log("xhr upload", e.loaded, "/", e.total);
};
xhr.onload = () => {
  if (xhr.status === 200) {
    const { url } = JSON.parse(xhr.responseText);
    console.log("uploaded:", url);
  }
};
const fd = new FormData();
fd.append("folder", "my-folder");
fd.append("file", file);
xhr.send(fd);
```

**关键点**：
- 浏览器 `xhr.upload.onprogress` 报「客户端 → 服务端入网」，但只有在请求**完全发出去**之前才有事件；之后服务端 → COS 阶段就需要 SSE
- SSE `Authorization` header 限制：浏览器 `EventSource` 不支持设 header；要么 cookie auth，要么用 `fetch` + `ReadableStream` 自己解 `data: ...\n\n`

#### 2.6.2 Python 客户端

```python
import httpx
import uuid

progress_id = str(uuid.uuid4())
total = 1024 * 1024 * 100  # 100MB

# 后台开 SSE
def watch_sse():
    with httpx.stream(
        "GET",
        f"http://server/api/file_to_url/progress?progress_id={progress_id}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=None,
    ) as r:
        for line in r.iter_lines():
            if line.startswith("data: "):
                frame = json.loads(line[6:])
                print(frame)
                if frame["done"]:
                    return frame

# POST
with open("file.bin", "rb") as f:
    r = httpx.post(
        f"http://server/api/file_to_url?progress_id={progress_id}&expected_bytes={total}",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("file.bin", f, "application/octet-stream")},
        data={"folder": "uploads"},
        timeout=None,
    )
print(r.json()["url"])
```

#### 2.6.3 curl（debug）

```bash
PROGRESS_ID=$(uuidgen | tr A-Z a-z)

# 后台 SSE
curl -N \
  -H "Authorization: Bearer $TOKEN" \
  "http://server/api/file_to_url/progress?progress_id=$PROGRESS_ID" &

# POST 上传
curl -X POST \
  -H "Authorization: Bearer $TOKEN" \
  -F "folder=test" \
  -F "file=@/path/to/file.bin" \
  "http://server/api/file_to_url?progress_id=$PROGRESS_ID"
```

---

## 3. 公开协议稳定性

下列变更将触发 **major version bump**（破坏性变更）：

- POST 必填字段（`file`）的字段名 / 含义变化
- 任意 SSE 帧 top-level key（`server_receive` / `cos_upload` / `done` / `error`）的字段名或形状变化
- POST 错误码（401/403/413/400）的语义变化
- progress_id owner 模型的语义变化

下列变更视为**兼容**（minor / patch bump）：

- 新增 SSE 帧字段（如未来加 `cos_upload.parts_done`）
- 新增可选 query / form 字段
- `_sse_tick` 取值的具体节奏调整
- 新增 HTTP 错误状态码（如新增 429）
- 错误 detail 文本的微调（消费方应只匹配状态码，不依赖 detail 文本作分支）

参见 [`CHANGELOG.md`](../CHANGELOG.md)。
