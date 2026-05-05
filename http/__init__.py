"""flow_upload.http: FastAPI / Web 层封装。

`register_upload_routes(app, ...)` 注册两条路由：
- POST  {prefix}             multipart 上传（可选 progress_id + expected_bytes）
- GET   {prefix}/progress    SSE 双通道进度（server_receive + cos_upload）

业务策略（object_key_strategy）与身份系统（auth_resolver）通过参数注入，
本模块不绑定 chat / avatar / desktop release 等任何具体业务。

进度 JSON 形状（公开协议，跨语言客户端依赖此结构）：
    {
      "server_receive": {"bytes_done": int, "bytes_total": int|None, "done": bool},
      "cos_upload":     {"bytes_done": int, "bytes_total": int|None, "done": bool},
      "done": bool,
      "error": str|None,
      "_sse_tick": int   # 仅 SSE 帧含此字段
    }

注意：本 subpackage 依赖 fastapi / starlette / python_multipart；顶层
`from flow_upload import ...` 不会触发本 subpackage 的 import，FastAPI
非纯传输层调用方的 hard dependency。
"""

from .progress import ProgressStore
from .routes import register_upload_routes
from .strategy import ObjectKeyContext, ObjectKeyStrategy

__all__ = [
    "ObjectKeyContext",
    "ObjectKeyStrategy",
    "ProgressStore",
    "register_upload_routes",
]
