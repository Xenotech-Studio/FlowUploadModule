"""
FlowUpload: 高可用文件上传模块（腾讯云 COS）

设计目标：
- **可复用 / 跨应用**：不绑定具体业务（chat / avatar / desktop release 等），
  上传到 COS 内的「哪个目录、什么对象名」由调用方决定（object_key 策略）。
- **不绑特定身份系统**：用户态、token 解析、Redis 进度等由调用方注入。
- **凭证不硬编码**：通过显式 provider 注入或宿主项目 `config.py` / 环境变量读取。

公开 API（顶层 = 纯传输层）：
- file_to_url(file, ...): 单次 put_object，UploadFile 流
- bytes_to_cos_url(body, ...): 单次 put_object，内存 bytes
- multipart_upload_from_chunk_queue(q, ...): 队列消费式 multipart 边收边传
- get_tencent_credentials(): (secret_id, secret_key) 共用查找
- set_credentials_provider(fn): 注入凭证 provider（最高优先级）

HTTP / FastAPI 层（按需 import，避免顶层硬依赖 fastapi）：
- from flow_upload.http import register_upload_routes, ObjectKeyContext, ObjectKeyStrategy

详见 README.md。
"""

from __future__ import annotations

from .cos_upload import (
    file_to_url,
    bytes_to_cos_url,
    multipart_upload_from_chunk_queue,
    get_tencent_credentials,
    set_credentials_provider,
    CredentialsProvider,
    DEFAULT_BUCKET,
    DEFAULT_REGION,
    DEFAULT_SCHEME,
)

__all__ = [
    "file_to_url",
    "bytes_to_cos_url",
    "multipart_upload_from_chunk_queue",
    "get_tencent_credentials",
    "set_credentials_provider",
    "CredentialsProvider",
    "DEFAULT_BUCKET",
    "DEFAULT_REGION",
    "DEFAULT_SCHEME",
]
