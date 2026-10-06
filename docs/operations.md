# 演示服务运维

## 定位一次失败

HTTP 响应的 `X-Request-ID` 由服务生成，不能由客户端覆盖。请求完成日志是 JSON，使用 `request_id` 查询；图请求附带 `run_id`，启用追踪时附带 `trace_id`。日志记录结果、状态、排队和阶段耗时、意图、证据数量、索引摘要、缓存状态、模型用量是否已知以及代码/提示词源码/配置摘要。

不要把嵌套、并发的阶段时间相加当成总延迟。审批跨 HTTP 请求，以 HMAC 脱敏会话/草稿 ID 和版本关联，每个请求有自己的运行记录；用户思考时间不计入模型执行时间。`TELEMETRY_HASH_KEY` 可独立配置，否则部署使用 `AUTH_SECRET`；换密钥会改变关联摘要，摘要不等于匿名化。

检索共享任务沿用 Phase 7 的单次用量归属，等待请求记录等待而不重复计费；热缓存明确为零外部 Embedding 调用。模型没有返回 usage 时标为未知，不填零。JSON 日志采用元数据白名单，原始消息、工单正文、工具参数、文档块、身份、URL 和异常原文不进入默认遥测。第三方普通日志只保留组件与严重级别；详细原因优先看请求稳定原因码及能力诊断。

## 检查和降级

| 检查/故障 | 行为 |
| --- | --- |
| `/health/live` 与兼容 `/health` | 只验证进程响应，不触发依赖或模型调用 |
| `/health/ready` | 业务 schema、Checkpoint/Store 连接与表、Agent 初始化，2 秒期限 |
| `/health/capabilities` | Bearer 保护；诊断短期缓存 5 秒；无付费调用 |
| PostgreSQL 中断/schema 不符 | 核心 503；不退回 SQLite |
| Redis 中断 | 新问答拒绝；审批/读取使用本地有界准入回退；核心就绪仍为 200 |
| Qdrant/索引不可用 | 检索不可用，不解释为“没有知识”；无关业务继续 |
| Langfuse 不可达 | 仅追踪降级；不摘除核心业务 |
| 模型配置存在 | 只表明已配置，不证明供应商当时可调用 |

详细端点不返回内部连接地址、密钥或异常文本。健康依赖解决启动顺序，运行中的断连依靠实际错误处理和连接池恢复。

## Redis 内存与降级

Redis 内存压力检查：先核对 `CONFIG GET maxmemory maxmemory-policy`、容器 `HostConfig.Memory`，再查看 `INFO memory` 的 `used_memory`、`used_memory_rss`、`mem_not_counted_for_evict` 和 `INFO stats` 的 `evicted_keys`。数据预算不是进程 RSS 上限；本项目固定 noeviction，不能将它改为逐出限流键的策略来掩盖压力。[Redis 官方说明](https://redis.io/docs/latest/reference/eviction/)。

达到数据预算后，缓存 SET 失败不会丢弃已算出的检索结果；入口限流写失败会返回 503，审批与读取使用既有有界本地回退，PostgreSQL 幂等约束仍生效。`/health/ready` 保持核心就绪，能力诊断按 Redis 内存统计返回 `redis_memory_limit`，短缓存最多 5 秒。此诊断不是每次未来写入的成功保证。处理时应定位缓存增长、等待 TTL、调整有余量的预算或清理已确认归属的测试键；不要在日常实例执行 FLUSHALL。缓存与限流共用内存的可用性取舍继续保留。

## 追踪配置与边界

默认 `LANGFUSE_TRACING=false`。开启需要接收端地址、公钥和密钥，使用合成任务验收后再使用。SDK 版本固定在锁文件。原有 callback 覆盖图、模型与工具，另加请求根记录，避免重复执行模型。自动 callback 的输入输出先删除，再经最终 OTLP exporter 白名单过滤；span 名称、事件、异常描述、资源和额外元数据都不直接透传。

导出队列最多 256 spans，批量 32，网络发送期限 2 秒，关闭限时等待约 3 秒，过滤失败丢弃整批，不退回原文。本地 HTTP OTLP 接收验证只证明序列化与过滤契约，不能证明远程 Langfuse 平台已接收、入库或查询成功。远程验收状态见 [阶段报告](phase9_deployment_observability.md)。默认不提供在线完整内容日志开关；Phase 8/9 合成评测原文存档与在线遥测分离。

## PostgreSQL 备份与恢复

以下是专属演练环境的命令，数据库名、项目名应显式核对。密码来自容器环境，不放在命令行。

```sh
docker compose --env-file .env.compose -p esa-demo exec -T postgres pg_dump -U support_demo -d enterprise_support -Fc -f /tmp/support.dump
docker compose --env-file .env.compose -p esa-demo cp postgres:/tmp/support.dump ./support.dump
docker compose --env-file .env.compose -p esa-demo exec -T postgres createdb -U support_demo support_restore
docker compose --env-file .env.compose -p esa-demo exec -T postgres pg_restore -U support_demo -d support_restore --exit-on-error /tmp/support.dump
```

`pg_dump` 使用数据库一致快照；记录备份时间、应用提交、schema 版本和索引版本。实际演练逐表比较行数与排序后的行内容摘要，涵盖业务工单、Checkpoint、会话归属和偏好。日常恢复应在独立环境核对后再切换连接，不能直接覆盖原库。备份含对话和工单内容，属于本地私有资产，禁止提交 Git。

本地 Qdrant 命名卷保留重启状态。恢复可使用其快照或从冻结 `snapshot_v2.json` 显式重建新 collection；重建需要 Embedding 预算，需核对 manifest、模型/维度和 chunk inventory 后再切换。Redis 缓存不按工单标准备份，丢失缓存及速率状态不会删除数据库幂等记录。

## 回滚与常见错误

- 镜像回滚：先确认旧代码可读当前 schema，指定已验证的镜像版本。
- 配置回滚：保留非秘密配置摘要，重新核对模型、索引与缓存命名空间契约。
- 数据库回滚：不会自动执行反向迁移；必要时从备份恢复新库并验证。旧镜像不能自动撤销 schema 变化。
- 索引回滚：切换到已验证、仍保留的 immutable collection；不要改写旧 collection 内容。
- 响应丢失但可能已创建工单：使用原 thread/draft/version 核查或重复批准原请求，不新建另一个草稿。
- Docker Hub 超时：检查 Docker Desktop 和调用 CLI 的终端代理；本地代理端口因设备而异，不写入仓库配置。
- 磁盘耗尽/只读：停止构建、释放空间后重启引擎；若缓存数据库损坏，先确认数据保留需求再恢复，不能默认清空所有卷。

本阶段仍是单 worker、声明身份、共享密钥、模拟业务工具的演示服务，不具备完整登录、多租户授权、多实例图锁、高可用或生产 SLA。
