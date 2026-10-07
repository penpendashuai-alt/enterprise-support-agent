# 检索实验与默认选择

最终数值入口为 [Benchmark](../benchmarks/results.md)，完整技术过程保留在 [Phase 4](phase4_dense_rag.md) 和 [Phase 5](phase5_hybrid_rag.md)。Phase 10 不重新调参、不调用模型，仅整理冻结记录。

## 数据与契约

v2 是 36 篇文档、72 个块：34 篇合成企业支持资料和 2 篇公开技术文档的原创摘要。快照固定 800/100 字符分块、DashScope text-embedding-v3 / 1024 维。Dense 和 ES 读取同一 [snapshot](../evaluation/datasets/snapshot_v2.json)，核对 chunk_id/content_hash/数量/ready 状态。资料不代表真实企业制度。

[hybrid_v1](../evaluation/datasets/hybrid_v1.json) 共 128 题，dev 68（60 有答案、8 无答案），heldout 60（52 有答案、8 无答案）。问题由开发助手构造，金标准用文档版本、章节、文本片段定位必要条款；存在可替代的完整证据集合。heldout 未参与阈值选择，但与 dev 共用小规模语料，不是未知领域泛化测试。

## 七组对照

| 组 | 候选/选择 |
| --- | --- |
| dense_original | 旧 Top5 / 0.65，保留 Phase 4 选择行为 |
| dense20 | 新路径 20 候选，dense 门槛 0.65，当前示例默认 |
| dense40 | 40 候选，dense 门槛 0.65 |
| bm25 | ES SmartCN 与 technical_terms，门槛 8 |
| hybrid | Dense+BM25，RRF；冻结 RRF 门槛 0 |
| hybrid_rerank | 混合候选后 gte-rerank-v2，门槛 0.3 |
| dense_rerank | Dense40 后重排，门槛 0.3 |

这些分数没有统一尺度。不得将 dense 0.65 直接用于 BM25/RRF/Reranker。实验禁用降级混淆组别；故障验证另列。选定参数来自 [frozen-v1](../evaluation/results/phase5/frozen-v1.json)。dev 原始候选按冻结门槛离线重选，原 journal 不改写；heldout 使用已冻结配置。

## 指标与结论

文档 Recall/MRR 先取 5 个 chunk，再去重到文档，不额外补够 5 个文档；必要证据覆盖按条款 locator 计算。最终证据完整覆盖要求至少一个 gold alternative 全部保留；无答案指标统计返回任何证据，并不等于最终回答编造。生成有据性来自另外每组 9 题的非独立审阅，不能借用检索题数作为分母。

heldout 的 Dense20 必要证据完整覆盖为 44/52，无答案返回证据 3/8；旧 Dense5 在这两项相同。Hybrid 完整覆盖 51/52，但无答案返回证据 8/8。Hybrid+Reranker 为 36/52、1/8。更强过滤可能减少无答案误接收，同时丢必要条款；不是叠更多组件就更好。

Dense20 是 dev 上综合默认选择，减少额外组件依赖；不宣称质量优于旧 Dense5。完整候选与筛选损失事件见 [comparison.json](../evaluation/results/phase5/comparison.json)，损失事件数量不是失败问题数。

## 失败与仍需解决的问题

Phase 8 的 P1 问题中可能找到了相关文档但缺少关键块；合法引用编号仍可能伴随无据扩写；工单抽取还可能把未来请求改写为已尝试。前者属于检索证据完整性，后两者是生成/事实状态问题，不能都归为“召回失败”。定位入口为 [Phase 8 报告](phase8_agent_evaluation.md) 的失败记录与 [Agent 评分](evaluation.md)。

Phase 7 精确缓存避免命中查询的外部 Embedding/搜索调用，但模型仍需路由和生成；4 个真实 Agent 样本/组没有证明端到端提速。不同实验环境的组件 P95 不合并；历史成本仅按返回 usage 估价。

Phase 5 实际 ES 验证为本机 Windows 服务，当前 Docker 下 Hybrid 组合未重新运行。本地正式知识库付费导入仍未执行；Phase 9/10 的 Qdrant 演示只有显式标注的 CI fixture，不等于 72 块正式索引质量验证。
