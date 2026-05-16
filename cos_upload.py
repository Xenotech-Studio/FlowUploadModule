"""
腾讯云 COS 上传：纯传输层 primitives。

三个函数互不知道彼此的业务用途，只接 (folder_name, object_name)：
- file_to_url：UploadFile 一次性 put_object（沿用 cos_filename / download_filename）
- bytes_to_cos_url：内存 bytes 一次性 put_object
- multipart_upload_from_chunk_queue：从 (kind, payload) chunk 队列边收边 multipart 上传

CHAT_FILES / AVATARS / DESKTOP_UPDATES 等「上传到 COS 内哪个目录」的策略由调用方决定，
本模块只负责拼 key、构造客户端、调 SDK，方便上层换桶/换策略不动传输代码。

凭证查找顺序（高 → 低）：
1. set_credentials_provider() 注入的 callable
2. 宿主项目 `config.py` 的 TENCENT_SECRET_ID / TENCENT_SECRET_KEY
3. 环境变量 TENCENT_SECRET_ID / TENCENT_SECRET_KEY

都拿不到时抛 RuntimeError；不再带任何硬编码默认密钥（与 user_system.cos_upload 的
唯一行为差异，仅在「config 未配 + 环境变量未设」这一种 misconfigured 情形下生效）。
"""

from __future__ import annotations

import logging
import os
import sys
from io import BytesIO
from queue import Queue
from typing import Any, Callable, List, Optional, Tuple

from qcloud_cos import CosConfig, CosS3Client


# 默认桶 / 区域 / 协议（三个 primitive 共用，避免散在各处再写一遍）
# 注：bucket 名称不算敏感，与 Flops user_system.cos_upload 历史默认一致；
# 若调用方不显式传 bucket 即落到此默认。
DEFAULT_BUCKET = "flowtask-1302933783"
DEFAULT_REGION = "ap-guangzhou"
DEFAULT_SCHEME = "https"


CredentialsProvider = Callable[[], Tuple[str, str]]


_credentials_provider: Optional[CredentialsProvider] = None


def set_credentials_provider(fn: Optional[CredentialsProvider]) -> None:
    """注入凭证 provider，返回 (secret_id, secret_key)。

    传 None 可清除注入，回退到 config / 环境变量查找路径。
    """
    global _credentials_provider
    _credentials_provider = fn


def _read_credentials_from_host_config() -> Tuple[str, str]:
    """从宿主项目 config.py 读取 TENCENT_SECRET_*；不存在或未配置返回 ("", "")。"""
    try:
        import config as _config  # type: ignore
    except ImportError:
        return "", ""
    sid = getattr(_config, "TENCENT_SECRET_ID", None) or ""
    sk = getattr(_config, "TENCENT_SECRET_KEY", None) or ""
    return str(sid).strip(), str(sk).strip()


def _read_credentials_from_env() -> Tuple[str, str]:
    return (os.environ.get("TENCENT_SECRET_ID") or "").strip(), (
        os.environ.get("TENCENT_SECRET_KEY") or ""
    ).strip()


def get_tencent_credentials() -> Tuple[str, str]:
    """返回 (secret_id, secret_key)，供 COS、联网搜索等腾讯云 API 共用。

    查找顺序：注入 provider → 宿主 config.py → 环境变量。都拿不到抛 RuntimeError。
    """
    if _credentials_provider is not None:
        try:
            sid, sk = _credentials_provider()
        except Exception as e:  # pragma: no cover - defensive
            raise RuntimeError(f"FlowUpload credentials provider raised: {e}") from e
        sid = (sid or "").strip()
        sk = (sk or "").strip()
        if sid and sk:
            return sid, sk

    sid, sk = _read_credentials_from_host_config()
    if sid and sk:
        return sid, sk

    sid, sk = _read_credentials_from_env()
    if sid and sk:
        return sid, sk

    raise RuntimeError(
        "FlowUpload: 缺少腾讯云凭证。请通过 set_credentials_provider(...) 注入，"
        "或在宿主项目 config.py 中配置 TENCENT_SECRET_ID / TENCENT_SECRET_KEY，"
        "或设置同名环境变量。"
    )


def _make_cos_client(
    *,
    bucket: Optional[str] = None,
    region: Optional[str] = None,
    scheme: Optional[str] = None,
) -> Tuple[CosConfig, CosS3Client, str]:
    """
    构造一对 (CosConfig, CosS3Client, resolved_bucket)。

    显式 Proxies={}：与 arXiv / 搜索等共用同一进程时，环境变量 HTTPS_PROXY 可能让 COS 误走代理，强制清空。
    """
    sid, sk = get_tencent_credentials()
    cfg = CosConfig(
        Region=region or DEFAULT_REGION,
        SecretId=sid,
        SecretKey=sk,
        Scheme=scheme or DEFAULT_SCHEME,
        Proxies={},
    )
    return cfg, CosS3Client(cfg), (bucket or DEFAULT_BUCKET)


def _resolve_cos_key(folder_name: str, object_name: str) -> Tuple[str, str]:
    """
    根据 folder_name + object_name 返回 (key, safe_basename)。

    - folder_name 末尾自动补 '/'（空串保留为空）。
    - object_name 仅取末段 basename，防止上层不慎传 'a/b/c' 把目录切坏。
    """
    folder = (folder_name + "/") if (folder_name and not folder_name.endswith("/")) else (folder_name or "")
    safe = (object_name or "file.bin").rsplit("/", 1)[-1].rsplit("\\", 1)[-1] or "file.bin"
    return folder + safe, safe


def _content_disposition_attachment(download_filename: Optional[str]) -> Optional[str]:
    """download_filename 非空时返回 'attachment; filename=...'，让浏览器下载用指定名字。"""
    if not download_filename:
        return None
    dl = download_filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    return f'attachment; filename="{dl}"'


class _CosUploadCountingReader:
    """包装 BytesIO：COS SDK 每次 read 后回调 (bytes_consumed, total)，用于上报上传进度。"""

    def __init__(self, data: bytes, on_read: Callable[[int, int], None]) -> None:
        self._io = BytesIO(data)
        self._on_read = on_read
        self._total = len(data)

    def read(self, amt: int = -1) -> bytes:
        b = self._io.read(amt)
        if b:
            self._on_read(self._io.tell(), self._total)
        return b

    def seek(self, pos: int, whence: int = 0) -> int:
        return self._io.seek(pos, whence)


def file_to_url(
    file,
    folder_name: str = "",
    bucket: str = DEFAULT_BUCKET,
    cos_filename: Optional[str] = None,
    download_filename: Optional[str] = None,
):
    """
    上传 UploadFile 到腾讯云 COS（位置参数兼容旧调用方）。

    参数：
      file: UploadFile（含 filename 与 .file 流）
      folder_name: COS key 前缀目录（不含末段文件名），由调用方决定策略
      bucket: COS bucket
      cos_filename: COS key 末段；不传则取 file.filename 的 basename
      download_filename: 设置 Content-Disposition，让浏览器下载时用指定名字

    返回：上传后的公网 HTTPS URL
    """
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)

    cfg, client, bucket_resolved = _make_cos_client(bucket=bucket)
    object_name = cos_filename if cos_filename else (getattr(file, "filename", None) or "file.bin")
    key, _ = _resolve_cos_key(folder_name, object_name)
    image_url = cfg.uri(bucket=bucket_resolved, path=key)

    upload_params: dict = {
        "Bucket": bucket_resolved,
        "Body": file.file,
        "Key": key,
        "StorageClass": "STANDARD",
        "EnableMD5": False,
    }
    cd = _content_disposition_attachment(download_filename)
    if cd:
        upload_params["ContentDisposition"] = cd

    client.put_object(**upload_params)
    logging.info("Image uploaded to COS: %s", image_url)
    return image_url


def bytes_to_cos_url(
    body: bytes,
    *,
    folder_name: str = "",
    object_name: str = "file.bin",
    bucket: str = DEFAULT_BUCKET,
    content_type: Optional[str] = None,
    on_body_read_progress: Optional[Callable[[int, int], None]] = None,
) -> str:
    """
    把内存中的字节上传到 COS，返回公网 HTTPS URL（与 file_to_url 同桶/同地域/同鉴权）。

    on_body_read_progress：可选；COS SDK 从 Body 读取时回调 (已读字节, 总字节)。
    """
    cfg, client, bucket_resolved = _make_cos_client(bucket=bucket)
    key, _ = _resolve_cos_key(folder_name, object_name)
    image_url = cfg.uri(bucket=bucket_resolved, path=key)

    body_stream: object = (
        _CosUploadCountingReader(body, on_body_read_progress)
        if on_body_read_progress is not None
        else BytesIO(body)
    )
    upload_params: dict = {
        "Bucket": bucket_resolved,
        "Body": body_stream,
        "Key": key,
        "StorageClass": "STANDARD",
        "EnableMD5": False,
    }
    if content_type and str(content_type).strip():
        upload_params["ContentType"] = str(content_type).strip()

    client.put_object(**upload_params)
    logging.info("Bytes uploaded to COS: %s", image_url)
    return image_url


def copy_object_to_cos_url(
    source_key: str,
    *,
    folder_name: str = "",
    object_name: str = "file.bin",
    bucket: str = DEFAULT_BUCKET,
    source_bucket: Optional[str] = None,
    source_region: Optional[str] = None,
) -> dict:
    """
    在 COS 内部把 source_key 复制成 folder_name/object_name 新 key（同 bucket 默认）。
    走 PutObjectCopy（服务端 copy），不下载也不上传字节。
    返回：{"url": 新对象公网 URL, "size_bytes": int|None, "mime_type": str|None}
    """
    cfg, client, dest_bucket = _make_cos_client(bucket=bucket)
    key, _ = _resolve_cos_key(folder_name, object_name)
    dest_url = cfg.uri(bucket=dest_bucket, path=key)

    src_bucket = source_bucket or dest_bucket
    src_region = source_region or DEFAULT_REGION
    copy_source = {"Bucket": src_bucket, "Key": source_key.lstrip("/"), "Region": src_region}
    client.copy_object(Bucket=dest_bucket, Key=key, CopySource=copy_source, CopyStatus="Copy")

    size_bytes: Optional[int] = None
    mime_type: Optional[str] = None
    try:
        head = client.head_object(Bucket=dest_bucket, Key=key)
        cl = head.get("Content-Length") if isinstance(head, dict) else None
        if cl is not None:
            try:
                size_bytes = int(cl)
            except (TypeError, ValueError):
                size_bytes = None
        ct = head.get("Content-Type") if isinstance(head, dict) else None
        if isinstance(ct, str) and ct.strip():
            mime_type = ct.strip()
    except Exception:
        pass

    logging.info("COS copy: %s -> %s", source_key, dest_url)
    return {"url": dest_url, "size_bytes": size_bytes, "mime_type": mime_type}


def multipart_upload_from_chunk_queue(
    q: "Queue[Tuple[str, Any]]",
    *,
    folder_name: str = "",
    object_name: str = "file.bin",
    bucket: str = DEFAULT_BUCKET,
    content_type: Optional[str] = None,
    on_cos_bytes: Optional[Callable[[int], None]] = None,
    part_size: int = 5 * 1024 * 1024,
) -> str:
    """
    从队列拉取 ("data", chunk) / ("end", None) / ("err", msg)，边收边 multipart 上传到 COS。

    单 part 最小 part_size（末 part 可更小）；空文件走 put_object。
    与 bytes_to_cos_url 共享 _make_cos_client / _resolve_cos_key，确保 bucket/region/scheme/key 拼法一致。
    """
    cfg, client, bucket_resolved = _make_cos_client(bucket=bucket)
    key, _ = _resolve_cos_key(folder_name, object_name)
    image_url = cfg.uri(bucket=bucket_resolved, path=key)

    buffer = bytearray()
    uploaded = 0
    upload_id: Optional[str] = None
    parts: List[dict] = []
    part_number = 1

    def _report_cos() -> None:
        """已确认上传的字节 + 仍待 upload_part 的缓冲，单调递增；避免 < part_size 时长期不报导致进度条卡在 0。"""
        if on_cos_bytes is not None:
            on_cos_bytes(int(uploaded) + len(buffer))

    def _flush(force: bool = False) -> None:
        nonlocal buffer, uploaded, part_number, parts
        while len(buffer) >= part_size or (force and buffer):
            take = len(buffer) if force else part_size
            if take <= 0:
                break
            body = bytes(buffer[:take])
            del buffer[:take]
            if not upload_id:
                raise RuntimeError("multipart_upload_from_chunk_queue: internal state error (no upload_id)")
            up = client.upload_part(
                Bucket=bucket_resolved,
                Key=key,
                PartNumber=part_number,
                UploadId=upload_id,
                Body=body,
            )
            etag_raw = up.get("ETag") or ""
            etag = str(etag_raw).strip().strip('"')
            parts.append({"PartNumber": part_number, "ETag": etag})
            part_number += 1
            uploaded += len(body)
            _report_cos()

    try:
        while True:
            kind, payload = q.get()
            if kind == "err":
                raise ValueError(str(payload) if payload is not None else "upload aborted")
            if kind == "end":
                break
            if kind != "data":
                continue
            chunk = payload if isinstance(payload, (bytes, bytearray)) else bytes(payload)
            if not chunk:
                continue
            if upload_id is None:
                extra: dict = {"Bucket": bucket_resolved, "Key": key}
                if content_type and str(content_type).strip():
                    extra["ContentType"] = str(content_type).strip()
                cre = client.create_multipart_upload(**extra)
                upload_id = cre.get("UploadId")
                if not upload_id:
                    raise RuntimeError("create_multipart_upload missing UploadId")
            buffer.extend(chunk)
            _flush(force=False)
            _report_cos()

        if upload_id is None:
            extra_put: dict = {
                "Bucket": bucket_resolved,
                "Key": key,
                "Body": bytes(buffer) if buffer else b"",
                "StorageClass": "STANDARD",
                "EnableMD5": False,
            }
            if content_type and str(content_type).strip():
                extra_put["ContentType"] = str(content_type).strip()
            client.put_object(**extra_put)
            uploaded = len(buffer)
            buffer.clear()
            _report_cos()
            logging.info("Stream uploaded to COS (put_object): %s", image_url)
            return image_url

        _flush(force=True)
        _report_cos()
        if not parts:
            client.abort_multipart_upload(Bucket=bucket_resolved, Key=key, UploadId=upload_id)
            raise RuntimeError("multipart upload: no parts")

        parts_sorted = sorted(parts, key=lambda x: int(x["PartNumber"]))
        # 腾讯云 qcloud_cos 的 dict_to_xml 要求键名为 Part（列表），用 Parts 会得到「Part Is Required」
        client.complete_multipart_upload(
            Bucket=bucket_resolved,
            Key=key,
            UploadId=upload_id,
            MultipartUpload={"Part": parts_sorted},
        )
        logging.info("Stream uploaded to COS (multipart): %s", image_url)
        return image_url
    except Exception:
        if upload_id:
            try:
                client.abort_multipart_upload(Bucket=bucket_resolved, Key=key, UploadId=upload_id)
            except Exception:
                pass
        raise
