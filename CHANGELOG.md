# Changelog

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
遵循语义化版本（[SemVer](https://semver.org/lang/zh-CN/)）。

> 当前 0.x 阶段，公开 API surface 被认为是 stable，但保留小幅形状调整的权利。
> 1.0 之前任何 minor bump 都可能新增字段 / 新增可选参数（向后兼容），patch bump
> 仅修 bug；major bump 在 1.0 之前等价于 minor，1.0 之后才严格遵循 SemVer。
> 协议层稳定性保证见 [`docs/PROTOCOL.md` §3](docs/PROTOCOL.md#3-公开协议稳定性)。

---

## [0.2.0] - 2026-05-05

### Added

- **`flow_upload.http` subpackage**：FastAPI / 流式 multipart / Redis 双通道进度 / SSE 路由的整体抽象
  - `register_upload_routes(app, *, redis_for_progress, auth_resolver, object_key_strategy, max_bytes, ...)` —— 一次注册 POST 上传 + GET SSE 进度两条路由
  - `ObjectKeyContext` —— 业务策略闭包的输入 dataclass（user_id / progress_id / safe_name / started_ms / form_fields）
  - `ObjectKeyStrategy` —— 类型别名 `Callable[[ObjectKeyContext], Tuple[str, str]]`
  - `ProgressStore` —— 进度状态机；同 progress_id 上读改写持锁，避免 server_receive / cos_upload 互相覆盖
- `ProgressStore` 的 `redis_client` 同时支持 Redis 实例和 `Callable[[], Redis]`，应对宿主在 startup 后才赋值的场景
- HTTP 协议明确字段、错误码、owner 模型；浏览器 / Python / curl 客户端示例

### Documentation

- 新增 `docs/API.md` —— 每个公开符号的完整签名 / 参数 / 异常 / 示例
- 新增 `docs/PROTOCOL.md` —— HTTP / SSE 公开协议规格
- 新增 `docs/RECIPES.md` —— 4 个典型业务接入 cookbook（chat / avatar / desktop release / paper 导入）
- 新增 `docs/OPERATIONS.md` —— nginx 配置、worker 模型、Redis 命名空间、超时、凭证优先级
- README 改为「5 分钟接入 + 文档导航」结构

---

## [0.1.0] - 2026-05-05

首个版本：纯传输层 primitives。代码源自 Flops `user_system/cos_upload.py`，
**唯一行为差异**是去除硬编码默认 SecretId / Key（见下文 Security）。

### Added

- `flow_upload.file_to_url(file, folder_name, bucket, ...)` —— UploadFile 一次性 `put_object`
- `flow_upload.bytes_to_cos_url(body, *, folder_name, object_name, ...)` —— 内存 bytes 一次性 `put_object`
- `flow_upload.multipart_upload_from_chunk_queue(q, *, folder_name, object_name, ...)` —— 队列消费式 multipart 边收边传
- `flow_upload.get_tencent_credentials()` —— 凭证统一查找入口
- `flow_upload.set_credentials_provider(fn)` —— 注入凭证 provider
- 凭证查找链：注入 provider → 宿主 `config.py` → 环境变量
- 常量：`DEFAULT_BUCKET` / `DEFAULT_REGION` / `DEFAULT_SCHEME`

### Security

- **移除硬编码默认 SecretId / SecretKey**：上游 `user_system/cos_upload.py` 含两个写死的腾讯云子账号凭证作为 fallback。本仓库不带任何默认 SecretId / Key；查找链全部失败时直接 raise `RuntimeError`，避免误用同一对凭证泄漏的风险
