# Phase 8 记录索引

实现与复现见 [开发文档](../../docs/phase8_agent_evaluation.md)，评分见 [rubric](rubric.md)。实验与规定审阅已完成，保留语义失败，未证明整体质量提升。最终派生结果以 **`final-v2/`** 为准，包含段落引用归属复核与补充草稿审计；早期摘要保留为历史记录。

| 路径 | 含义 |
| --- | --- |
| `design-manifest.json` | 36/24 家族分组、预算、预选缓存子集与执行顺序 |
| `candidate-freeze.json` | 留出前的源码、评测器、参数与家族轮换顺序 |
| `runs/pilot-baseline/` | 两条联通检查，不计正式样本数 |
| `runs/dev-baseline/` | 首次基线，31 项已尝试，第 31 项评测器错误，5 项未执行 |
| `runs/dev-baseline-v2/` | 修复记录写入后的完整基线新实验 |
| `runs/dev-candidate/` | 候选开发集，原始回答和审阅均保留 |
| `dev-baseline-interrupted-summary.json` | 首次中断结果，以全部 36 个预定任务为分母 |
| `dev-paired.json` | 修复后基线与候选开发集配对，未替换首次失败 |
| `heldout/` | 冻结后按家族交错运行的两方案分片，campaign 保存顺序 |
| `components/raw-query/` | 绕过 Router 的原始问题检索 |
| `components/fixed-baseline/`、`fixed-candidate/` | 同一已记录证据的生成对照，无业务工具执行 |
| `components/reviews.json` | 固定证据失败归因，不能计作端到端成功率 |
| `components/controlled-evidence/` | 从已记录候选中追加恶意块，明确为受控输入 |
| `calls/`、`pricing.json` | 唯一物理调用账本及保守估价；未知用量不计零 |
| `final-v2/` | 最终各层汇总、开发/留出/回归配对与成本，可纯离线重建 |
| `draft-audit.json` | 冻结评分遗漏的草稿事实忠实度补审；明确为留出后分析 |
| `citation-scope-review.json` | 人工补全段落引用映射；此前审阅版本仍保留 |
| `runs/regression-*/` | 六个历史/边界回归任务，两组均为 5/6，P1 定义仍遗漏 |
| `runs/cache-*/` | 预选缓存子集：流程 3/3，草稿语义补审后 2/3 |
| `components/injection-*/` | 受控恶意块输入后的生成记录，无业务工具绑定 |
| `components/citation-validation.json` | 确定性无效编号输入，保留替换前后文本，无付费模型调用 |

每个语义审阅绑定原始记录摘要。同一开发助手参与场景设计、实现和评分，不是独立人工盲评。`reviews.v2.json` 是开发集审阅复核版，补记通用概念错误；初次审阅仍保留。后续摘要以复核版为准，不能择优选用。

运行 `python scripts/rebuild_phase8.py --output <新目录>` 可重建最终报告，不调用 API。主留出冻结契约两组均为 20/24；草稿补审后为基线 19/24、候选 18/24，不能仅引用较好的一项。全阶段保守估价 6.147443 元，515 次物理调用，未知用量为 0。
