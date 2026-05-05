# API Reference

本文档列出 `flow_upload` 与 `flow_upload.http` 下所有公开符号的完整签名、
参数语义、返回值、异常以及最小可运行示例。

阅读路径：

- 「我只想把 bytes / UploadFile 上传到 COS」→ 看 [顶层传输层](#顶层传输层)
- 「我要在 FastAPI 应用里给前端开 multipart 上传 + 进度 SSE」→ 看 [`flow_upload.http`](#flow_uploadhttp)
- 「腾讯云凭证从哪来」→ 看 [凭证管理](#凭证管理)

> 协议字段、错误码、客户端实现指南：见 [`PROTOCOL.md`](PROTOCOL.md)。
> 业务策略 cookbook：见 [`RECIPES.md`](RECIPES.md)。

---

## 目录

- [顶层传输层](#顶层传输层)
  - [`file_to_url`](#file_to_url)
  - [`bytes_to_cos_url`](#bytes_to_cos_url)
  - [`multipart_upload_from_chunk_queue`](#multipart_upload_from_chunk_queue)
- [凭证管理](#凭证管理)
  - [`get_tencent_credentials`](#get_tencent_credentials)
  - [`set_credentials_provider`](#set_credentials_provider)
  - [`CredentialsProvider`](#credentialsprovider)
- [常量](#常量)
- [`flow_upload.http`](#flow_uploadhttp)
  - [`register_upload_routes`](#register_upload_routes)
  - [`ObjectKeyContext`](#objectkeycontext)
  - [`ObjectKeyStrategy`](#objectkeystrategy)
  - [`ProgressStore`](#progressstore)

---

## 顶层传输层

不依赖 fastapi。`from flow_upload import ...` 即可。

### `file_to_url`

把 FastAPI / Starlette 风格的 `UploadFile` 一次性 `put_object` 到腾讯云 COS。

```python
def file_to_url(
    file,
    folder_name: str = "",
    bucket: str = DEFAULT_BUCKET,
    cos_filename: Optional[str] = None,
    download_filename: Optional[str] = None,
) -> str
```

| 参数 | 类型 | 说明 |
|---|---|---|
| `file` | `UploadFile`-like | 必须有 `.filename: str` 属性和 `.file` 文件流（同步可读） |
| `folder_name` | `str` | COS key 前缀目录（不含末段文件名）；末尾自动补 `/`；空串视为根 |
| `bucket` | `str` | COS bucket 名；默认 `DEFAULT_BUCKET` |
| `cos_filename` | `str \| None` | COS key 末段；不传则取 `file.filename` 的 basename |
| `download_filename` | `str \| None` | 设置 `Content-Disposition: attachment; filename="..."`，让浏览器下载用指定名字 |

**返回**：`str` —— 公网 HTTPS URL（如 `https://flowtask-1302933783.cos.ap-guangzhou.myqcloud.com/AVATARS/abc.png`）。

**异常**：
- `RuntimeError` —— 凭证未配置（参见 [凭证管理](#凭证管理)）
- `qcloud_cos.cos_exception.CosClientError` / `CosServiceError` —— 网络 / 鉴权 / 桶不存在等

**示例**：

```python
from flow_upload import file_to_url

# FastAPI 路由内（避免使用 register_upload_routes 而想自己处理 multipart 时）
@app.post("/upload_avatar")
async def upload_avatar(file: UploadFile = File(...)):
    url = file_to_url(file, folder_name="AVATARS", bucket="my-bucket-1234567890")
    return {"url": url}
```

**注意**：
- `file.file` 是一个**同步文件流**；本函数是同步执行的，会阻塞当前线程。在 asyncio 路由里**应**用 `await asyncio.to_thread(file_to_url, ...)` 包一层。
- 大文件（>几百 MB）建议改走 [`multipart_upload_from_chunk_queue`](#multipart_upload_from_chunk_queue)，此函数会把整个流读完后一次 `put_object`，COS 一次性上行可能受网络抖动影响。

---

### `bytes_to_cos_url`

把内存中的字节上传到 COS。

```python
def bytes_to_cos_url(
    body: bytes,
    *,
    folder_name: str = "",
    object_name: str = "file.bin",
    bucket: str = DEFAULT_BUCKET,
    content_type: Optional[str] = None,
    on_body_read_progress: Optional[Callable[[int, int], None]] = None,
) -> str
```

| 参数 | 类型 | 说明 |
|---|---|---|
| `body` | `bytes` | 完整字节内容（位置参数） |
| `folder_name` | `str` | 同 `file_to_url` |
| `object_name` | `str` | COS key 末段；只取 basename，传 `"a/b/c"` 会被压成 `"c"` |
| `bucket` | `str` | COS bucket；默认 `DEFAULT_BUCKET` |
| `content_type` | `str \| None` | 设置 `Content-Type` 响应头；空字符串 / `None` 都不设 |
| `on_body_read_progress` | `(int, int) -> None \| None` | COS SDK 每次从 Body 读取后回调 `(bytes_consumed, bytes_total)` |

**返回**：`str` —— 公网 HTTPS URL。

**异常**：同 `file_to_url`。

**示例**：

```python
from flow_upload import bytes_to_cos_url

pdf_bytes = await download_pdf(url)
url = bytes_to_cos_url(
    pdf_bytes,
    folder_name="PAPERS",
    object_name=f"{paper_id}.pdf",
    content_type="application/pdf",
)
```

**注意**：上传完整 `bytes` 时所有内容驻留内存。100 MB 以上数据应改用 [`multipart_upload_from_chunk_queue`](#multipart_upload_from_chunk_queue)。

---

### `multipart_upload_from_chunk_queue`

从队列消费 `(kind, payload)` 元组，边收边走 COS multipart。适合「网络流入网与 COS 上传并行」的场景（典型：HTTP 上传代理、大文件流转）。

```python
def multipart_upload_from_chunk_queue(
    q: "Queue[Tuple[str, Any]]",
    *,
    folder_name: str = "",
    object_name: str = "file.bin",
    bucket: str = DEFAULT_BUCKET,
    content_type: Optional[str] = None,
    on_cos_bytes: Optional[Callable[[int], None]] = None,
    part_size: int = 5 * 1024 * 1024,
) -> str
```

**队列协议**：本函数**消费**端，调用方在另一线程 / 协程往 `q` 里 `put`：

| `kind` | `payload` | 含义 |
|---|---|---|
| `"data"` | `bytes` / `bytearray` | 一个数据片段，会进入 multipart buffer |
| `"end"` | `None` | 上游数据流结束信号；本函数收到后冲掉缓冲 + `complete_multipart_upload` |
| `"err"` | `str` | 上游失败；本函数 `abort_multipart_upload` 后抛 `ValueError(payload)` |

| 参数 | 类型 | 说明 |
|---|---|---|
| `q` | `queue.Queue[Tuple[str, Any]]` | 同步 queue；建议 `maxsize` 控制内存（如 256） |
| `folder_name` / `object_name` / `bucket` / `content_type` | 同 `bytes_to_cos_url` ||
| `on_cos_bytes` | `(int) -> None \| None` | 回调「累计已确认 + 缓冲未上传」字节数（单调递增）|
| `part_size` | `int` | 单个 part 最小字节；默认 5 MiB（COS 最小要求） |

**返回**：`str` —— 公网 HTTPS URL。

**异常**：
- `ValueError(msg)` —— 收到 `("err", msg)` 元组时透传
- `RuntimeError` —— 凭证未配置 / 内部状态错误
- `qcloud_cos.cos_exception.*` —— COS SDK 错误

**行为约束**：
- 整个流字节数 < `part_size` 时退化成 `put_object`（不会建 multipart，省去 commit/abort 开销）
- 收到 `"err"` 时尝试 `abort_multipart_upload` 释放 COS 端的 part；失败静默
- 函数同步执行，会阻塞当前线程；典型用法是放入 `threading.Thread` 后台跑

**最小示例**：

```python
import queue
import threading
from flow_upload import multipart_upload_from_chunk_queue

q: queue.Queue = queue.Queue(maxsize=256)
result = {"url": None, "err": None}

def _consumer():
    try:
        result["url"] = multipart_upload_from_chunk_queue(
            q,
            folder_name="UPLOADS",
            object_name="big.bin",
            on_cos_bytes=lambda n: print(f"COS uploaded: {n}"),
        )
    except Exception as e:
        result["err"] = e

t = threading.Thread(target=_consumer, daemon=True)
t.start()

# 在另一线程 / 协程里推数据
for chunk in stream_data():
    q.put(("data", chunk))
q.put(("end", None))

t.join()
if result["err"]:
    raise result["err"]
print(result["url"])
```

---

## 凭证管理

腾讯云 `SecretId` / `SecretKey` 的查找链（高 → 低优先级）：

1. 通过 [`set_credentials_provider`](#set_credentials_provider) 注入的 callable
2. 宿主项目 `config.py` 的 `TENCENT_SECRET_ID` / `TENCENT_SECRET_KEY`（通过 `import config` 读取）
3. 环境变量 `TENCENT_SECRET_ID` / `TENCENT_SECRET_KEY`

都拿不到时调用任何上传函数都会 raise `RuntimeError`。模块**没有**任何硬编码默认 SecretId/Key。

### `get_tencent_credentials`

```python
def get_tencent_credentials() -> Tuple[str, str]
```

**返回**：`(secret_id, secret_key)` —— 两个非空字符串。

**异常**：
- `RuntimeError` —— 整条查找链都没拿到非空凭证；message 提示三种解决路径
- 注入 provider 自身抛错时，原异常包装成 `RuntimeError("FlowUpload credentials provider raised: ...")`

**用法**：腾讯云联网搜索（SearchPro）等其他需要同对凭证的地方可以直接复用，避免重复实现查找链。

```python
from flow_upload import get_tencent_credentials

secret_id, secret_key = get_tencent_credentials()
```

---

### `set_credentials_provider`

```python
def set_credentials_provider(fn: Optional[CredentialsProvider]) -> None
```

注入凭证 provider；最高优先级。`fn` 是 0 参 callable，返回 `(secret_id, secret_key)`。

| 参数 | 类型 | 说明 |
|---|---|---|
| `fn` | `Callable[[], Tuple[str, str]]` 或 `None` | 传 `None` 清除注入，回退到 config / 环境变量查找路径 |

**注入时机**：本函数是模块级单例 setter；建议在应用启动时调用一次，不要在请求路径上反复切换。

```python
from flow_upload import set_credentials_provider

def _provider():
    # 推荐：把读凭证的来源集中在一处，方便 secret rotate 时改一个地方
    import config
    return config.TENCENT_SECRET_ID, config.TENCENT_SECRET_KEY

set_credentials_provider(_provider)
```

---

### `CredentialsProvider`

类型别名：

```python
CredentialsProvider = Callable[[], Tuple[str, str]]
```

仅作类型提示用。

---

## 常量

```python
DEFAULT_BUCKET = "flowtask-1302933783"
DEFAULT_REGION = "ap-guangzhou"
DEFAULT_SCHEME = "https"
```

`DEFAULT_BUCKET` 是 Flops 历史默认值；新接入方应在所有上传函数里**显式传 `bucket=...`**，不要依赖默认。`DEFAULT_REGION` / `DEFAULT_SCHEME` 一般不需要改。

---

## `flow_upload.http`

依赖 `fastapi` / `starlette` / `python_multipart`（仅 import http subpackage 时生效）。

`from flow_upload.http import ...`。

### `register_upload_routes`

**一次注册两条路由**：

- `POST {route_prefix}` —— multipart 上传（可选 `?progress_id=<uuid>&expected_bytes=<int>`）
- `GET {route_prefix}/progress?progress_id=<uuid>` —— SSE 推送 `server_receive` + `cos_upload` 双通道进度

完整签名：

```python
def register_upload_routes(
    app: FastAPI,
    *,
    redis_for_progress: Any,
    auth_resolver: Callable[[Request], str],
    auth_resolver_optional: Optional[Callable[[Request], Optional[str]]] = None,
    object_key_strategy: ObjectKeyStrategy,
    max_bytes: int,
    bucket: Optional[str] = None,
    max_concurrent: int = 8,
    route_prefix: str = "/api/file_to_url",
    redis_state_prefix: str = "flow_upload:f2u_prog:",
    redis_owner_prefix: str = "flow_upload:f2u_prog_owner:",
    redis_state_ttl_sec: int = 7200,
    extra_form_fields: Optional[List[str]] = None,
    sse_keepalive_interval_sec: float = 0.12,
    sse_max_ticks: int = 48000,
) -> ProgressStore
```

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `app` | `FastAPI` | ✅ | 路由会被注册到的 FastAPI 实例 |
| `redis_for_progress` | `Redis` 实例 **或** `Callable[[], Redis]` | ✅ | 进度状态存储；传 callable 实现迟绑定（适合宿主在 startup 才初始化 Redis） |
| `auth_resolver` | `(Request) -> str` | ✅ | 解析当前请求的 user_id；解析失败应直接 raise `HTTPException(401/403)` |
| `auth_resolver_optional` | `(Request) -> str \| None` | ❌ | 软态 resolver；用在「无 progress_id」上传分支以便策略闭包仍能拿到 user_id；缺省时该分支 user_id 总是 `None` |
| `object_key_strategy` | `ObjectKeyStrategy` | ✅ | 业务策略，把 `ObjectKeyContext` 映射成 `(folder_prefix, object_name)` |
| `max_bytes` | `int` | ✅ | 单文件硬上限（字节）；超过时返回 HTTP 413 |
| `bucket` | `str \| None` | ❌ | 上传目标 COS bucket；`None` 时使用 `DEFAULT_BUCKET` |
| `max_concurrent` | `int` | ❌ | 同时进入 COS 工作的最大上传数（per-process semaphore）；默认 8 |
| `route_prefix` | `str` | ❌ | POST 路径；GET 进度自动派生为 `{prefix}/progress`；默认 `/api/file_to_url` |
| `redis_state_prefix` | `str` | ❌ | Redis 进度 JSON key 前缀；默认 `flow_upload:f2u_prog:` |
| `redis_owner_prefix` | `str` | ❌ | Redis owner 映射 key 前缀；默认 `flow_upload:f2u_prog_owner:` |
| `redis_state_ttl_sec` | `int` | ❌ | 进度 / owner 在 Redis 上的过期时间；默认 7200（2 小时） |
| `extra_form_fields` | `List[str] \| None` | ❌ | 除 `folder` 之外要解析并塞进 `ObjectKeyContext.form_fields` 的 multipart 字段名 |
| `sse_keepalive_interval_sec` | `float` | ❌ | SSE 帧间隔；默认 0.12 秒 |
| `sse_max_ticks` | `int` | ❌ | SSE 最大帧数（防止无限循环）；默认 48000，对应约 96 分钟 |

**返回**：`ProgressStore` 实例 —— 调用方可以在自定义异步任务中复用同一进度命名空间，比如手动写自定义 finish_err。

**异常（注册期）**：
- `ValueError` —— `max_bytes <= 0` 或 `redis_for_progress is None`

**对外契约（运行期）**：见 [`PROTOCOL.md`](PROTOCOL.md)。

**最小示例**：

```python
from fastapi import FastAPI, HTTPException, Request
from redis import Redis
from flow_upload import set_credentials_provider
from flow_upload.http import register_upload_routes, ObjectKeyContext

app = FastAPI()
redis_user = Redis(host="127.0.0.1", port=6379, db=7, decode_responses=False)

# 1) 凭证 provider
def _creds():
    import config
    return config.TENCENT_SECRET_ID, config.TENCENT_SECRET_KEY
set_credentials_provider(_creds)

# 2) 身份解析（按你的 auth 系统改）
def auth_strict(request: Request) -> str:
    auth = request.headers.get("authorization") or ""
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "Authorization required")
    user_id = my_lookup_user_by_token(auth[7:])
    if not user_id:
        raise HTTPException(403, "Invalid token")
    return user_id

def auth_optional(request: Request):
    try:
        return auth_strict(request)
    except HTTPException:
        return None

# 3) 业务策略
def my_strategy(ctx: ObjectKeyContext):
    user = ctx.user_id or "anon"
    return f"UPLOADS/{user}", ctx.safe_name

# 4) 注册
register_upload_routes(
    app,
    redis_for_progress=redis_user,
    auth_resolver=auth_strict,
    auth_resolver_optional=auth_optional,
    object_key_strategy=my_strategy,
    max_bytes=20 * 1024 * 1024 * 1024,  # 20 GiB
    bucket="my-bucket-1234567890",
)
```

---

### `ObjectKeyContext`

业务策略的输入。冻结 dataclass。

```python
@dataclass(frozen=True)
class ObjectKeyContext:
    user_id: Optional[str]
    progress_id: Optional[str]
    safe_name: str
    started_ms: int
    form_fields: Dict[str, str]
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `user_id` | `str \| None` | `auth_resolver` 解析出的当前用户 id；无 `progress_id` 且 `auth_resolver_optional` 缺省时为 `None` |
| `progress_id` | `str \| None` | 本次上传的进度通道 id；前端不开进度通道时为 `None` |
| `safe_name` | `str` | multipart 中文件 part 的 filename basename，已防注入 / 截断 512 字符 |
| `started_ms` | `int` | 本次请求处理开始的 Unix ms 时间戳 |
| `form_fields` | `Dict[str, str]` | 解析后的表单字段值；包含 `"folder"` + `register_upload_routes(extra_form_fields=...)` 中声明的所有字段名；未传字段值为空字符串 |

策略闭包从这些字段推导 `(folder_prefix, object_name)` 并返回。

---

### `ObjectKeyStrategy`

类型别名：

```python
ObjectKeyStrategy = Callable[[ObjectKeyContext], Tuple[str, str]]
```

返回 `(folder_prefix, object_name)`：
- `folder_prefix` 末尾**不需要** `/`，传输层会自动补
- `object_name` 仅取 basename，传 `"a/b/c"` 会被切成 `"c"`

策略**应当是纯函数**：相同的 `ObjectKeyContext` 应返回相同的 `(folder_prefix, object_name)`，方便复现 / 调试。

---

### `ProgressStore`

进度状态机，由 `register_upload_routes` 内部创建并返回。**通常你不需要直接构造**；只在希望从其他后台任务（非上传路由）写入 / 读取同一个 progress_id 状态时才会用到。

```python
class ProgressStore:
    def __init__(
        self,
        *,
        redis_client: Union[RedisLike, Callable[[], RedisLike]],
        state_prefix: str,
        owner_prefix: str,
        ttl_sec: int,
    ) -> None
```

公共方法：

| 方法 | 说明 |
|---|---|
| `normalize_pid(raw)` (静态) | UUID 校验；空白返回 `None`，非法格式抛 `ValueError` |
| `get_state(pid)` | 返回当前进度 JSON dict 或 `None`（未登记） |
| `get_owner(pid)` | 返回 `progress_id` 当前 owner 的 user_id 字符串或 `None` |
| `begin(pid, user_id, *, expected_total=None)` | 登记 owner（已存在不同 owner → `PermissionError`）+ 重置 state 为初始空帧 |
| `server_receive_bytes(pid, done, total=None)` | 更新「服务端入网」字节进度（`done=False`） |
| `server_receive_done(pid, total)` | 标记入网完成；同时把 `cos_upload.bytes_total` 写到该值 |
| `cos_uploaded_bytes(pid, done)` | 更新「服务端 → COS」字节进度（`done=False`） |
| `finish_ok(pid, total)` | 终态：双通道 done = true、bytes_done = bytes_total = total、error = null；释放本进程内的 lock |
| `finish_err(pid, err)` | 终态：双通道 done = true、`error: <truncated_to_800>`；释放 lock |

**线程安全**：每个 `progress_id` 持有一个 `threading.Lock`，所有读改写组合在锁下完成。`server_receive` 与 `cos_upload` 两条写入路径并发时不会互相覆盖。

**JSON 形状**：见 [`PROTOCOL.md`](PROTOCOL.md#sse-帧字段)。
