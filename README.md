# FlowUploadModule

Python 端「高可用文件上传」可复用模块。把腾讯云 COS 上传 + 流式分片 +
进度反馈这套基础设施从特定业务里剥出来，做成跨应用可复用的 submodule。

代码源自 Flops（`Xenotech-Studio/Flops`）—— 上传 primitives 原本因为头像
上传顺手塞进了 `user_system/cos_upload.py`；HTTP/Web 层（流式 multipart +
Redis 进度 + SSE）原本散落在 Flops `server.py`。本仓库把这两层都抽出来。

---

## 当前能力

### `flow_upload`（顶层 = 纯传输层）

- `file_to_url(file, folder_name, bucket, ...)` —— UploadFile 一次性 `put_object`
- `bytes_to_cos_url(body, ...)` —— 内存 bytes 一次性 `put_object`
- `multipart_upload_from_chunk_queue(q, ...)` —— 从队列消费式 multipart 边收边传
- `get_tencent_credentials()` / `set_credentials_provider(fn)` —— 凭证统一入口
- `DEFAULT_BUCKET` / `DEFAULT_REGION` / `DEFAULT_SCHEME`

不依赖 fastapi。

### `flow_upload.http`（HTTP/FastAPI 层）

- `register_upload_routes(app, ...)` —— 一次注册：
  - `POST {prefix}` —— multipart 上传，可选 `?progress_id=...&expected_bytes=...`
  - `GET  {prefix}/progress` —— SSE 推 `server_receive` + `cos_upload` 双通道
- `ObjectKeyContext` / `ObjectKeyStrategy` —— 业务策略接口
- `ProgressStore` —— 进度状态机（Redis 双通道）

业务策略（CHAT_FILES / AVATARS / DESKTOP_UPDATES …）与身份系统（token →
user_id）通过参数注入，本模块对这些一无所知。

依赖 fastapi / starlette / python_multipart（仅 import http 子包时才需要）。

---

## 路线图

- [x] **Phase 1**：纯传输层 primitives
- [x] **Phase 2**：HTTP/Web 层抽象（流式 multipart + Redis 双通道进度 + SSE 路由）
- [ ] **Phase 3**：执行端 SSE forwarder（Python）通用化（替代 Flops
      `runtime_service.py:_forward_f2u_cos_progress_sse` 中的私货部分）

前端 SDK 见配套 submodule：
[FlowUploadModuleSDK](https://github.com/Xenotech-Studio/FlowUploadModuleSDK)（XHR 上传 + SSE 进度订阅）。

---

## 集成示例

### 作为 git submodule 接入

```bash
git submodule add git@github.com:Xenotech-Studio/FlowUploadModule.git flow_upload
```

确保宿主项目 `sys.path` 能找到 repo 父目录。

### 注入凭证（推荐）

```python
from flow_upload import set_credentials_provider

def _provider():
    import config
    return config.TENCENT_SECRET_ID, config.TENCENT_SECRET_KEY

set_credentials_provider(_provider)
```

未注入 provider 时按以下顺序回退：宿主 `config.py` → 环境变量
`TENCENT_SECRET_ID / TENCENT_SECRET_KEY`。都没有时抛 `RuntimeError`。

### 注册 HTTP 路由

```python
from flow_upload.http import register_upload_routes, ObjectKeyContext

def _chat_object_key_strategy(ctx: ObjectKeyContext):
    user = ctx.user_id or "anon"
    return f"CHAT_FILES/{user}", f"{ctx.progress_id or 'x'}_{ctx.safe_name}"

register_upload_routes(
    app,
    redis_for_progress=redis_user,
    auth_resolver=resolve_bearer_user_id,           # 必填 / 严格态
    auth_resolver_optional=try_bearer_user_id,      # 可选 / 软态
    object_key_strategy=_chat_object_key_strategy,
    max_bytes=20 * 1024 * 1024 * 1024,
    max_concurrent=8,
    route_prefix="/api/file_to_url",
    redis_state_prefix="myapp:f2u_prog:",           # 迁移老数据时保持原前缀
    redis_owner_prefix="myapp:f2u_prog_owner:",
    extra_form_fields=["conversation_id"],
)
```

### 进度协议（公开契约）

POST 后客户端拿同一 `progress_id` 走 SSE：

```
GET /api/file_to_url/progress?progress_id=<uuid>
Authorization: Bearer <token>
Accept: text/event-stream
```

每帧 JSON：

```json
{
  "server_receive": {"bytes_done": 123, "bytes_total": 456, "done": false},
  "cos_upload":     {"bytes_done":  64, "bytes_total": 456, "done": false},
  "done": false,
  "error": null,
  "_sse_tick": 12
}
```

`server_receive` 是「客户端 → 服务端入网」字节进度；`cos_upload` 是「服务端 →
腾讯云 COS」字节进度。两者 done 都 true 时整体 done。

---

## 设计取向

- **不绑业务**：上传到 COS 内的「哪个目录、什么对象名」由调用方注入策略，
  本模块不维护任何业务命名（CHAT_FILES / AVATARS / DESKTOP_UPDATES …）。
- **不绑身份系统**：用户态、token 解析由 `auth_resolver(_optional)` 注入。
- **不绑 form 字段**：除了约定的 `folder` 和 `file`，业务想要的字段
  （`conversation_id` / `doc_id` / …）通过 `extra_form_fields` 声明并由
  策略读取，本模块不知道任何业务字段含义。
- **凭证不硬编码**：仓库内不存任何 SecretId/Key 默认值。
