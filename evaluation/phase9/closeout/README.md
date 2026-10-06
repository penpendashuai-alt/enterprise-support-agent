# Phase 9 收尾验收

2026-10-06：本阶段核心收尾验收完成，可选验证范围见限制。Redis 资源边界、真实服务本地/远程追踪和网页辅助检查通过；GitHub Actions 五个必需 job 全部成功。最终状态见 `validation.json`，远端 CI 证据见 `github-actions.json`。

| 记录 | 范围 |
| --- | --- |
| `baseline.json` | 收尾前 1357 份实际工作区文件摘要及本地可恢复 ZIP 摘要 |
| `redis-compose-contracts.json` | 默认/local/CI/dev 的 64 MiB 数据预算、noeviction、128 MiB 容器上限 |
| `redis-memory-v1/` | 首轮实际 OOM 后连接缓冲释放，缓存写失败条件不稳定；保留失败 |
| `redis-memory-v2/` | 稳定压力下 6 组检查通过：缓存、限流、审批幂等、存活与恢复 |
| `business-tracing-local-v1/` | 真实 HTTP Service 七个请求、54 spans、本地完整 OTLP 过滤与不可达退出 |
| `remote-tracing-v1/` | 指定远端项目七个业务请求，Observations API v2 查询、关联和全字段组敏感扫描 |
| `remote-tracing-ui.json` | 登录后界面核对：异常原因、请求/图 ID、空 Input/Output 和 ERROR 子记录 |
| `github-run-37436624165/` | CI 实际上传的 7 份脱敏 JSON 永久副本；摘要见 github-actions.json |
| `github-actions.json` | 实际交付代码提交、工作流/job 结果与公开资产位置 |
| `validation.json`、`integrity.json`、`final-integrity.json` | 本次检查范围、历史记录与最终发布私有资产边界 |

压力实例初始预算 4 MiB，实际填充至 OOM 后临时设为 3 MiB，以排除客户端释放缓冲带来的空余；恢复时仅删除专属键并恢复 4 MiB。该受控实验不证明任意负载下 128 MiB 都足够，也不改变默认 64 MiB 配置。缓存与限流共用内存，noeviction 不会静默逐出限流状态。

追踪使用真实图、数据库、Redis 和 Qdrant，模型与向量为明确的确定性测试组件。远端查询扫描 `core,basic,io,metadata,model,usage,time,prompt,metrics,trace_context` 字段组；公开记录只保留必要摘要，不公开原始身份、工单、密钥或项目配置。没有公开分享远程 trace。

默认无付费模型或 Embedding 调用，未重跑草稿或 Phase 8 质量实验。历史草稿仍为每轮 11 个已生成草稿忠实、1 个澄清；0.223848 元仍是返回 usage 估价，非精确物理调用账单。AppTest 不等于浏览器 WebSocket；CI Qdrant 不等于正式知识库付费导入；Hybrid、多实例和生产身份体系不在此次范围。

复现命令见 [部署说明](../../../docs/deployment.md)，运维取舍见 [运维说明](../../../docs/operations.md)。每次选择新项目名与输出目录，旧记录不得覆盖。

验收代码提交为 `2ac4ed5316ee69a028e8065e19259700bcb26ed4`：[实际 CI 运行](https://github.com/penpendashuai-alt/enterprise-support-agent/actions/runs/37436624165)。Python 三版本各 375 通过、4 跳过；Docker 构建、13 组业务检查、6 组压力检查、7 请求追踪、上传与清理均成功。文档和资产随后单独提交。push 未生成运行记录时增加显式 workflow_dispatch，并保留全部检查；未改云部署保护条件。
