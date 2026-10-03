# Phase 7 原始记录

实现说明与复现命令见 [Phase 7 开发文档](../../docs/phase7_async_redis_performance.md)。`personal/` 和 `.cache/` 继续只保留本地。

- `final/service-*-matrix*.json`：冻结代码后的完整四组矩阵，每组 2250 请求、30 批。
- `final/service-*-repeat.json`：反向模式顺序重跑 50 请求 / 并发 10 的五个工作负载，每组 250 请求。
- `summary.json`：由 `scripts/summarize_phase7.py` 重建的成功/失败分类、延迟、吞吐、命中与用量汇总，服务主结果只读取 `final/`。
- `environment.json`：本机版本、进程和连接数、交付源码字节摘要。每个最终服务报告另存实际启动时源码摘要，不用交付时摘要冒充运行时版本。
- `live-retrieval.json`：3 个固定查询、18 次真实检索；命中证据等价、本次调用量为零。
- `live-agent.json`：8 条交错真实 Agent 比较、1 条英文偏好、1 条已知 P1 问题；含 Router 查询、证据、回答与真实用量。没有证明 Agent 整体提速。
- `redis-verification.json`：真实 Redis TTL、坏值、索引状态检查、100 并发原子双桶及补充/过期。
- `network-verification-v2.json`：真实网络断连、资源归还、SSE 前后错误、已校验答案计时。
- `redis-outage-verification.json`：真实不可达 Redis 的新问答拒绝与读取回退。
- `storage-regression.json`：开启新控制后的完整 Phase 6 存储与恢复回归。
- `storage-redis-restart-v2.json`：专属 Redis 停机、恢复与重复批准、提交后崩溃恢复。

根目录 `service-*.json` 是开发期探索记录，部分与本地验证重叠，不作为最终性能结论。`service-baseline.json` 是最早的改造前基准；其 hot 未预热，unique 仅批内不同，不能混入新版工作负载比较。

保留了三个失败/中断记录：`network-verification.json`（修复前取消清理）、`storage-redis-restart.json`（Redis shutdown 的控制脚本误报）、`service-baseline-matrix.json`（旧源码 fixture 的兼容错误，0 批）。这些文件均不计为验收成功，随后使用新文件名完整重跑。

每个请求都有成功标志、HTTP 状态、失败 detail、时长和可用的查询/缓存信息。拒绝不计入成功吞吐，P50/P95 成功和失败分开；相邻批次的令牌桶状态延续，没有隐蔽重置配额。知识回答编号校验通过不代表全文语义正确；P1 的证据不足和 Phase 5 冲突回答的无依据扩展继续保留。

成本估计 0.7227385 元 / 授权上限 10 元。所有大规模服务负载使用确定性组件，不调用付费聊天模型。`final/` 的模型吞吐不能写成真实 LLM 吞吐。
