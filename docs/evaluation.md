# 如何解释本项目的验证结果

本项目没有单一“准确率”。[Benchmark](../benchmarks/results.md) 和 [来源清单](../benchmarks/sources.json) 分别给出样本、分母、模式、环境与原始路径；缺失信息写 unknown。

| 层次 | 回答的问题 | 证据 | 不能证明 |
| --- | --- | --- | --- |
| 软件单元/集成测试 | 代码契约、异常路径、归属、幂等是否符合预期 | [tests](../tests)、[CI](../.github/workflows/test.yml) | 真实模型总体质量；总测试数不是本人新增数 |
| 检索组件评测 | 是否找全并保留必要证据 | [Phase 5](rag_experiments.md)、retrieval.csv | 最终生成有据、任务完成 |
| 生成语义审阅 | 每个主张是否受证据支持、草稿是否忠于事实 | [Phase 8 final-v2](../evaluation/phase8/final-v2)、[Phase 9](../evaluation/phase9/README.md) | 独立盲评或生产泛化 |
| Agent 多步任务 | 意图、工具、审批、核查、回答能否共同满足任务契约 | [agent_v1](../evaluation/datasets/agent_v1.json)、[评分器](../scripts/eval_support/scoring.py) | 任意用户分布或无限长会话正确性 |
| 工程性能 | 有限批次的成功/拒绝、延迟、容量与外部调用 | [Phase 7](phase7_async_redis_performance.md)、latency.csv | 真实大模型吞吐 SLA |
| 部署与浏览器 | 真实存储、重启、恢复、追踪和 UI 协议能否工作 | [Phase 9 CI](../evaluation/phase9/closeout/github-actions.json)、[Phase 10 演示](demo.md) | 完整登录权限、多 worker 或真实企业接入 |

## 任务评测设计

Phase 8 的 60 个主任务按家族划分 dev 36 / heldout 24，另有历史失败回归；轮次和任务不是同一分母。三层证据是结构检查、离线组件检查与非独立开发助手语义审阅。真实 HTTP/PostgreSQL 流程包括审批恢复；模型/服务故障采用标注的注入方式。保存原始记录、审阅、调用账本、源码与提示词指纹，不把重复实验挑选成最优一次。

冻结任务契约 heldout 基线/候选均 20/24。事后草稿事实补审为 19/24 与 18/24，属于新增审阅口径，不追溯成预注册指标。候选未证明整体质量提升。结构通过、引用支持与任务成功各自有分母；未审阅不能自动算成功。

Phase 9 两轮均 12 案例、11 草稿、1 澄清；11/11 已生成草稿忠实，不是 12/12 完成。第二轮是工具返回记录器修复，原提示词相同，保留首轮。历史 0.223848 元是有返回 usage 的估价，旧 physical_calls 字段不证明物理 HTTP 尝试总数。

## 离线复算

```sh
python scripts/summarize_phase10.py
python scripts/summarize_phase10.py --check
python scripts/summarize_phase10.py --output .cache/phase10-rebuild
uv run pytest tests/evaluation/test_phase10_summary.py
```

入口仅依赖标准库，不读取 .env，不加载 Agent/模型配置。输出严格位于派生目录；禁止写入 evaluation 原始记录目录。输入缺失、未知格式、分母不一致或原 journal 摘要错误即失败。sources.json 的文本摘要统一 CRLF→LF，二进制 gzip 按原字节校验，保证 Windows/Linux 复算一致。缺少精确运行提交时标 unknown，不用当前 HEAD 冒充历史运行版本。

Phase 5 复用已冻结的 comparison/generation-summary，并记录原 journal、配置、数据集与审阅摘要；Phase 7 从逐请求记录重算成功吞吐和成功样本分位数；Phase 8 复用 final-v2 的原评分和补审。Phase 10 测试独立核对代表性 44/52、20/24、19/24、18/24 和 11/11/12 分母，并验证拒绝、未知值、错误输入。

## 统计约定与限制

- Phase 5 p50 为中位数，p95 为 nearest-rank；Phase 7 使用 floor((n-1)×p)。不对各组 P95 取平均，不混用组件与整次请求时长。
- 429 和错误不进入成功吞吐，缓存观测数单独列出；没有观测到 usage 时保持 unknown，不填零。
- 本阶段演示回答来自确定性替代，只有传输、图、存储与 UI 是实际运行；真实 LLM 新演示未执行。
- 自建合成数据、公开文档摘要、小样本、同语料划分和非独立审阅限制结论。没有生产流量、用户满意度、工单节省比例或高可用指标。

最终阶段完成状态见 [Phase 10 交付记录](../evaluation/phase10/README.md)。个人简历与用户自测保留本地，不能由自动测试代替。
