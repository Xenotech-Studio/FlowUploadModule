"""ObjectKeyStrategy 抽象。

策略从「这次请求的元信息」决定上传到 COS 的 (folder_prefix, object_name)，
与流式 multipart 解析、Redis 进度、SSE 路由等基础设施完全解耦。

业务侧（chat / avatar / desktop release / paper export 等）只需写一个 ctx → (folder, name)
的纯函数，传入 register_upload_routes(...)。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, Optional, Tuple


@dataclass(frozen=True)
class ObjectKeyContext:
    """传给 ObjectKeyStrategy 的只读视图。

    字段：
      user_id:      auth_resolver 解析出的当前用户（无 token 上传时为 None）
      progress_id:  本次上传的进度通道 id（前端不开进度通道时为 None）
      safe_name:    multipart 里 file part 的 filename basename（已防注入）
      started_ms:   本次请求处理开始的 ms 时间戳
      form_fields:  解析后的表单字段值（含 "folder" 与 register_upload_routes 中
                    extra_form_fields 声明的所有字段名；未传字段值为空字符串）
    """

    user_id: Optional[str]
    progress_id: Optional[str]
    safe_name: str
    started_ms: int
    form_fields: Dict[str, str] = field(default_factory=dict)


# (folder_prefix, object_name) —— folder_prefix 末尾不需要 "/"（cos_upload 内部会补）
ObjectKeyStrategy = Callable[[ObjectKeyContext], Tuple[str, str]]
