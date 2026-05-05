# Recipes

四个常见业务场景的接入模板。每个 recipe 给完整可拷贝代码，**不假设**你看过任何
其它项目（Flops 等）—— 只看 recipe 本身就能 work。

| Recipe | 是否要 progress_id | extra_form_fields | 典型 COS 路径 |
|---|---|---|---|
| [1. 聊天附件](#recipe-1-聊天附件) | ✅ 大文件需要 | `["conversation_id"]` | `CHAT_FILES/<user>/<8hex>_<YYMMDDHHmm>_<filename>` |
| [2. 用户头像](#recipe-2-用户头像) | ❌ | 无 | `AVATARS/<user_id>.png` |
| [3. 桌面端发布包](#recipe-3-桌面端发布包) | ❌（管理端通常一次性） | `["channel", "platform", "version"]` | `DESKTOP_UPDATES/<channel>/<platform>/<version>/<filename>` |
| [4. PDF / Paper 导入](#recipe-4-pdf--paper-导入)（用 `bytes_to_cos_url`） | N/A | N/A | `PAPERS/<paper_id>.pdf` |

---

## Recipe 1：聊天附件

**业务规则**：
- 任何登录用户都能上传任意附件给当前会话
- 同一用户的所有聊天附件落到 `CHAT_FILES/<user_id>/...` 目录
- 文件名前加 8 hex 随机前缀 + 分钟时间戳，避免同名碰撞 + 方便人肉调试找到当时的上传
- 大文件（图片 / 视频 / 长 PDF）必须有进度反馈
- 同一 `conversation_id` 的附件可以分组（虽然这里只是放进 form 给策略闭包看，
  实际是否分组取决于后续业务怎么用）

**完整代码**：

```python
"""
Chat attachments uploader.
依赖：fastapi、redis、qcloud_cos、python_multipart
"""

import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

from fastapi import FastAPI, HTTPException, Request
from redis import Redis

from flow_upload import set_credentials_provider
from flow_upload.http import register_upload_routes, ObjectKeyContext

app = FastAPI()
redis_user = Redis(host="127.0.0.1", port=6379, db=7, decode_responses=False)


# ============== 凭证 ==============
def _creds():
    import config
    return config.TENCENT_SECRET_ID, config.TENCENT_SECRET_KEY
set_credentials_provider(_creds)


# ============== 身份解析 ==============
# 这里假设 token 在 Redis 里反查 user_id；按你的 auth 系统改实现
def lookup_user_by_token(token: str) -> Optional[str]:
    raw = redis_user.get(f"token:{token}")
    return raw.decode("utf-8") if raw else None

def auth_strict(request: Request) -> str:
    auth = request.headers.get("authorization") or ""
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "Authorization required")
    user_id = lookup_user_by_token(auth[7:].strip())
    if not user_id:
        raise HTTPException(403, "Invalid token")
    return user_id

def auth_optional(request: Request) -> Optional[str]:
    try:
        return auth_strict(request)
    except HTTPException:
        return None


# ============== 业务策略：CHAT_FILES/<user>/<8hex>_<YYMMDDHHmm>_<filename> ==============
_SEG_BAD_CHARS = re.compile(r"[^a-zA-Z0-9_\-.]")
_TZ_PLUS_8 = timezone(timedelta(hours=8))

def sanitize(s: str, max_len: int = 64) -> str:
    """清洗 COS 路径段：替换非法字符为 _，截断长度。"""
    s = (s or "").strip()
    s = _SEG_BAD_CHARS.sub("_", s)
    return s[:max_len] if max_len else s

def chat_object_key_strategy(ctx: ObjectKeyContext) -> Tuple[str, str]:
    user = sanitize(ctx.user_id or "anon", 96)
    folder_hint = ctx.form_fields.get("folder", "")  # client 想传的子目录提示，可空

    # 文件名前缀：progress_id 末 8 位（如有）或 8 位随机 hex；用于人肉关联同一会话的上传
    pid_tail = (ctx.progress_id or "").replace("-", "")[-8:] or secrets.token_hex(4)

    # 时间戳：YYMMDDHHmm（精确到分钟够用，文件名不长）
    started = datetime.fromtimestamp(ctx.started_ms / 1000, tz=_TZ_PLUS_8)
    minute = started.strftime("%y%m%d%H%M")

    # 完整路径
    parts = ["CHAT_FILES", user]
    if folder_hint:
        parts.append(sanitize(folder_hint, 80))
    folder_prefix = "/".join(parts)
    object_name = f"{pid_tail}_{minute}_{ctx.safe_name}"
    return folder_prefix, object_name


# ============== 注册 ==============
register_upload_routes(
    app,
    redis_for_progress=redis_user,
    auth_resolver=auth_strict,
    auth_resolver_optional=auth_optional,
    object_key_strategy=chat_object_key_strategy,
    max_bytes=20 * 1024 * 1024 * 1024,  # 20 GiB
    max_concurrent=8,
    bucket="my-bucket-1234567890",
    route_prefix="/api/file_to_url",
    redis_state_prefix="myapp:f2u_prog:",        # 自选命名空间
    redis_owner_prefix="myapp:f2u_prog_owner:",
    extra_form_fields=["conversation_id"],
)
```

**前端调用**（含进度）：

```javascript
const progressId = crypto.randomUUID();
const fd = new FormData();
fd.append("folder", "");                    // 可空
fd.append("conversation_id", conv.id);      // 业务字段
fd.append("file", file);

const xhr = new XMLHttpRequest();
xhr.open("POST", `/api/file_to_url?progress_id=${progressId}&expected_bytes=${file.size}`);
xhr.setRequestHeader("Authorization", `Bearer ${token}`);
xhr.upload.onprogress = (e) => updateProgressBar(e.loaded / e.total);
xhr.onload = () => { /* xhr.responseText -> {"url": ...} */ };
xhr.send(fd);

// SSE 监听 server_receive + cos_upload 双通道（详见 PROTOCOL.md §2.6.1）
```

---

## Recipe 2：用户头像

**业务规则**：
- 仅登录用户能上传自己的头像
- 文件名固定为 `<user_id>.<ext>`（前端会先 resize / 转 PNG，再以 user_id 命名）
- 同一用户重复上传**覆盖**旧头像（COS put_object 默认覆盖）
- 不需要 progress_id（头像通常 < 1 MB）

**完整代码**：

```python
from typing import Optional, Tuple
from fastapi import FastAPI, HTTPException, Request
from redis import Redis
from flow_upload import set_credentials_provider
from flow_upload.http import register_upload_routes, ObjectKeyContext

app = FastAPI()
redis_user = Redis(host="127.0.0.1", port=6379, db=7)


def _creds():
    import config
    return config.TENCENT_SECRET_ID, config.TENCENT_SECRET_KEY
set_credentials_provider(_creds)


def auth_strict(request: Request) -> str:
    # 同 Recipe 1
    ...

def auth_optional(request: Request) -> Optional[str]:
    try: return auth_strict(request)
    except HTTPException: return None


# ============== 头像策略：AVATARS/<user_id>.<ext> ==============
def avatar_object_key_strategy(ctx: ObjectKeyContext) -> Tuple[str, str]:
    if not ctx.user_id:
        # 头像必须有用户身份；策略闭包里也再 double check
        raise HTTPException(401, "Avatar upload requires authentication")
    # 信任前端给的 safe_name 后缀（前端已 resize + 重命名为 <user_id>.png）
    return "AVATARS", ctx.safe_name


register_upload_routes(
    app,
    redis_for_progress=redis_user,
    auth_resolver=auth_strict,
    auth_resolver_optional=auth_optional,
    object_key_strategy=avatar_object_key_strategy,
    max_bytes=10 * 1024 * 1024,                        # 10 MB 头像够大了
    bucket="my-bucket-1234567890",
    route_prefix="/api/upload_avatar",                  # 业务专用路径
    redis_state_prefix="myapp:avatar_prog:",
    redis_owner_prefix="myapp:avatar_prog_owner:",
)
```

**前端调用**（无进度）：

```javascript
const fd = new FormData();
fd.append("file", new File([resizedPngBlob], `${userId}.png`, { type: "image/png" }));

const r = await fetch("/api/upload_avatar", {
  method: "POST",
  headers: { Authorization: `Bearer ${token}` },
  body: fd,
});
const { url } = await r.json();
```

---

## Recipe 3：桌面端发布包

**业务规则**：
- 仅 admin 用户能上传发布包（在 `auth_resolver` 里再加 admin 检查）
- 路径包含发布渠道 / 平台 / 版本号，便于客户端按平台拉取：
  `DESKTOP_UPDATES/<channel>/<platform>/<version>/<filename>`
- channel ∈ {`stable`, `beta`}；platform ∈ {`mac-x64`, `mac-arm64`, `win-x64`, `linux-x64`}
- 发布包通常 50–500 MB，建议 progress_id（管理 UI 上看进度）

**完整代码**：

```python
import re
from typing import Optional, Tuple
from fastapi import FastAPI, HTTPException, Request
from redis import Redis
from flow_upload import set_credentials_provider
from flow_upload.http import register_upload_routes, ObjectKeyContext

app = FastAPI()
redis_user = Redis(host="127.0.0.1", port=6379, db=7)
ADMIN_USER_IDS = {"alice", "bob"}


def _creds():
    import config
    return config.TENCENT_SECRET_ID, config.TENCENT_SECRET_KEY
set_credentials_provider(_creds)


# auth：先做 token → user_id，再确认是 admin
def auth_admin_strict(request: Request) -> str:
    user_id = my_token_to_user_id(request)             # 你的现有实现
    if not user_id:
        raise HTTPException(401, "Authorization required")
    if user_id not in ADMIN_USER_IDS:
        raise HTTPException(403, "Admin only")
    return user_id

def auth_admin_optional(request: Request) -> Optional[str]:
    try: return auth_admin_strict(request)
    except HTTPException: return None


# ============== 策略：严格白名单约束 channel / platform / version 形状 ==============
_VALID_CHANNELS = {"stable", "beta"}
_VALID_PLATFORMS = {"mac-x64", "mac-arm64", "win-x64", "linux-x64"}
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(-[a-zA-Z0-9.]+)?$")  # SemVer-ish

def desktop_release_strategy(ctx: ObjectKeyContext) -> Tuple[str, str]:
    channel = ctx.form_fields.get("channel", "")
    platform = ctx.form_fields.get("platform", "")
    version = ctx.form_fields.get("version", "")

    if channel not in _VALID_CHANNELS:
        raise HTTPException(400, f"channel must be one of {sorted(_VALID_CHANNELS)}")
    if platform not in _VALID_PLATFORMS:
        raise HTTPException(400, f"platform must be one of {sorted(_VALID_PLATFORMS)}")
    if not _VERSION_RE.match(version):
        raise HTTPException(400, "version must be SemVer-like (1.2.3 or 1.2.3-beta.1)")

    folder_prefix = f"DESKTOP_UPDATES/{channel}/{platform}/{version}"
    return folder_prefix, ctx.safe_name


register_upload_routes(
    app,
    redis_for_progress=redis_user,
    auth_resolver=auth_admin_strict,
    auth_resolver_optional=auth_admin_optional,
    object_key_strategy=desktop_release_strategy,
    max_bytes=2 * 1024 * 1024 * 1024,                  # 2 GB
    bucket="my-bucket-1234567890",
    route_prefix="/api/release/desktop",
    redis_state_prefix="myapp:release_prog:",
    redis_owner_prefix="myapp:release_prog_owner:",
    extra_form_fields=["channel", "platform", "version"],
)
```

**注意**：策略闭包里 raise `HTTPException` 是支持的，会被 FastAPI 正常处理为 4xx 响应。

---

## Recipe 4：PDF / Paper 导入

**业务规则**：
- 用户提交一个 arxiv URL，后台下载 PDF 并上传到 COS
- 这是**服务端到服务端**场景，没有浏览器 multipart；不用 `register_upload_routes`，
  直接调顶层 `bytes_to_cos_url`

**完整代码**：

```python
import asyncio
import httpx
from flow_upload import bytes_to_cos_url, set_credentials_provider

def _creds():
    import config
    return config.TENCENT_SECRET_ID, config.TENCENT_SECRET_KEY
set_credentials_provider(_creds)


async def import_arxiv_paper(arxiv_id: str) -> str:
    """从 arxiv 下载 PDF，重传到 COS，返回我们桶内的公网直链。"""
    pdf_url = f"https://arxiv.org/pdf/{arxiv_id}.pdf"
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.get(pdf_url, follow_redirects=True)
        r.raise_for_status()
    pdf_bytes = r.content

    # bytes_to_cos_url 同步阻塞，包 to_thread 避免阻塞事件循环
    url = await asyncio.to_thread(
        bytes_to_cos_url,
        pdf_bytes,
        folder_name="PAPERS",
        object_name=f"arxiv_{arxiv_id}.pdf",
        bucket="my-bucket-1234567890",
        content_type="application/pdf",
    )
    return url
```

**何时用 `multipart_upload_from_chunk_queue` 而不是 `bytes_to_cos_url`**：
- 服务端到服务端的转发，且文件很大（>500 MB）
- 想在「下载源」与「上传 COS」之间并行（边下载边上传，不要等下载完）

最小骨架：

```python
import queue
import threading
import httpx
from flow_upload import multipart_upload_from_chunk_queue

def stream_to_cos(source_url: str, dst_object: str) -> str:
    q: queue.Queue = queue.Queue(maxsize=256)
    out = {"url": None, "err": None}

    def consumer():
        try:
            out["url"] = multipart_upload_from_chunk_queue(
                q,
                folder_name="PAPERS",
                object_name=dst_object,
                bucket="my-bucket-1234567890",
                content_type="application/pdf",
            )
        except Exception as e:
            out["err"] = e

    t = threading.Thread(target=consumer, daemon=True)
    t.start()

    try:
        with httpx.stream("GET", source_url, timeout=300.0) as r:
            r.raise_for_status()
            for chunk in r.iter_bytes(chunk_size=1024 * 256):
                q.put(("data", chunk))
        q.put(("end", None))
    except Exception as e:
        q.put(("err", str(e)))
        raise

    t.join(timeout=86400)
    if out["err"]:
        raise out["err"]
    return out["url"]
```

---

## Recipe 共性总结

所有用 `register_upload_routes(...)` 的接入点都涉及这 4 块：

1. **凭证**：`set_credentials_provider` 注入一次（应用启动期）
2. **身份**：`auth_resolver` 严格态 + `auth_resolver_optional` 软态；按你 auth 系统的逻辑实现，**不**继承 flow_upload 任何东西
3. **策略**：`ObjectKeyStrategy` 纯函数，把 `ObjectKeyContext` 翻译成 `(folder_prefix, object_name)`；可以在闭包里 raise `HTTPException` 拒绝非法形状
4. **注册**：`register_upload_routes(app, ...)` 一次完成，独立的 `route_prefix` 和 `redis_*_prefix` 命名空间，互不干扰

如果你的同一个 FastAPI 应用要同时支持多种业务，**多次调用 `register_upload_routes`** 即可：每次给不同的 `route_prefix` + 不同的 `redis_state_prefix` + 不同的 strategy。
