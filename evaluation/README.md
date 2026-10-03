# 评测与验收资产

本目录保留 Synthetic enterprise support dataset / public technical documentation 的 Phase 4 Dense 基线和 Phase 5 对照实验。真实评测使用 DashScope Embedding、Qdrant Cloud、Elasticsearch、Reranker 和聊天模型；Mock 只用于程序行为测试。

## Phase 7

Phase 7 的 Redis 缓存、并发控制、真实网络取消和性能对照见 [Phase 7 记录索引](phase7/README.md) 与 [开发文档](../docs/phase7_async_redis_performance.md)。四组最终服务矩阵、真实检索和小规模真实模型分别记录，拒绝不计为成功吞吐；没有证明真实 Agent 整体提速。

## Phase 6

- [开发与复现](../docs/phase6_memory_postgres.md)：PostgreSQL、会话归属、显式偏好及旧环境边界。
- `baselines/phase5/manifest.json`：Phase 5 Git 基线和源码/配置/评测摘要。
- `phase6/storage-final.json`：真实 PostgreSQL 多连接、导入、服务重启、提交后退出与数据库停机恢复；确定性模型，无付费调用。
- `phase6/live.json`、`live-preferences-retry.json`、[回答审阅](phase6/answer-review.md)：真实模型首轮及偏好修复复测，累计保守估价 0.348176 元；包含失败记录。
- `phase6/service-startup.json`：实际服务入口连接 PostgreSQL 的启动检查。
- `phase6/legacy-preflight.json`：现有本地 SQLite 的只读格式检查；没有认领或导入未知归属。
- `phase6/validation.json`、`integrity.json`：自动化检查、冻结边界与隐私检查；不代表生产授权或多 worker 保证。

## Phase 5

- [开发与结果说明](../docs/phase5_hybrid_rag.md)：安装、配置、指标、取舍及已知质量问题。
- `baselines/phase4/`：Phase 5 开始前的可恢复源码 ZIP、dirty 状态和逐文件哈希。
- `datasets/hybrid_v1.json`、`snapshot_v2.json`：128 题（68 dev / 60 heldout），36 文档 / 72 个共享分块；同源改写分组划分。
- `datasets/generation_v1.json`：批量实验前选定的 9 道生成审阅题，七组共 63 个回答。
- `results/phase5/frozen-v1.json`：dev 校准、阈值网格、数据/检索源码指纹与留出运行前的默认选择。
- `dev-v1.json`、`heldout-v1.json` 及同名 `.jsonl.gz`：896 次真实检索的原始候选、分数、排名、选取原因、用量和延迟。dev 原始门槛为 0，正式 dev 指标需离线按冻结门槛重放。
- `results/phase5/comparison.json`：离线重放 dev 与首次 heldout 的七组指标、候选数量和必要块流失原因；未覆盖原始数据。
- `generation-v1.json`、`generation-review-blinded.json`、`generation-review.json`、`generation-summary.json`：完整最终回答、隐藏组名审阅文件、逐条判断及汇总。同一编码助手审阅，不是独立人工标注或盲评；首次未保存校验前模型文本。
- `agent-v1.json`、`agent-extended-v1.json`、`agent-answer-review.md`：真实 Router、invoke/SSE、多轮状态与语义审阅。11/11 结构检查通过，但 P1 短问不完整、冲突场景有无依据扩展。
- `business-regression.json`：6/6 工单审批真实模型回归，临时 SQLite。
- `ingestion-v2*.json`、`integration-probe.json`、`rerank-probe.json`、`dependency-faults.json`、`es-restart.json`：真实索引、分词、Reranker、故障和重启验证。
- `environment.json`、`cost-summary.json`：环境、官方单价和完整成功 API 用量估价；不是供应商账单。

运行 `python scripts/summarize_phase5.py` 可不调用外部 API 重建对照及费用汇总。主要实验文档 Recall@5 均已饱和；必要条款覆盖、回答有据性和无答案边界必须分开看。数据集由编码助手编写和逐源核对，独立人工标注复核尚未完成，不能用于声称生产泛化或独立基准领先。

## Phase 4 原始记录

- `datasets/dense_v1.json`：48 条人工构造并标注文档 ID、章节的查询，dev/heldout 各 24 条，各含 20 条有答案与 4 条无答案问题。
- `results/phase4/cloud-probe.json`：临时 collection 的真实读写、维度校验及清理。
- `results/phase4/ingestion-v1*.json`：首次导入与重复复用，20 篇文档、40 块。
- `results/phase4/dense-dev-v1.json`：开发集候选分数与阈值选择；原始运行阈值 0.45，离线校准选择 0.65。
- `results/phase4/dense-heldout-v1.json`：冻结 0.65 后首次留出集结果；未用其修改阈值。
- `results/phase4/agent-live-v1.json`：12 个真实模型接口/流程场景。
- `results/phase4/agent-robustness-v1.json`：5 个补充场景，冲突和恶意文档使用明确标记的受控证据，其余使用真实检索。
- `results/phase4/agent-grounding-recheck-v1.json`：人工发现一次越出证据的概率推断后，加强提示词并复测 2 个场景。保留前一轮原始回答。
- `results/phase4/answer-review.md`：逐场景的证据支持性审阅，不把合法编号等同于事实正确。
- `results/phase4/cost-summary.json`：各记录的实际 token 汇总与估价，非供应商账单。

候选指标先取 5 个 chunk，再映射 doc_id、稳定去重、截断到最多 5 个文档，不额外补取。Recall@5 是每题命中相关文档数除以标注相关文档数的均值；MRR@5 是首个相关文档名次倒数的均值。当前每题只有一个相关文档，因此该 Recall 等同于命中率。无答案题不进入这两个分母。

`accepted_gold_document_coverage` 另行衡量阈值和上下文预算过滤之后仍保留正确文档的题目比例。留出集候选 Recall@5 / MRR@5 都为 1.0，但过滤后覆盖仅为 0.75；4 个无答案题中 1 个仍返回候选证据。这是已知基线不足，Phase 5 扩大语料与评测再比较检索方案，不据此声称生产效果或提升幅度。

开发和验收问题围绕相同 20 篇小规模语料编写，独立的是问题表达，并非未见文档或真实用户分布。Dense 数据集未把省略上下文的续问直接送入检索器；多轮改写单独由 L4-04、L4-12 验证。未经本阶段实验验证，不宣称分块参数最优。

复现命令见 [Phase 4 开发文档](../docs/phase4_dense_rag.md)。实测报告保留运行时版本、数据集指纹、查询、分数、引用、延迟和接口 token 量，不包含密钥、个人云端地址或本地私有文件。每次付费验收使用新文件名，保留历史用量。
