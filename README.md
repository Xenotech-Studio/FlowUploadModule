# FlowUploadModule

Python 端「高可用文件上传」可复用模块，目标：把腾讯云 COS 上传 + 边收边传 +
进度反馈这套基础设施从某个具体业务里剥出来，做成跨应用可复用的 submodule。

代码源自 Flops（`Xenotech-Studio/Flops`）的 `user_system/cos_upload.py`，原本因为
头像上传顺手塞进了用户系统；现在抽离独立。

---

## 当前阶段

**Phase 1**（已落地）：纯传输层 primitives。

- `file_to_url(file, ...)` —— UploadFile 一次性 put_object
- `bytes_to_cos_url(body, ...)` —— 内存 bytes 一次性 put_object
- `multipart_upload_from_chunk_queue(q, ...)` —— 从队列消费式 multipart 边收边传
- `get_tencent_credentials()` / `set_credentials_provider(fn)` —— 凭证统一入口

行为与原 `user_system.cos_upload` 一致；唯一差异是**移除了硬编码默认密钥**，
凭证必须从 provider / 宿主 `config.py` / 环境变量任一处可取得，缺失时抛错。

## 路线图

- **Phase 2**：HTTP/Web 层抽象
  - 流式 multipart 解析 + COS chunk 队列对接（不经过 FastAPI UploadFile，避免整段落盘）
  - Redis 双通道进度状态机（`server_receive` + `cos_upload`）
  - SSE 路由：`GET <prefix>/progress?progress_id=...`
  - `register_upload_routes(app, ...)`：业务策略与身份解析全部以参数注入
- **Phase 3**：执行端 SSE forwarder（Python），等价于 Flops 现有的
  `_forward_f2u_cos_progress_sse` 但通用化（无 chat/attachment 私货）。
- **Phase 4**：清理旧调用方，移除 `user_system.cos_upload` 的兼容 shim。

前端 SDK 见配套 submodule：[FlowUploadModuleSDK](https://github.com/Xenotech-Studio/FlowUploadModuleSDK)
（XHR 上传、SSE 进度订阅）。

---

## 设计取向

- **不绑业务**：上传到 COS 内的「哪个目录、什么对象名」（CHAT_FILES / AVATARS /
  DESKTOP_UPDATES 等）由调用方注入策略，本模块不维护任何业务命名。
- **不绑身份系统**：用户态、token 校验、Redis 进度由调用方注入。
- **凭证不硬编码**：仓库内不存任何 SecretId/Key 默认值。

## 安装与使用

作为 git submodule：

```bash
git submodule add git@github.com:Xenotech-Studio/FlowUploadModule.git flow_upload
```

确保宿主项目 `sys.path` 能找到仓库根目录后即可：

```python
from flow_upload import file_to_url, bytes_to_cos_url, multipart_upload_from_chunk_queue

url = file_to_url(uploaded_file, folder_name="AVATARS", bucket="my-bucket")
```

凭证可显式注入（最高优先级）：

```python
from flow_upload import set_credentials_provider

def _provider():
    import config
    return config.TENCENT_SECRET_ID, config.TENCENT_SECRET_KEY

set_credentials_provider(_provider)
```

未注入 provider 时按以下顺序回退：宿主 `config.py` 的 `TENCENT_SECRET_ID/KEY` →
环境变量 `TENCENT_SECRET_ID / TENCENT_SECRET_KEY`。
