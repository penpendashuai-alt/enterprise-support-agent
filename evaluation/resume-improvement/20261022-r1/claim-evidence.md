# 工程能力与证据映射（2026-10）

| 表述 | 原始/派生依据 | 层次、分母与限制 |
| --- | --- | --- |
| 构建 128 条查询并比较 7 组检索配置 | evaluation/datasets/hybrid_v1.json；benchmarks/results.md；docs/rag_experiments.md | 历史离线：68 dev / 60 heldout，非当前新增成绩 |
| Dense20 必要证据完整覆盖 44/52；无答案返回证据 3/8 | benchmarks/results.md 的检索留出表与 sources.json | 52 可答、8 无答案；不是答案正确率，也未证明优于 Dense5 |
| 构建 60 个合成 Agent 任务，拆分结构与语义验证 | evaluation/datasets/agent_v1.json；docs/phase8_agent_evaluation.md | 36 dev / 24 heldout；补审后 19/24、18/24，不写质量提升 |
| 本地确定性矩阵累计 10,000 请求，验证服务限流、缓存和资源保护 | docs/phase7_async_redis_performance.md；benchmarks/results.md | 累计请求，不是并发数；含拒绝，非真实 LLM 吞吐 |
| 检索热缓存均值 0.522s（n=9），无缓存 1.267s（n=6） | 历史 Phase7 real retrieval 原始记录与 benchmarks/results.md | 3 查询两轮；冷 miss 1.568s（n=3），非 Agent P95 |
| 13 组容器业务/恢复和 6 组 Redis 检查 | evaluation/phase9；evaluation/phase10/README.md | 确定性模型 + 真实基础设施；不是本轮已重跑 |
| 本次80个合成Agent任务，40 dev / 40 heldout | [dataset](datasets/agent.json)、[配对计划](heldout/paired-plan.json)、[执行](heldout/execution.json) | 完整任务计数，不把轮次或AB方案当更多独立场景；留出26家族簇 |
| 10个预期生成草稿的留出任务，忠实完成4/10→9/10 | [审阅](reviews/)、[草稿复算](derived/supporting-evidence.json)、[案例](case-studies.md) | 5家族各2任务；生成后忠实度4/4、9/9，不是事实准确率40%→90%；候选仍1题未生成 |
| 约束动作状态、影响范围原话和字段分工 | src/agents/ticket_flow.py；[原始a-dev-14](baseline/agent-dev/a-dev-14.json)、[候选](development/agent-dev-v2-recovered/a-dev-14.json)；a-dev-33编辑链 | 提示词不保证未来完全遵循，仍有description重复范围；无新增评审模型 |
| 收窄设备信息查询的检索前置条件 | src/agents/support_agent.py；[a-dev-27](baseline/agent-dev/a-dev-27.json)、[候选](development/agent-dev-v2-recovered/a-dev-27.json)；tests/agents/test_support_agent.py | 明确设备且无故障才允许只读查询；实际故障仍检索，工具为模拟 |
| 新建120查询，必要证据完整覆盖38/45、无答案返回证据1/15 | [dataset](datasets/retrieval.json)、[原始检索](heldout/retrieval/)、[复算](derived/supporting-evidence.json) | 60 dev / 60 heldout；同36篇演示资料72块；不是回答准确率或未知企业泛化 |
| 版本/指纹绑定批准、唯一约束防重、未知结果核查 | src/tickets/、src/support_storage/；[容器13项](demo/container-regression/checks.json)；heldout多轮记录 | 版本、内容指纹、幂等各有职责；图检查点和业务库不是一个原子事务 |
| 本轮5种确定性负载1000请求，资源归零 | [逐请求矩阵](performance/deterministic-balanced.json)、[复算](derived/supporting-evidence.json) | 并发2为500/500成功；并发20为168/500成功+332次503；桶初态40/20，两轮反序；非1000并发或真实LLM吞吐 |
| 本轮13项容器业务、重启、备份恢复检查 | [checks](demo/container-regression/checks.json)、[requests](demo/container-regression/request-summaries.json) | 确定性模型/单块fixture；本地锁文件一致镜像覆盖当前源码；不代替真实模型浏览器 |
| 20查询每条件40次，检索预热均值272ms→47ms，36次实际hit无Embedding | [组件原始](performance/retrieval-cache-recovered/report.json)、[复算](derived/retrieval-cache.json)、[性能说明](performance/results.md) | 关闭/冷/预热各40次；预热仍4miss，不缓存证据不足结果；120次证据一致含空证据；真实Embedding+本地正式72块，非Agent整体提速 |
| Agent缓存开关各100次确认 | [原始](performance/agent-confirm/)、[审阅](reviews/performance-confirm.json)、[复算](derived/performance-confirm.json) | 20查询各5重复，预热20次不计入；全部响应3.903/3.696s，语义成功78/100与80/100；不写稳定P95改善 |
| 当前软件回归408 passed / 4 skipped | [软件检查归档](derived/software-checks.json) | 整个仓库包含上游，非本人新增408项；不能代替模型质量 |
| 当前真实浏览器完成引用、编辑、重启恢复与批准防重 | [演示记录](demo/README.md)、[数据库断言](demo/checks.json) | 真实模型+正式72块；模拟只读工具；审批v2仅1张DEMO；MFA回答附加解释仍有无据内容 |

链接以本目录为基准，未链接的仓库路径以根目录为基准。旧记录保持只读。7组检索来自历史128查询，本轮120查询未重新比较7组；个人信息和简历仅本地保存。

整体主结果27/40→32/40，原协议25/40→30/40，均可复算；[评分修订](protocol-amendments.md) 对称接受批准状态不变且未写库的普通文字确认安全重显。配对均成功25、仅候选7、仅基线2、均失败6；家族簇bootstrap差值95%区间[-2.6,27.5]个百分点包含0，不能写成整体显著提升。

候选1项知识新增错误、1项数据库中断均保留在主分母，正常依赖39对仅为敏感性分析。本轮检索组件未改，不把不同dev与heldout题的43/45、38/45当作优化收益。评测由同一开发助手审阅，非独立人工盲评。
