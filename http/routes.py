"""注册 FastAPI 上传路由：POST + SSE GET。

外部接入点是 register_upload_routes(app, ...)；身份系统、Redis、业务策略全部
通过参数注入，不直接 import 任何特定 user system / config。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable, List, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from .progress import ProgressStore
from .strategy import ObjectKeyStrategy
from .stream import stream_upload_to_cos

logger = logging.getLogger(__name__)


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
) -> ProgressStore:
    """在 FastAPI 应用上注册流式上传路由 + SSE 进度路由。

    参数：
      redis_for_progress:        Redis 客户端实例（仅用于进度状态）；也可传 0 参 callable
                                  返回实例，用于宿主在 startup 后才赋值的场景，避免
                                  在模块加载时拿到 None。
      auth_resolver:             (Request) -> user_id；解析失败应抛 HTTPException(401/403)
      auth_resolver_optional:    (Request) -> user_id | None；无 token / 失败返回 None
                                  仅在「无 progress_id」的兼容上传分支用到
      object_key_strategy:       (ObjectKeyContext) -> (folder_prefix, object_name)
                                  业务策略（如 chat: CHAT_FILES/<user>/...）
      max_bytes:                 单文件硬上限
      bucket:                    COS bucket；None 则使用 cos_upload.DEFAULT_BUCKET
      max_concurrent:            同时进入 COS 线程池的最大上传数
      route_prefix:              POST 路由路径；GET 进度为 {prefix}/progress
      redis_state_prefix /
      redis_owner_prefix /
      redis_state_ttl_sec:       Redis 命名空间与 TTL（迁移老数据时可保持原前缀以兼容）
      extra_form_fields:         除 "folder" 之外要解析的多余 form 字段名（值进入 ObjectKeyContext.form_fields）
      sse_keepalive_interval_sec / sse_max_ticks:
                                 SSE 流推送节流；保留默认即可

    返回：
      ProgressStore 实例，调用方可在自定义异步任务中复用同一进度命名空间。
    """
    if max_bytes <= 0:
        raise ValueError("max_bytes must be > 0")

    store = ProgressStore(
        redis_client=redis_for_progress,
        state_prefix=redis_state_prefix,
        owner_prefix=redis_owner_prefix,
        ttl_sec=redis_state_ttl_sec,
    )
    upload_semaphore = asyncio.Semaphore(max(1, int(max_concurrent)))
    progress_route = route_prefix.rstrip("/") + "/progress"

    @app.get(progress_route)
    async def upload_progress_sse(
        request: Request,
        progress_id: str = Query(...),
    ):
        """SSE：从 Redis 中按周期读取进度快照下发。

        SSE 往往早于 multipart POST 到达；若尚未有 owner 登记，本路由惰性
        登记当前用户为 owner，避免长时间 404。owner 不一致返回 403。
        """
        try:
            pid = ProgressStore.normalize_pid(progress_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid progress_id")
        if not pid:
            raise HTTPException(status_code=400, detail="progress_id required")

        user_id = auth_resolver(request)
        owner = store.get_owner(pid)
        if not owner:
            try:
                store.begin(pid, user_id, expected_total=None)
            except PermissionError:
                raise HTTPException(status_code=403, detail="progress_id not owned by current user")
            owner = store.get_owner(pid)
        if owner != str(user_id):
            raise HTTPException(status_code=403, detail="progress_id not owned by current user")

        async def _gen():
            # 不在「整段 JSON 字符串」层去重：否则 Nginx/部分代理会攒够缓冲才下发，
            # 客户端长时间读不到帧，表现为 cos 条不更新、仅结束时一次合并。
            # 每帧带单调 _sse_tick，保证周期性 flush。
            for tick in range(int(sse_max_ticks)):
                snap = store.get_state(pid)
                if snap is None:
                    payload = {
                        "server_receive": {"bytes_done": 0, "bytes_total": None, "done": False},
                        "cos_upload": {"bytes_done": 0, "bytes_total": None, "done": False},
                        "done": False,
                        "error": None,
                        "_sse_tick": tick,
                    }
                else:
                    payload = {**snap, "_sse_tick": tick}
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                if isinstance(snap, dict) and snap.get("done"):
                    break
                await asyncio.sleep(float(sse_keepalive_interval_sec))

        return StreamingResponse(
            _gen(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-store, no-transform",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.post(route_prefix)
    async def upload_endpoint(
        request: Request,
        progress_id: Optional[str] = Query(None),
        expected_bytes: Optional[int] = Query(
            None,
            ge=1,
            le=max_bytes,
            description="可选：上传正文总字节数（与 multipart 文件域一致），用于双通道进度条显示确定比例",
        ),
    ):
        """multipart 上传到 COS。

        - 有 progress_id：流式 + Redis 双通道；GET {prefix}/progress 实时订阅
        - 无 progress_id：整文件读入再上传（与旧行为兼容；浏览器仅有 XHR upload 进度）
        - form 域：folder（可选）+ extra_form_fields 中各字段（可选）+ file（必填）
        - 文件 part 必须排在最后；前置字段先到，便于策略基于它们决策 COS key
        """
        try:
            pid = ProgressStore.normalize_pid(progress_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid progress_id")

        if pid:
            user_id = auth_resolver(request)
            try:
                store.begin(pid, user_id, expected_total=expected_bytes)
            except PermissionError:
                raise HTTPException(status_code=403, detail="progress_id not owned by current user")
        else:
            user_id = (
                auth_resolver_optional(request) if auth_resolver_optional is not None else None
            )

        async with upload_semaphore:
            try:
                if pid:
                    on_sr_bytes = lambda n: store.server_receive_bytes(pid, n)  # noqa: E731
                    on_sr_done = lambda n: store.server_receive_done(pid, n)  # noqa: E731
                    on_cos = lambda n: store.cos_uploaded_bytes(pid, n)  # noqa: E731
                else:
                    on_sr_bytes = on_sr_done = on_cos = None

                result_url, rx_n = await stream_upload_to_cos(
                    request,
                    progress_id=pid,
                    object_key_strategy=object_key_strategy,
                    bucket=bucket,
                    max_bytes=max_bytes,
                    extra_form_fields=extra_form_fields,
                    user_id=user_id,
                    on_server_receive_bytes=on_sr_bytes,
                    on_server_receive_done=on_sr_done,
                    on_cos_uploaded_bytes=on_cos,
                )
                if pid:
                    store.finish_ok(pid, rx_n)
            except ValueError as ve:
                msg = str(ve)
                if pid:
                    store.finish_err(pid, msg)
                if msg == "too_large":
                    raise HTTPException(
                        status_code=413,
                        detail=f"File too large (max {max_bytes} bytes)",
                    ) from ve
                if msg.endswith("_too_large"):
                    field = msg[: -len("_too_large")]
                    raise HTTPException(
                        status_code=400,
                        detail=f"{field} field too large",
                    ) from ve
                raise HTTPException(status_code=400, detail=msg) from ve
            except Exception as e:
                if pid:
                    if isinstance(e, HTTPException):
                        det = e.detail
                        err_s = (
                            det
                            if isinstance(det, str)
                            else (json.dumps(det, ensure_ascii=False) if det is not None else str(e))
                        )
                    else:
                        err_s = str(e)
                    store.finish_err(pid, str(err_s)[:800])
                raise
        return {"url": result_url}

    return store
