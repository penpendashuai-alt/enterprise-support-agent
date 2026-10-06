# Phase 9 验证记录

2026-10-06：离线测试、实际容器业务与恢复、本地追踪导出已通过。后续收尾的 Redis 压力和远程 Langfuse API 已通过；GitHub Actions 与网页辅助核对状态以 [收尾索引](closeout/README.md) 为准。Docker 损坏数据盘已备份并重建，前三轮容器失败记录保留，最终第四轮通过。

| 资产 | 含义 |
| --- | --- |
| `baseline.json` | Phase 8 提交 c1ed166、1267 个文件摘要、私有可恢复 ZIP 的摘要 |
| `drafts-v1/` | 12 个真实草稿回归输入、最终草稿与用量；未保存原始结构化工具返回 |
| `draft-review.json` | 首轮逐条语义审阅，非独立审阅 |
| `drafts-v2/` | 同提示词复测，补齐结构化工具返回与澄清消息，保留首轮 |
| `draft-review-v2.json` | 完整记录版逐条语义审阅 |
| `tracing-v3/report.json` | 锁定 Langfuse SDK → 本地 HTTP OTLP 接收端；4 类合成轨迹、父子关系、过滤、队列及不可达清理 |
| `tracing-v2/report.json` | 早期接收成功记录，模型标签错误，已由 v3 替代 |
| `setup-failures.json` | 构建和追踪过程的失败、修正原因 |
| `docker-recovery.json` | Docker 修复、容器运行、构建镜像 ID 与源码摘要 |
| `containers-v1/` 至 `containers-v3/` | 原始失败记录；原因见 setup-failures.json |
| `containers-v4/` | 13 组容器验收通过、10 表备份恢复摘要、过滤后的实际请求日志 |
| `default-compose-v1.json` | 默认栈启动与既有云端索引健康检查，无付费请求 |
| `validation.json`、`integrity.json` | 检查汇总、预算口径、历史评测与隐私检查 |

草稿两轮均为 11 个草稿、1 个影响范围澄清，已生成草稿的事实状态审阅通过。两轮 24 次有返回的逻辑聊天调用保守估价合计 0.223848 元，返回结果缺失 usage 为 0，未调用付费 Embedding。第二轮为记录器修复复测，不用于筛选更好答案；历史 Phase 8 留出题不被称为新的未见数据。

本地 OTLP 接收端验证没有付费调用，也不是远程 Langfuse 平台。远程接收与查询已在 closeout/remote-tracing-v1 中另行验证；网页面板辅助检查待登录。离线完整对话和结构化输出仅针对合成评测案例，不进入默认线上日志。

容器 CI 使用确定性聊天和向量，PostgreSQL/Redis/Qdrant 为实际基础设施。Streamlit 为镜像内 AppTest 与真实服务交互，未测试浏览器 WebSocket；默认云端栈验证了启动、认证和索引健康，未执行真实模型问答。本地正式知识库付费导入及 Hybrid 未运行。

复现方法见 [部署说明](../../docs/deployment.md) 与 [阶段报告](../../docs/phase9_deployment_observability.md)。每次验证使用新目录，不能覆盖原记录。

费用口径补充：前两轮复测沿用了客户端默认传输重试，回调记录不能证明物理 HTTP 尝试总数；manifest 中 physical_calls 是历史字段名，本阶段只按逻辑调用解释。0.223848 元仅按有返回 usage 估价，未核对供应商账单。后续脚本已关闭 SDK 自动重试并限制输出，不为修正计数口径再次付费刷跑。

收尾最新入口：[closeout/README.md](closeout/README.md)。旧记录保留原实验状态；最终范围由收尾 validation 与对应代码提交的 GitHub Actions 结果共同确定。
