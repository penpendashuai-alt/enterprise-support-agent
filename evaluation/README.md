# Phase 4 评测资产

本目录记录 Synthetic enterprise support dataset / public technical documentation 的 Dense 基线。真实评测结果来自 DashScope Embedding、Qdrant Cloud 和单独的聊天模型验收；本地假向量只用于程序行为测试。

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
