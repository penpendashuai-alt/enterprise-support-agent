# Agent 质量与性能验证（2026-10）

2026-10-22 至 2026-10-24，针对草稿事实抽取与设备查询路由完成两轮开发对照、新留出评测、性能确认和真实浏览器验证。本目录保留数据集、冻结协议、逐次执行、语义审阅、成本账本与离线复算结果。

## 改了什么

本轮产品变更只涉及两个文件，候选 v2 在留出测试前冻结，之后未据留出题调参：

- `src/agents/ticket_flow.py`：针对“我个人”被误判信息缺失、描述与影响范围混写，约束主体表达、动作状态、字段分工及后续更正。提示词仍不保证所有输出完全遵循。
- `src/agents/support_agent.py`：保留原始查询与错误码；明确设备编号且没有故障描述的只读设备查询可以直接使用工具，实际故障排查仍要求知识证据。

没有改审批、数据库约束、模型或检索阈值来换分。新增统一实际 HTTP 请求账本、配对运行、分层评分、语义审阅及可复算性能工具。失败、重试、基础设施中断均有记录。具体原因、原输出与反例见 [两个改进案例](case-studies.md)。

## 主要结果

| 验证层次 | 结果 | 解释范围 |
| --- | --- | --- |
| 草稿留出 | 预期生成的10题，生成且忠实完成4/10→9/10 | 5家族各2题；已生成忠实度4/4、9/9；不能写准确率40%→90% |
| Agent留出 | 完整成功27/40→32/40；原协议25/40→30/40 | 40对均尝试；候选含1项数据库中断，不删除失败 |
| 检索留出 | 必要证据完整覆盖38/45；无答案返回证据1/15 | 新120查询中60题留出；不是回答准确率，检索组件未改 |
| 检索缓存 | off/cold/warm各40次：均值272/223/47ms | 20查询各2重复；warm实际36hit/4miss；36hit无Embedding |
| Agent缓存 | off/warm各100次，全部响应均值3.903/3.696s | 20查询各5重复，语义成功78/100与80/100；不写稳定P95提升 |
| 服务负载 | 1000请求；并发2成功500/500，并发20成功168/500、503拒绝332次 | 本地单worker、确定性模型与向量，所有批次资源归零 |
| 软件与容器 | 408 passed、4 skipped；13项容器检查通过 | 仓库整体含上游测试；容器使用确定性模型和单块fixture |
| 最终浏览器 | 真实引用、无答案/不可用区分、澄清、编辑、重启恢复、批准与防重已核验 | 真实模型与正式72块；只读工具模拟，工单写入隔离演示库 |

Agent配对为均成功25、仅候选7、仅基线2、均失败6。26家族簇的差值95% bootstrap区间约[-2.6,27.5]个百分点，包含0；不宣称整体显著或普遍提升。候选知识题7/12→6/12有退化，多轮8/8→7/8含基础设施中断。主成果限定为草稿完成改善。

所有语义审阅由同一开发助手完成并绑定原始记录hash，非独立人工评审。任务按家族划分，但仍围绕36篇演示资料（34篇合成、2篇公开摘要），不能外推真实企业分布。

## 失败与运行限制

- 历史6项回归结构均通过，完整语义4/6；两个P1相关回答仍缺定义。注入回归没有召回攻击块，不能据此声称已经验证摄入攻击后的防御。
- 最终浏览器的MFA回答包含“避免更多失败或锁定”这一无直接证据的附加解释；另一次默认P3问答有完整对应证据。演示完成不代表全部回答无幻觉。
- 云端Qdrant连接失败后，使用原冻结快照重建本地正式索引；不把新本地时延与旧云端结果对比。Docker停机、磁盘不足和数据库中断均保留。用户迁移Docker后恢复原容器，没有重置账本。
- Docker Hub认证超时，容器检查使用本机已有CI基础镜像；确认依赖锁摘要一致后覆盖当前源码。此路径及检查见 [容器记录](demo/container-regression/checks.json)。
- 当前仍限定单worker、共享Bearer及客户端声明身份；没有完整登录、多租户权限或生产SLA。本轮不重验远程Langfuse、Hybrid部署或语音功能。

## 费用与证据入口

最终保守估算 **16.4324485元 / 授权60元**：1000次聊天、512次Embedding物理请求，均已结算，无pending/unknown；含失败尝试、索引导入和预热。按冻结高峰输入/输出单价记账，没有扣除缓存折扣或免费额度，不是供应商账单。分阶段金额与合同hash见 [费用汇总](calls/final-summary.json)；逐请求见 [账本导出](calls/attempts.json)。

- [环境与阶段状态](manifest.json)、[冻结协议](protocol.md)、[对称评分修订](protocol-amendments.md)。
- [数据集](datasets/)、[基线](baseline/)、[两轮开发实验](development/)、[新留出与历史回归](heldout/)、[语义审阅](reviews/)。
- [质量复算](derived/heldout-corrected-protocol/quality.json)、[检索/草稿/负载复算](derived/supporting-evidence.json)、[性能结果](performance/results.md)。
- [真实浏览器与数据库断言](demo/README.md)、[软件检查](derived/software-checks.json)、[历史文件完整性](derived/historical-integrity.json)。
- [工程能力与证据映射](claim-evidence.md)、[改进案例与反例](case-studies.md)。

## 无付费复算

从仓库根目录执行，PowerShell中可将 `python` 换成 `.\venv\Scripts\python.exe`。以下只读原始记录并更新派生汇总，不调用模型，不重新运行测试集：

```powershell
$campaign = 'evaluation/resume-improvement/20261022-r1'
python scripts/summarize_resume_campaign.py --split heldout --baseline "$campaign/heldout/baseline" --candidate "$campaign/heldout/candidate" --baseline-reviews "$campaign/reviews/baseline-heldout.json" --candidate-reviews "$campaign/reviews/candidate-heldout.json" --apply-protocol-amendments --output "$campaign/derived/heldout-corrected-protocol"
python scripts/derive_resume_evidence.py
python scripts/summarize_resume_performance.py agent --input "$campaign/performance/agent-confirm" --reviews "$campaign/reviews/performance-confirm.json" --output "$campaign/derived/performance-confirm.json"
python scripts/summarize_resume_performance.py cache --input "$campaign/performance/retrieval-cache-recovered/report.json" --output "$campaign/derived/retrieval-cache.json"
```

原始记录不能用复跑覆盖。后续改进应另建实验编号；已查看的本轮heldout只能作为回归集。

## 公开记录说明

2026-10-29 整理公开展示入口。冻结协议、原始模型输出、失败记录与评测分母保持不变；历史协议提及的个人工作日志 `progress.md` 仅保留本地，个人材料不属于复算输入。软件检查摘要中的本机绝对路径替换为仓库相对路径，原日志 hash 保留。目录与脚本中的 `resume` 名称保留以兼容历史来源路径。
