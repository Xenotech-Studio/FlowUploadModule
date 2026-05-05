"""上传进度状态机：在 Redis 上单 key 维护双通道（server_receive + cos_upload）的合并 JSON。

为什么要锁：
    server_receive（asyncio 事件循环里推进）与 cos_upload（COS 工作线程里推进）
    会同时改同一个 key 的不同子对象。无锁时其中一方会用 stale snapshot 整份覆盖
    另一方刚写入的最新值，表现为：cos_upload 长期是 0、上传结束才一次拉满。

JSON 形状是公开协议（跨语言 SSE 客户端依赖），见 http/__init__.py 顶部。
"""

from __future__ import annotations

import json
import threading
import uuid
from typing import Any, Callable, Dict, Optional, Union


# Redis 实例或实例工厂——后者用于宿主在 startup 之后才创建 Redis 连接的场景
# （例如 Flops 的 redis_client_user 在 @app.on_event("startup") 里赋值，
#  register_upload_routes 在模块加载阶段即被调用，此时直传实例会拿到 None）。
RedisLike = Any
RedisSource = Union[RedisLike, Callable[[], RedisLike]]


class ProgressStore:
    """对单个 (state_prefix, owner_prefix) 命名空间下所有 progress_id 的读写做集中协调。

    redis_client 可传：
      - Redis-like 实例（同步）
      - 0 参 callable，每次需要 Redis 时调用并返回实例（迟绑定，应对宿主延迟初始化）
    """

    def __init__(
        self,
        *,
        redis_client: RedisSource,
        state_prefix: str,
        owner_prefix: str,
        ttl_sec: int,
    ) -> None:
        if redis_client is None:
            raise ValueError("redis_client is required")
        self._r_src: RedisSource = redis_client
        self._sp = str(state_prefix or "")
        self._op = str(owner_prefix or "")
        self._ttl = int(ttl_sec)
        self._locks: Dict[str, threading.Lock] = {}

    def _r(self) -> RedisLike:
        """每次请求时解析 Redis 实例：callable 工厂 → 调用一次取最新；实例 → 直接返回。

        宿主把 redis_for_progress=lambda: my_redis_client_user 传进来时，每次请求
        都重新读 my_redis_client_user 的当前值；适合 Redis 在模块 startup 才赋值
        的场景。
        """
        src = self._r_src
        if callable(src):
            r = src()
            if r is None:
                raise RuntimeError(
                    "ProgressStore: redis_client callable returned None "
                    "(Redis 尚未初始化？请确认 startup 已完成)"
                )
            return r
        return src

    @staticmethod
    def normalize_pid(raw: Optional[str]) -> Optional[str]:
        """空白返回 None；非空但不是合法 UUID 抛 ValueError。"""
        if raw is None or not str(raw).strip():
            return None
        s = str(raw).strip()
        try:
            uuid.UUID(s)
        except (ValueError, TypeError, AttributeError) as e:
            raise ValueError("Invalid progress_id") from e
        return s

    def _state_key(self, pid: str) -> str:
        return f"{self._sp}{pid}"

    def _owner_key(self, pid: str) -> str:
        return f"{self._op}{pid}"

    def _lock(self, pid: str) -> threading.Lock:
        lk = self._locks.get(pid)
        if lk is None:
            lk = threading.Lock()
            self._locks[pid] = lk
        return lk

    def _release_lock(self, pid: str) -> None:
        self._locks.pop(pid, None)

    # ---------------- read ----------------

    def get_state(self, pid: str) -> Optional[Dict[str, Any]]:
        raw_b = self._r().get(self._state_key(pid))
        if not raw_b:
            return None
        try:
            s = raw_b.decode("utf-8") if isinstance(raw_b, bytes) else str(raw_b)
            out = json.loads(s)
            return out if isinstance(out, dict) else None
        except Exception:
            return None

    def get_owner(self, pid: str) -> Optional[str]:
        v = self._r().get(self._owner_key(pid))
        if v is None:
            return None
        return v.decode("utf-8") if isinstance(v, bytes) else str(v)

    # ---------------- write ----------------

    def _set_state(self, pid: str, obj: Dict[str, Any]) -> None:
        self._r().setex(self._state_key(pid), self._ttl, json.dumps(obj, ensure_ascii=False))

    def begin(
        self,
        pid: str,
        user_id: str,
        *,
        expected_total: Optional[int] = None,
    ) -> None:
        """初始化（或对相同 owner 重置）一个 progress_id 的状态。

        如果该 progress_id 已被其他用户 owner 持有，抛 PermissionError；
        否则新建 owner 并把 state 重置为初始空帧。
        """
        with self._lock(pid):
            existing = self.get_owner(pid)
            if existing and existing != str(user_id):
                raise PermissionError("progress_id not owned by current user")
            if not existing:
                self._r().setex(self._owner_key(pid), self._ttl, str(user_id))
            tot = int(expected_total) if expected_total is not None else None
            self._set_state(
                pid,
                {
                    "server_receive": {"bytes_done": 0, "bytes_total": tot, "done": False},
                    "cos_upload": {"bytes_done": 0, "bytes_total": tot, "done": False},
                    "done": False,
                    "error": None,
                },
            )

    def server_receive_bytes(self, pid: str, done: int, total: Optional[int] = None) -> None:
        with self._lock(pid):
            cur = self.get_state(pid) or {}
            sr_prev = cur.get("server_receive")
            sr: Dict[str, Any] = dict(sr_prev) if isinstance(sr_prev, dict) else {}
            sr["bytes_done"] = int(done)
            if total is not None:
                sr["bytes_total"] = int(total)
            sr["done"] = False
            cur["server_receive"] = sr
            if not isinstance(cur.get("cos_upload"), dict):
                cur["cos_upload"] = {"bytes_done": 0, "bytes_total": None, "done": False}
            self._set_state(pid, cur)

    def server_receive_done(self, pid: str, total: int) -> None:
        t = int(total)
        with self._lock(pid):
            cur = self.get_state(pid) or {}
            sr_prev = cur.get("server_receive")
            sr: Dict[str, Any] = dict(sr_prev) if isinstance(sr_prev, dict) else {}
            sr["bytes_done"] = t
            sr["bytes_total"] = t
            sr["done"] = True
            cur["server_receive"] = sr
            cu_prev = cur.get("cos_upload")
            cu: Dict[str, Any] = dict(cu_prev) if isinstance(cu_prev, dict) else {}
            cu["bytes_total"] = t
            cur["cos_upload"] = cu
            self._set_state(pid, cur)

    def cos_uploaded_bytes(self, pid: str, done: int) -> None:
        with self._lock(pid):
            cur = self.get_state(pid) or {}
            cu_prev = cur.get("cos_upload")
            cu: Dict[str, Any] = dict(cu_prev) if isinstance(cu_prev, dict) else {}
            cu["bytes_done"] = int(done)
            cu["done"] = False
            cur["cos_upload"] = cu
            if "error" not in cur:
                cur["error"] = None
            self._set_state(pid, cur)

    def finish_ok(self, pid: str, total: int) -> None:
        t = int(total)
        with self._lock(pid):
            self._set_state(
                pid,
                {
                    "server_receive": {"bytes_done": t, "bytes_total": t, "done": True},
                    "cos_upload": {"bytes_done": t, "bytes_total": t, "done": True},
                    "done": True,
                    "error": None,
                },
            )
        self._release_lock(pid)

    def finish_err(self, pid: str, err: str) -> None:
        with self._lock(pid):
            cur = self.get_state(pid) or {}
            for k in ("server_receive", "cos_upload"):
                sub = cur.get(k)
                d: Dict[str, Any] = dict(sub) if isinstance(sub, dict) else {}
                d["done"] = True
                cur[k] = d
            cur["done"] = True
            cur["error"] = str(err)[:800]
            self._set_state(pid, cur)
        self._release_lock(pid)
