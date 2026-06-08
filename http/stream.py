"""流式 multipart 解析 → COS chunk 队列。

不经过 FastAPI 的 UploadFile：Starlette 的 UploadFile 会先整段解析 multipart 落盘
再进路由，无法做到「入网与 COS 边收边传」。本函数直接读 `request.stream()`
拿到的 net chunk → MultipartParser → 入文件 part 的内存 / 入 COS multipart 队列。

设计取向：
- 不知道 chat / avatar / desktop release 等任何业务概念
- 不知道 user_system / token / Redis；进度回报通过 callable 钩子传入
- 不知道 conversation_id / doc_id 等 form 字段；通过 extra_form_fields 声明
"""

from __future__ import annotations

import asyncio
import logging
import queue
import threading
import time
import urllib.parse
from collections import deque
from typing import Any, Callable, Dict, List, Optional, Tuple

from fastapi import HTTPException, Request

from ..cos_upload import bytes_to_cos_url, multipart_upload_from_chunk_queue
from .strategy import ObjectKeyContext, ObjectKeyStrategy

logger = logging.getLogger(__name__)

_FOLDER_FIELD_LIMIT = 65536
_OTHER_FIELD_LIMIT = 2048
_NET_SLICE = 256 * 1024


def _decode_multipart_filename(cdopts: Dict[bytes, bytes]) -> str:
    """从 multipart Content-Disposition 选项里取文件名并正确解码。

    现代浏览器把 filename 的非 ASCII 字符按 UTF-8 字节放进 multipart body；旧代码按
    latin-1 解码会让中文名乱码并污染 COS key。这里 RFC 5987 的 filename* 优先，否则
    普通 filename 先按 UTF-8 解，失败再回退 latin-1。
    """
    star = cdopts.get(b"filename*")
    if star:
        s = star.decode("latin-1", "replace")
        if "''" in s:
            charset, _, enc = s.split("'", 2)
            try:
                return urllib.parse.unquote(enc, encoding=(charset or "utf-8"), errors="replace")
            except (LookupError, ValueError):
                return urllib.parse.unquote(enc, encoding="utf-8", errors="replace")
    raw = cdopts.get(b"filename", b"file.bin")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1", "replace")


async def stream_upload_to_cos(
    request: Request,
    *,
    progress_id: Optional[str],
    object_key_strategy: ObjectKeyStrategy,
    bucket: Optional[str] = None,
    max_bytes: int,
    extra_form_fields: Optional[List[str]] = None,
    user_id: Optional[str] = None,
    started_ms: Optional[int] = None,
    on_server_receive_bytes: Optional[Callable[[int], None]] = None,
    on_server_receive_done: Optional[Callable[[int], None]] = None,
    on_cos_uploaded_bytes: Optional[Callable[[int], None]] = None,
) -> Tuple[str, int]:
    """解析 multipart 并把文件 part 流式上传到 COS。

    返回：(public_url, rx_bytes)

    异常：
      - ValueError("too_large")：文件域超过 max_bytes
      - ValueError("<field>_too_large")：folder / extra_form_fields 中某字段超长
      - HTTPException(400)：multipart 协议错误 / 缺 file 域
    """
    try:
        from python_multipart.multipart import MultipartParser, parse_options_header
    except ImportError:  # pragma: no cover
        from multipart.multipart import MultipartParser, parse_options_header  # type: ignore[no-redef]

    declared_fields = set(extra_form_fields or [])
    declared_fields.add("folder")  # folder 是约定字段，始终解析
    started_ms = int(started_ms) if started_ms else int(time.time() * 1000)

    ctype_hdr = request.headers.get("content-type") or ""
    main, opts = parse_options_header(ctype_hdr)
    mty = main.split(b";")[0].strip().lower() if main else b""
    if mty != b"multipart/form-data":
        raise HTTPException(status_code=400, detail="Expected multipart/form-data")
    boundary = opts.get(b"boundary")
    if not boundary:
        raise HTTPException(status_code=400, detail="Missing multipart boundary")

    ctx: Dict[str, Any] = {
        "started_ms": started_ms,
        "form_buf": {name: bytearray() for name in declared_fields},
        "form_str": {name: "" for name in declared_fields},
        "safe_name": "file.bin",
        "ctype": None,
        "current_is_file": False,
        "current_field": "",
        "part_headers": [],
        "hfield": b"",
        "hvalue": b"",
        "file_seen": False,
    }

    has_pid = bool(progress_id)
    file_buf: Optional[bytearray] = bytearray() if not has_pid else None
    q: Optional[queue.Queue] = queue.Queue(maxsize=256) if has_pid else None
    pending: Optional[deque] = deque() if has_pid else None
    rx = {"n": 0}
    cos_holder: Dict[str, Any] = {"t": None, "errs": [], "out": {}}

    def _build_ctx_view() -> ObjectKeyContext:
        return ObjectKeyContext(
            user_id=user_id,
            progress_id=progress_id,
            safe_name=str(ctx.get("safe_name") or "file.bin"),
            started_ms=int(ctx.get("started_ms") or 0),
            form_fields=dict(ctx["form_str"]),
        )

    def ensure_cos_thread() -> None:
        if not has_pid or q is None or cos_holder["t"] is not None:
            return
        folder_prefix, object_name = object_key_strategy(_build_ctx_view())

        def _run() -> None:
            try:
                cos_kwargs: Dict[str, Any] = {
                    "folder_name": folder_prefix,
                    "object_name": object_name,
                    "content_type": (
                        str(ctx["ctype"]).strip() if ctx["ctype"] else None
                    )
                    or None,
                }
                if bucket:
                    cos_kwargs["bucket"] = bucket
                if on_cos_uploaded_bytes is not None:
                    cos_kwargs["on_cos_bytes"] = on_cos_uploaded_bytes
                cos_holder["out"]["url"] = multipart_upload_from_chunk_queue(q, **cos_kwargs)
            except Exception as e:  # pragma: no cover - re-raised on join
                cos_holder["errs"].append(e)

        t = threading.Thread(target=_run, daemon=True, name="flow-upload-cos-consumer")
        t.start()
        cos_holder["t"] = t

    # ---------------- multipart parser callbacks ----------------

    def on_part_begin() -> None:
        ctx["part_headers"] = []
        ctx["hfield"] = b""
        ctx["hvalue"] = b""
        ctx["current_is_file"] = False
        ctx["current_field"] = ""

    def on_header_field(data: bytes, start: int, end: int) -> None:
        ctx["hfield"] += data[start:end]

    def on_header_value(data: bytes, start: int, end: int) -> None:
        ctx["hvalue"] += data[start:end]

    def on_header_end() -> None:
        ctx["part_headers"].append((ctx["hfield"].lower().strip(), ctx["hvalue"]))
        ctx["hfield"] = b""
        ctx["hvalue"] = b""

    def on_headers_finished() -> None:
        disp_b = b""
        pct: Optional[str] = None
        for k, v in ctx["part_headers"]:
            if k == b"content-disposition":
                disp_b = v
            elif k == b"content-type":
                pct = v.decode("latin-1", errors="replace").strip() or None
        if not disp_b:
            return
        _, cdopts = parse_options_header(disp_b.decode("latin-1", errors="replace"))
        field_name = cdopts.get(b"name", b"").decode("latin-1", errors="replace")
        if b"filename" in cdopts:
            ctx["current_is_file"] = True
            ctx["file_seen"] = True
            raw_fn = _decode_multipart_filename(cdopts).strip()
            ctx["safe_name"] = (
                raw_fn.rsplit("\\", 1)[-1].rsplit("/", 1)[-1] or "file.bin"
            )[:512]
            ctx["ctype"] = pct or "application/octet-stream"
        else:
            ctx["current_is_file"] = False
            ctx["current_field"] = field_name

    def on_part_data(data: bytes, start: int, end: int) -> None:
        chunk = bytes(data[start:end])
        if not chunk:
            return
        if ctx["current_is_file"]:
            if has_pid:
                ensure_cos_thread()
                assert pending is not None
                pending.append(chunk)
            else:
                assert file_buf is not None
                if len(file_buf) + len(chunk) > max_bytes:
                    raise ValueError("too_large")
                file_buf.extend(chunk)
            return
        field = ctx["current_field"]
        buf = ctx["form_buf"].get(field)
        if buf is None:
            return  # 未声明的 form 字段静默忽略
        limit = _FOLDER_FIELD_LIMIT if field == "folder" else _OTHER_FIELD_LIMIT
        if len(buf) + len(chunk) > limit:
            raise ValueError(f"{field}_too_large")
        buf.extend(chunk)

    def on_part_end() -> None:
        if ctx["current_is_file"]:
            return
        field = ctx["current_field"]
        buf = ctx["form_buf"].get(field)
        if buf is not None:
            ctx["form_str"][field] = buf.decode("utf-8", errors="replace").strip()
            buf.clear()

    callbacks = {
        "on_part_begin": on_part_begin,
        "on_part_data": on_part_data,
        "on_part_end": on_part_end,
        "on_header_field": on_header_field,
        "on_header_value": on_header_value,
        "on_header_end": on_header_end,
        "on_headers_finished": on_headers_finished,
        "on_end": lambda: None,
    }
    parser = MultipartParser(boundary, callbacks, max_size=float("inf"))

    loop = asyncio.get_event_loop()

    async def _drain_pending() -> None:
        if not has_pid or pending is None or q is None:
            return
        while pending:
            b = pending.popleft()
            lb = len(b)
            if rx["n"] + lb > max_bytes:
                await loop.run_in_executor(None, q.put, ("err", "too_large"))
                raise ValueError("too_large")
            rx["n"] += lb
            if on_server_receive_bytes is not None:
                on_server_receive_bytes(rx["n"])
            await loop.run_in_executor(None, lambda bb=b: q.put(("data", bb)))

    # 若上游一次把整段 body 交给 ASGI（常见：Nginx proxy_request_buffering），单次 parser.write
    # 会在同步路径里跑完所有 on_part_data，pending 堆满后才 await drain，COS 线程长时间 q.get
    # 不到数据。把入网块再切片，每片 write 后 drain，保证边解析边入队、Redis/SSE 与 COS 并行推进。
    try:
        async for net_chunk in request.stream():
            if net_chunk:
                if len(net_chunk) <= _NET_SLICE:
                    parser.write(net_chunk)
                    if has_pid:
                        await _drain_pending()
                else:
                    for i in range(0, len(net_chunk), _NET_SLICE):
                        parser.write(net_chunk[i : i + _NET_SLICE])
                        if has_pid:
                            await _drain_pending()
            elif has_pid:
                await _drain_pending()
        parser.finalize()
        if has_pid:
            await _drain_pending()
    except ValueError:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"multipart parse error: {e}") from e

    if not ctx["file_seen"]:
        raise HTTPException(status_code=400, detail="Missing file field in multipart body")

    if not has_pid:
        assert file_buf is not None
        body = bytes(file_buf)
        folder_prefix, object_name = object_key_strategy(_build_ctx_view())
        cos_kwargs: Dict[str, Any] = {
            "folder_name": folder_prefix,
            "object_name": object_name,
            "content_type": (str(ctx["ctype"]).strip() if ctx["ctype"] else None) or None,
        }
        if bucket:
            cos_kwargs["bucket"] = bucket
        url = await asyncio.to_thread(bytes_to_cos_url, body, **cos_kwargs)
        return url, len(body)

    assert q is not None
    await loop.run_in_executor(None, q.put, ("end", None))
    if on_server_receive_done is not None:
        on_server_receive_done(rx["n"])
    t = cos_holder["t"]
    if t is None:
        raise RuntimeError("internal: COS consumer not started for file part")
    await asyncio.to_thread(t.join, 86400.0)
    if cos_holder["errs"]:
        raise cos_holder["errs"][0]
    url = str(cos_holder["out"].get("url") or "").strip()
    if not url:
        raise RuntimeError("flow_upload: COS consumer returned empty url")
    return url, int(rx["n"])
