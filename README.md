# FlowUploadModule

> 高可用文件上传模块（腾讯云 COS）：纯传输层 primitives + FastAPI 流式 multipart +
> Redis 双通道进度 + SSE 路由。**业务、身份系统、表单字段、凭证**全部通过参数注入，
> 模块对宿主一无所知。

| 文档 | 用途 |
|---|---|
| **[`docs/API.md`](docs/API.md)** | 每个公开符号的完整签名 / 参数 / 返回 / 异常 / 示例 |
| **[`docs/PROTOCOL.md`](docs/PROTOCOL.md)** | HTTP / SSE 公开协议规格（跨语言客户端契约） |
| **[`docs/RECIPES.md`](docs/RECIPES.md)** | 业务接入 cookbook（chat / avatar / desktop release / paper 导入） |
| **[`docs/OPERATIONS.md`](docs/OPERATIONS.md)** | 部署 / 运维（nginx、worker、Redis、超时、凭证） |
| **[`CHANGELOG.md`](CHANGELOG.md)** | 版本变更（SemVer） |

---

## 这个模块在解决什么

- 你想给前端开一条 `/api/file_to_url` 这样的 multipart 上传路由，并且**支持大文件 + 实时进度**
- 文件最终落到腾讯云 COS，URL 直接拿回前端用
- 进度信息要分两段：「客户端 → 服务端入网」 + 「服务端 → COS 上行」（不是一段假进度）
- 不希望把上传逻辑跟你的 user 系统、auth 系统、Redis 命名空间、业务命名（聊天 / 头像 / 发布包 / ...）耦合在一起

如果以上都对，这个模块就是你要的。

## 这个模块**不**做什么

- 不做客户端 SDK（前端 XHR + EventSource 是你自己写；协议见 [PROTOCOL.md](docs/PROTOCOL.md)；
  配套前端 submodule [FlowUploadModuleSDK](https://github.com/Xenotech-Studio/FlowUploadModuleSDK) 计划中）
- 不做 token / 鉴权（由你注入 `auth_resolver`）
- 不做业务命名（CHAT_FILES / AVATARS 这些目录由你的 `ObjectKeyStrategy` 闭包决定）
- 不内置 form 字段（除约定的 `folder` + `file`，业务字段由 `extra_form_fields` 声明）
- 不存任何凭证（查找链：注入 provider → `config.py` → 环境变量）

---

## 5 分钟接入

### 1. 作为 git submodule 接入

```bash
git submodule add git@github.com:Xenotech-Studio/FlowUploadModule.git flow_upload
```

确保宿主项目 `sys.path` 包含 repo 父目录，使 `from flow_upload import ...` 工作。

依赖（仅在 import 对应层时生效）：

| 层 | 依赖 |
|---|---|
| `flow_upload`（顶层）| `qcloud-cos-sdk-python` |
| `flow_upload.http` | 上面 + `fastapi` + `python-multipart` |

### 2. 注入凭证（一次）

```python
from flow_upload import set_credentials_provider

def _provider():
    import config                              # 宿主自己的 config.py
    return config.TENCENT_SECRET_ID, config.TENCENT_SECRET_KEY

set_credentials_provider(_provider)
```

未注入时按 `config.py` → 环境变量回退。三层都拿不到 → `RuntimeError`。

### 3. 注册路由

```python
from fastapi import FastAPI, HTTPException, Request
from redis import Redis
from flow_upload.http import register_upload_routes, ObjectKeyContext

app = FastAPI()
redis_user = Redis(host="127.0.0.1", port=6379, db=7, decode_responses=False)


def auth_strict(request: Request) -> str:
    auth = request.headers.get("authorization") or ""
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "Authorization required")
    user_id = my_lookup_user_by_token(auth[7:].strip())
    if not user_id:
        raise HTTPException(403, "Invalid token")
    return user_id


def auth_optional(request):
    try: return auth_strict(request)
    except HTTPException: return None


def my_strategy(ctx: ObjectKeyContext):
    user = ctx.user_id or "anon"
    return f"UPLOADS/{user}", ctx.safe_name


register_upload_routes(
    app,
    redis_for_progress=redis_user,                    # 也可传 lambda: redis_user 实现迟绑定
    auth_resolver=auth_strict,
    auth_resolver_optional=auth_optional,
    object_key_strategy=my_strategy,
    max_bytes=20 * 1024 * 1024 * 1024,                # 20 GiB
    bucket="my-bucket-1234567890",
    extra_form_fields=["conversation_id"],            # 可选，按业务声明
)
```

完成。前端就能 POST `/api/file_to_url?progress_id=<uuid>` 上传 + GET
`/api/file_to_url/progress?progress_id=<uuid>` 订阅 SSE 进度。

完整可运行示例（含 chat / avatar / release / paper 4 种业务场景）见 [RECIPES.md](docs/RECIPES.md)。

---

## 公开符号速览

```python
# 顶层（不依赖 fastapi）
from flow_upload import (
    file_to_url,                       # UploadFile 一次性 put_object
    bytes_to_cos_url,                  # 内存 bytes 一次性 put_object
    multipart_upload_from_chunk_queue, # 队列消费式 multipart 边收边传
    get_tencent_credentials,           # (str, str)
    set_credentials_provider,          # 注入凭证（最高优先级）
    CredentialsProvider,               # 类型别名
    DEFAULT_BUCKET, DEFAULT_REGION, DEFAULT_SCHEME,
)

# HTTP / FastAPI 层
from flow_upload.http import (
    register_upload_routes,            # 一次注册 POST + SSE GET 两条路由
    ObjectKeyContext,                  # 业务策略输入 dataclass
    ObjectKeyStrategy,                 # 类型别名
    ProgressStore,                     # 进度状态机（通常不用直接构造）
)
```

每个符号的完整签名、参数、异常、示例：[`docs/API.md`](docs/API.md)。

---

## 架构概览

```
┌────────────────────────────────────────────────────────────────────┐
│  你的 FastAPI 应用                                                  │
│                                                                    │
│   register_upload_routes(app, ...)                                 │
│           │                                                        │
│           ├─► auth_resolver(request)               【你注入】       │
│           ├─► object_key_strategy(ctx) → (folder, name) 【你注入】  │
│           └─► redis_for_progress                   【你注入】       │
│                                                                    │
└─────────────────────────┬──────────────────────────────────────────┘
                          ▼
┌────────────────────────────────────────────────────────────────────┐
│  flow_upload.http                                                  │
│                                                                    │
│   POST /api/file_to_url     GET /api/file_to_url/progress          │
│        │                          │                                │
│        ▼                          ▼                                │
│   stream.py            ←——→  routes.py（SSE 帧生成）                │
│   ┌──────────────┐                │                                │
│   │ MultipartParser  │  ←———————   │ ProgressStore（progress.py）  │
│   │ + chunk queue   │              │   ┌─────────────┐             │
│   └──────────────┘                  │   │   Redis     │             │
│        │ on_*_bytes 回调              │   │ 双通道 JSON │             │
│        ▼                            │   └─────────────┘             │
│   COS 工作线程（threading.Thread）   │                                │
│        │                            │                                │
│        ▼                            │                                │
│   multipart_upload_from_chunk_queue │                                │
│   （cos_upload.py 顶层）            │                                │
└────────────────────────────────────────────────────────────────────┘
                          │
                          ▼
                ┌─────────────────┐
                │  腾讯云 COS     │
                └─────────────────┘
```

**两条独立的写入路径**：
- `server_receive` 由 asyncio 事件循环里 multipart 解析推进
- `cos_upload` 由 COS 工作线程在 `multipart_upload_from_chunk_queue` 里推进

`ProgressStore` 给同一个 `progress_id` 的所有读写都加锁，避免两条路径互相覆盖。

---

## 设计取向

- **不绑业务**：上传到 COS 内的「哪个目录、什么对象名」由 `ObjectKeyStrategy` 闭包决定
- **不绑身份系统**：用户态、token 解析由 `auth_resolver(_optional)` 注入
- **不绑 form 字段**：除约定的 `folder` + `file`，业务字段（`conversation_id` / `doc_id` / `version` / ...）通过 `extra_form_fields` 声明，模块不知道任何业务字段含义
- **顶层无 fastapi 依赖**：`from flow_upload import ...` 只需 qcloud-cos-sdk-python，方便服务端到服务端的脚本场景复用
- **凭证不硬编码**：仓库内不存任何 SecretId / Key 默认值

---

## 路线图

- [x] **0.1.0**：纯传输层 primitives
- [x] **0.2.0**：HTTP/Web 层抽象（流式 multipart + Redis 双通道进度 + SSE 路由）+ 完整文档
- [ ] **0.3.0**：执行端 SSE forwarder（Python）通用化（聚合多端进度的转发器，待抽离）
- [ ] **0.4.0**：JS SDK（[FlowUploadModuleSDK](https://github.com/Xenotech-Studio/FlowUploadModuleSDK)）—— 浏览器 / Electron 端的 XHR + EventSource 封装

详见 [CHANGELOG.md](CHANGELOG.md)。
