# 设计决策与证据

每项选择都限定在单实例企业支持演示场景。上游已提供 LangGraph、FastAPI、SSE、AgentClient、Streamlit 和基础测试；本 Fork 的主要增量是企业支持业务、检索契约、审批持久化和分层验收。

| 问题 | 当前实现 | 备选与代价 | 代码 / 检查入口 | 限制 |
| --- | --- | --- | --- | --- |
| 自由工具循环难预测 | 显式 Router 与有限 State 字段，独立检索/草稿/审批分支 | 通用 ReAct 更灵活，但更难约束创建副作用 | [图](../src/agents/support_agent.py)、[路由测试](../tests/agents/test_support_model.py) | 分类和抽取仍会出错；结构正确不等于语义正确 |
| 模型可能调用不相关工具 | 按意图允许集合、严格参数模型、4 次单步上限和超时 | 仅靠 prompt 无执行端约束；全开放工具风险更高 | [工具](../src/agents/support_tools.py)、[图测试](../tests/service/test_support_service.py) | 服务/设备为固定模拟数据，没有真实企业权限 |
| 草稿被编辑后旧批准失效 | 草稿版本、内容指纹、显式 ApprovalInput；修改后重新确认 | 单一 yes/no 无法绑定用户实际看到的内容 | [流程](../src/agents/ticket_flow.py)、[审批测试](../tests/service/test_ticket_approval.py) | 人工仍需核对事实；提示词不能替代确认 |
| 重试创建可能重复写入 | 数据库唯一约束、按原草稿核查，unknown 禁止另建 | 只用进程锁不能跨重启；分布式事务成本超出范围 | [仓库](../src/support_storage/postgres.py)、[创建服务](../src/tickets/service.py)、[仓库测试](../tests/tickets/test_repository.py) | Checkpoint 与业务库非原子提交；语义不是全链路 exactly-once |
| 检索方案复杂度与证据覆盖冲突 | dev 选择 Dense20；Hybrid/Reranker 为可选 | BM25/Hybrid 补足条款但无答案误接收增加；重排也可能丢必要块 | [实验](rag_experiments.md)、[冻结配置](../evaluation/results/phase5/frozen-v1.json) | 未证明新默认优于 Dense5；同语料合成题 |
| 热查询反复访问供应商 | Redis 精确 query+配置+索引版本缓存，single-flight 合并 | 语义缓存可提高命中但可能把相似问题误当同题 | [缓存](../src/rag/cache.py)、[缓存测试](../tests/execution/test_cache_control.py) | 不缓存最终回答，不免除全部 LLM 调用 |
| 缓存与限流共用内存 | 64 MiB、noeviction、128 MiB 容器硬上限；缓存写失败返回已算证据，入口限流失败拒绝新问答 | LRU 会逐出速率状态；分实例隔离更好但增加运维范围 | [内存脚本](../scripts/verify_phase9_redis_memory.py)、[CI 证据](../evaluation/phase9/closeout/github-actions.json) | 共享压力会影响问答；64/128 不是通用容量比例 |
| 外部调用拥塞与排队 | asyncio、有界容量、等待/请求超时、取消清理 | 无界并发可能加剧限流与内存压力 | [控制](../src/execution/control.py)、[Phase 7](phase7_async_redis_performance.md) | 拒绝是保护行为，不计成功吞吐；未证明真实 Agent 提速 |
| 同会话并发修改状态 | 单 worker 会话锁，PostgreSQL 持久化 | 多 worker 需分布式协调，不能仅加副本 | [控制](../src/execution/control.py)、[部署](deployment.md) | 无高可用/多实例保证 |
| 追踪意外上传正文 | callback 屏蔽与最终 OTLP 白名单双层过滤，HMAC 关联身份/草稿 | 全量日志易排错但增加泄漏风险 | [追踪](../src/execution/observability.py)、[真实远端检查](../scripts/verify_phase9_business_tracing.py) | 元数据不可替代语义评测；关闭接收端后等待有界 |
| 质量指标容易被误读 | 结构、证据、非独立语义审阅、业务任务与基础设施分开 | 单一“准确率”方便展示但掩盖不同分母 | [评分](../scripts/eval_support/scoring.py)、[Benchmark](../benchmarks/results.md) | 没有独立真实用户基准；不能声称生产效果 |

## 保持失败可解释

HTTP 200 不一定代表模型完成任务：路由异常可返回澄清降级，日志与追踪记录 `router_unavailable`。Redis 不可写时新问答 503，审批/读取可有界回退；数据库不可用则核心不就绪。`/health/ready` 不付费探测模型，`/health/capabilities` 区分已配置和已验证可调用。

Phase 8 的候选提示词曾修复 dev 的局部问题，但 heldout 冻结契约均为 20/24，草稿补审后基线 19/24、候选 18/24。因此保留候选失败，不把后续部署稳定性包装成整体质量提升。Phase 9 草稿提示词补充事实时态，12 案例中每轮 11 个草稿、1 个澄清；已生成草稿忠实不等于所有请求完成。

## 可替换处与不承诺处

要接入真实工单平台，应在业务适配层实现同等幂等/核查语义和权限，而非允许模型任意写数据库。要扩成多租户，必须增加服务端身份与授权体系。要做生产 SLA，需要独立负载、容量与故障数据；当前有限批次分位数不能充当 SLA。以上均是后续设计方向，当前没有实现。
