# 项目导览：从知识问答到可恢复工单

Enterprise Support Agent 面向企业 IT 支持：用户先查询制度或排查问题，必要时生成工单草稿，编辑并确认后写入业务库。项目将模型生成与确定性业务规则分开，让引用、批准、持久化和故障恢复都有可检查的实现与证据。

## 推荐阅读路径

1. 看 [真实浏览器演示](../evaluation/resume-improvement/20261022-r1/demo/README.md)，理解从澄清到批准的完整流程。
2. 看 [实际 LangGraph 图](architecture.md#实际图)，再进入 [support_agent.py](../src/agents/support_agent.py) 和 [ticket_flow.py](../src/agents/ticket_flow.py)。
3. 看 [两个改进案例](../evaluation/resume-improvement/20261022-r1/case-studies.md)，对照原输出、修改原因、候选结果与反例。
4. 用 [离线复算](../evaluation/resume-improvement/20261022-r1/README.md#无付费复算) 核查数字，或按 [免模型密钥演示](demo.md) 在本机运行。

## 相对上游增加了什么

| 层次 | 上游提供 | 本 Fork 的新增实现 |
| --- | --- | --- |
| 服务与界面 | FastAPI、AgentClient、Streamlit、SSE、多模型适配 | 企业支持入口、来源展示、可编辑审批表单、会话恢复与显式偏好 |
| Agent | LangGraph 服务骨架与通用示例 | 结构化 Router、澄清、独立检索、按意图限制只读工具、版本化人工审批 |
| 知识库 | 通用 RAG 示例 | Qdrant 文档快照与索引契约、必要证据评测、ES/RRF/重排可选对照、精确缓存 |
| 业务持久化 | Checkpoint 集成基础 | PostgreSQL 工单唯一约束、会话归属、批准指纹、提交后故障核查与恢复 |
| 工程验证 | 基础测试与 CI 模板 | 配对任务评测、语义审阅、物理请求账本、容器业务与恢复验收、追踪数据过滤 |

上游为 [JoshuaC215/agent-service-toolkit](https://github.com/JoshuaC215/agent-service-toolkit)，保留 MIT 版权与基础实现。新增代码使用 AI 辅助开发。

## 一条完整业务链

以“VPN 从下午两点起连接失败，尚未尝试重启”为例：

1. Router 判断为报障请求；影响范围不明确时先澄清，不创建工单。
2. 抽取草稿，保留“尚未尝试”的事实状态。现象、时间和操作状态进入 description，影响对象进入 impact。
3. 用户把影响范围改为“三人小组”并保存，草稿升为 v2。此时业务库仍无工单，v1 批准请求被拒绝。
4. 服务重启后从 PostgreSQL 恢复同一会话和待审批 v2；用户确认后创建 DEMO 工单。
5. 重放同一批准时，后端按原请求和归属核查，返回已有工单；对应记录数保持为 1。

这条链路已经通过真实模型与浏览器验证，[数据库断言](../evaluation/resume-improvement/20261022-r1/demo/checks.json) 与 [截图](../evaluation/resume-improvement/20261022-r1/demo/browser/ticket-completed-recovered.png) 可对应查看。服务和设备查询工具是固定模拟，工单写入实际隔离数据库。

## 三个关键设计

### 批准必须绑定用户实际看到的草稿

模型只负责提取信息，写入由显式批准触发。草稿 ID、版本与内容指纹共同约束批准对象；修改草稿后旧批准不能继续使用。用户在聊天中输入“确认”也不能绕过结构化审批。

[审批状态机](../src/agents/ticket_flow.py) · [输入模型](../src/tickets/models.py) · [接口与恢复测试](../tests/service/test_ticket_approval.py)。

### 业务库与图检查点之间的失败需要核查

业务提交与 Checkpoint 不在一个原子事务里。若工单已入库但图保存失败，重试应先查询原请求；版本、归属与指纹一致则返回已有结果，查询本身失败则保留 unknown 状态。唯一约束防止同一请求重复创建，不能据此承诺所有情形的端到端 exactly-once。

[创建与核查服务](../src/tickets/service.py) · [PostgreSQL 仓库](../src/support_storage/postgres.py) · [故障恢复取舍](design_decisions.md)。

### 检索命中、回答有据和任务成功分别验证

检索候选还需经过门槛、Top K 与上下文预算才能成为证据；引用编号合法也不等于每个生成主张正确。因此评测分别检查必要条款覆盖、回答/草稿语义和完整任务。缓存键包含查询、配置及索引身份，证据不足结果不进入正向缓存。

[证据选择](../src/rag/retriever.py) · [引用校验](../src/rag/answers.py) · [缓存实现](../src/rag/cache.py) · [指标解释](evaluation.md)。

## 改进与验证

| 问题 | 修改与验证 | 结果 |
| --- | --- | --- |
| “我个人”被误判为信息不足，范围与描述混写 | 约束事实状态、原话范围与字段职责；冻结候选后运行新留出任务 | 10 个预期生成草稿的留出任务，生成且忠实完成 4/10 → 9/10 |
| 纯设备信息查询被静态检索阻断 | 明确设备编号且没有故障描述时允许直接查只读工具；故障/文档请求仍要求证据 | 新增回归覆盖三种路径，模型对照保留在原始记录 |
| 重复检索产生外部调用与等待 | Redis 精确缓存；关闭、冷缓存、预热各 40 次，记录实际命中 | 平均组件耗时 272 / 223 / 47 ms，36 次实际命中无 Embedding 调用 |

[案例中的原始输出与反例](../evaluation/resume-improvement/20261022-r1/case-studies.md) · [完整方法与分母](../evaluation/resume-improvement/20261022-r1/README.md)。整体任务提升区间包含 0，知识退化与基础设施失败保留；缓存组件效果不直接等于 Agent 端到端效果。

## 源码地图

| 模块 | 入口 |
| --- | --- |
| 业务图、路由、工具限制 | [support_agent.py](../src/agents/support_agent.py)、[support_tools.py](../src/agents/support_tools.py) |
| 草稿、批准、写入核查 | [ticket_flow.py](../src/agents/ticket_flow.py)、[tickets/service.py](../src/tickets/service.py) |
| 持久化、归属、显式偏好 | [support_storage](../src/support_storage/)、[memory/postgres.py](../src/memory/postgres.py) |
| 索引、检索、缓存、引用 | [rag](../src/rag/)、[RAG 实验](rag_experiments.md) |
| 并发、取消、追踪 | [execution](../src/execution/)、[容器验收工具](../scripts/verify_phase9_containers.py) |
| 配对运行、评分、审阅、成本 | [resume_support](../scripts/resume_support/)、[评测测试](../tests/evaluation/) |

当前是单 worker 的工程演示系统，使用合成资料与公开技术摘要；共享 Bearer 和客户端声明身份尚不构成完整多租户权限。适用范围、非独立审阅和已知问题见 [报告](../evaluation/resume-improvement/20261022-r1/README.md#失败与运行限制)。
