# Operations

部署 / 运维注意事项。这套 HTTP 层在「大文件 + 双通道进度」场景下对反向代理、
worker 模型、Redis 命名空间都有些 nontrivial 要求；本文档把这些坑列清楚。

---

## 1. Nginx 反代

如果生产环境前面有 nginx（典型部署），**必须**调以下几项，否则要么大文件
413 / 504、要么 SSE 进度条卡死：

```nginx
http {
  # ============== 全局 ==============
  client_max_body_size 20G;            # 与 register_upload_routes(max_bytes=...) 对齐
  client_body_buffer_size 1m;          # 单连接的内存缓冲；流式时其实只是 hint
  client_body_timeout    600s;         # 单 chunk 间最大空闲；超大文件 + 慢网络要拉长
  send_timeout           600s;

  server {
    listen 443 ssl http2;
    server_name myapp.example.com;

    # ============== 关键：上传路由不要让 nginx 整段缓冲 ==============
    location /api/file_to_url {
      proxy_request_buffering off;     # 关键：不要等 nginx 收完再转发，否则丧失流式 + 服务端进度
      proxy_buffering        off;      # 同样关键：响应也不要缓冲（虽然这条路返回小 JSON 影响不大）
      proxy_http_version     1.1;
      proxy_read_timeout     86400s;   # 大文件 + 慢网络可以撑很久；按 SLA 调
      proxy_send_timeout     86400s;
      proxy_pass             http://127.0.0.1:8300;

      # 透传必要 header
      proxy_set_header Host $host;
      proxy_set_header X-Real-IP $remote_addr;
      proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
      proxy_set_header X-Forwarded-Proto $scheme;
    }

    # ============== SSE 路由：禁缓冲 ==============
    location /api/file_to_url/progress {
      proxy_buffering        off;      # 关键：SSE 必须实时下行
      proxy_cache            off;
      proxy_http_version     1.1;
      proxy_read_timeout     7200s;    # 与 redis_state_ttl_sec 对齐
      proxy_pass             http://127.0.0.1:8300;

      proxy_set_header Host $host;
      proxy_set_header X-Real-IP $remote_addr;
      proxy_set_header Connection '';  # 关键：不要让 nginx 把 keep-alive 关了
    }

    # ... 其它路由 ...
  }
}
```

**为什么关键**：

- `proxy_request_buffering off`：默认 nginx 收完整段请求体才转发到 upstream，这会让
  `flow_upload.http.stream_upload_to_cos` 的「边收边上传 COS」完全失效——服务端要等
  nginx 整段收完才看到第一个字节，`server_receive` 进度从 0 跳到 100%、`cos_upload`
  根本来不及推进就一次性结束。**必须关闭**。

- `proxy_buffering off` + `Connection ''`（SSE 路由）：默认 nginx 会攒 8K 缓冲再下发，
  SSE 客户端会出现「几秒钟没数据 → 一次推送多帧」的卡顿表现。`flow_upload.http.routes`
  返回的响应里已经带 `X-Accel-Buffering: no` header（这是 nginx-specific signal），
  nginx **会尊重**它，但**只在** `proxy_buffering on/off` 默认值未被站点覆盖时生效；
  保险起见 location 级别明确 `off`。

- `proxy_read_timeout` / `proxy_send_timeout`：HTTP 上传方向，巨型文件（10+ GB）+
  低带宽链路总耗时可达 1-2 小时。默认 60s 直接断掉。SSE 这条 7200s 与
  `redis_state_ttl_sec` 默认值对齐（再大也没意义，超出 TTL 之后 Redis 已没数据）。

---

## 2. Worker 模型 / 并发

### 2.1 `max_concurrent` 的语义

`register_upload_routes(max_concurrent=N)` 创建一个 `asyncio.Semaphore(N)`。
**这是 per-process 的**：每个 uvicorn worker 进程独立持有自己的 semaphore。

总并发 = `uvicorn --workers <W>` × `max_concurrent`

如果你跑 4 worker × `max_concurrent=8`，总能有 32 个并发上传在 COS 工作线程里。

### 2.2 推荐值

| 场景 | uvicorn workers | `max_concurrent` |
|---|---|---|
| 单机 SaaS（< 100 并发用户） | 2-4 | 8 |
| 中等流量（< 1000 并发） | CPU 核数 | 16 |
| 大文件为主（> 1 GB / 上传） | CPU 核数 ÷ 2 | 4 |

注意：每个并发上传至少占一个 COS 工作线程 + 一个 ASGI request 协程；**内存**上每个
进行中的 multipart 上传持有 ≥ 5 MiB 的 part buffer（`part_size` 默认值）。32 并发 = 32 × 5 MiB = ~160 MB 常驻；可接受但要算进部署预算。

### 2.3 跨 worker 的进度状态

`server_receive` / `cos_upload` 状态存在 Redis，**所有 worker 共享**。所以浏览器发
SSE 请求时被路由到哪个 worker 都没关系，能读到同一份进度。

---

## 3. Redis 命名空间

### 3.1 默认命名空间

| 用途 | 默认 key 前缀 | 默认 TTL |
|---|---|---|
| 进度状态 JSON | `flow_upload:f2u_prog:` | 7200 秒 |
| owner 映射（progress_id → user_id） | `flow_upload:f2u_prog_owner:` | 7200 秒 |

每条记录的 key 形如 `flow_upload:f2u_prog:8e72a0c5-3b2d-4f...`。

### 3.2 多业务共存 / 老数据迁移

如果你在同一个 Redis db 上跑多个 `register_upload_routes(...)` 实例（不同的
`route_prefix`），**应当**给每个实例不同的 `redis_state_prefix` /
`redis_owner_prefix`，避免 progress_id 命名空间相互污染：

```python
register_upload_routes(app, ..., route_prefix="/api/chat/upload",
    redis_state_prefix="myapp:chat_prog:",  redis_owner_prefix="myapp:chat_prog_owner:")
register_upload_routes(app, ..., route_prefix="/api/release/upload",
    redis_state_prefix="myapp:release_prog:", redis_owner_prefix="myapp:release_prog_owner:")
```

如果你从其它实现迁移过来（譬如 Flops 的 `flops:f2u_prog:` 老前缀），**保持原前缀**
就能在升级期间继续读老数据：

```python
register_upload_routes(app, ...,
    redis_state_prefix="flops:f2u_prog:",
    redis_owner_prefix="flops:f2u_prog_owner:")
```

### 3.3 Redis 实例选型

进度数据的特点：
- 高频写（每个上传每 100ms 写一次入网进度 + 每次 part 完成写一次 cos 进度）
- 短 TTL（默认 2 小时）
- 单 key 大小很小（一条进度 JSON ~200 bytes）

任何标准 Redis 都能扛。如果生产环境内存紧张，可以为这两个 prefix 设
`maxmemory-policy volatile-ttl`，让短 TTL 数据先被淘汰。

### 3.4 lazy redis 客户端

如果你的 Redis 实例在 `@app.on_event("startup")` 里才赋值（典型：连接池要用 asyncio
事件循环），`register_upload_routes` 在模块加载时调用会拿到 `None`。**传 callable**：

```python
redis_client_user = None  # 在 startup 里赋值

@app.on_event("startup")
def _startup():
    global redis_client_user
    redis_client_user = Redis(host=..., port=..., db=7)

register_upload_routes(
    app,
    redis_for_progress=lambda: redis_client_user,   # 迟绑定
    ...
)
```

ProgressStore 在每次需要 Redis 时调用 callable 取最新值；启动后才会真正用到。
若 callable 仍返回 `None` 时（启动失败 / 错配），进度更新会 raise `RuntimeError`
被路由捕获后转成 500 响应。

---

## 4. 超时调优

### 4.1 不同层的超时清单

| 层 | 默认 | 建议 |
|---|---|---|
| nginx `proxy_read_timeout` / `proxy_send_timeout` | 60s | 大文件路由 86400s；SSE 7200s |
| nginx `client_body_timeout` | 60s | 600s |
| uvicorn `--timeout-keep-alive` | 5s | 大文件路由别走 keep-alive 或拉到 60s |
| `register_upload_routes(sse_max_ticks=N)` | 48000（≈96 分钟） | 大文件 SLA 长的拉到 144000（≈4.8 小时） |
| `register_upload_routes(redis_state_ttl_sec=N)` | 7200（2 小时） | 同上，与 sse_max_ticks 对齐 |
| `multipart_upload_from_chunk_queue` 内部 `t.join(86400)` | 24 小时 | 不要改；这是 COS 后台线程的 hard ceiling |

### 4.2 实测建议

20 GiB 文件、100 Mbps 上行 → 完整传输 ≈ 30 分钟。预留 2 倍 buffer 时间，
所有 \*_timeout 都至少 1 小时；测试期间记得拿大文件实测一遍 SSE 在 50% 进度时不会断。

---

## 5. 凭证管理

### 5.1 优先级（高 → 低）

1. `set_credentials_provider(fn)` 注入的 callable
2. 宿主项目 `config.py` 的 `TENCENT_SECRET_ID` / `TENCENT_SECRET_KEY`
3. 环境变量 `TENCENT_SECRET_ID` / `TENCENT_SECRET_KEY`

都失败 → `RuntimeError`。**没有硬编码默认值**。

### 5.2 推荐做法

- **本地开发 / 单机部署**：在 `config.py` 写死（注意不要 commit）
- **CI / 容器化部署**：用环境变量
- **多租户 / 凭证轮换**：注入 provider，让 provider 从你的 secret manager（Vault / AWS Secrets / 自建）读取

```python
from flow_upload import set_credentials_provider

def _provider():
    # 从 Vault 读，每次都拉最新（如果 secret rotate 了也能马上生效）
    secret = vault_client.read_secret("tencent-cos")
    return secret["secret_id"], secret["secret_key"]

set_credentials_provider(_provider)
```

注意 `_provider` 会在**每次上传**时被调用一次（`get_tencent_credentials` → `_make_cos_client`），不要在里面做重活；如果 secret manager 调用慢，自己加个 LRU
缓存 + 短 TTL（如 5 分钟）。

### 5.3 凭证最小权限

腾讯云 CAM 给上传函数用的子账户至少需要：
- `cos:PutObject`（单次 put_object）
- `cos:InitiateMultipartUpload` / `UploadPart` / `CompleteMultipartUpload` / `AbortMultipartUpload`（multipart 路径）
- 限制到本应用 bucket 的 ARN

---

## 6. 监控建议

至少建立这几个指标：

| 指标 | 来源 | 含义 |
|---|---|---|
| 上传 RPS / 字节速率 | nginx access log | 流量趋势 |
| 上传 P95 / P99 耗时 | nginx access log | SLA |
| HTTP 413 / 5xx 计数 | nginx access log | 异常比例 |
| Redis 进度 key 数量 | `redis-cli --scan --pattern '<state_prefix>*' \| wc -l` | 在途上传数（粗略） |
| `cos_upload.bytes_done == 0 && time > 30s` 的会话 | 自定义脚本扫描 Redis | nginx `proxy_request_buffering` 配错的早期信号 |

最后一项是「曾经踩过的坑」的早期警报：如果 Redis 里大量 progress 状态 30 秒后 cos_upload 还是 0，说明 nginx 在等收完整段 body，配置有问题。
