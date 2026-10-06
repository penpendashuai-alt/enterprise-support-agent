# Phase 9：部署、业务可观测性与自动化验收

状态（2026-10-06）：实现、离线测试、实际容器业务与恢复验收、本地追踪 HTTP 导出通过。收尾新增 Redis 内存压力与远程 Langfuse API 验收通过；GitHub Actions 正在准备，网页面板辅助检查待登录。最终结果以 closeout 记录为准。以下区分证据范围；不宣称生产上线。

## 基线与改造边界

基线为 Phase 8 的 `c1ed166c1abf4a5b6fad48057165fa3b0833f694`，保存了 Git archive 和 1267 个受版本管理文件的摘要，包括源码、锁文件、迁移、索引 snapshot、提示词、冻结配置和最终评测。ZIP 位于本地忽略目录 `.cache/phase9-baseline.zip`，摘要公开于 `evaluation/phase9/baseline.json`。个人文档及凭据不进入 Git 或镜像。

上游已提供 Dockerfile、Compose、CI 和 Langfuse callback；本阶段对齐企业支持业务。检索仍为 Dense20/v2、1024 维、800/100 分块契约、0.65 dense 门槛；没有重做 RAG 调优。Phase 8 原始实验和 `final-v2/` 保持不变。

## 草稿事实状态

定位到 `prepare_draft` 提示词只强调保留“已尝试操作”，没有明确请求、计划、否定和多轮更正。修改要求保留各自事实状态，禁止把未来请求补为已完成/已尝试。字段映射不添加状态，用户结构化编辑仍原样替换草稿、增加版本并重新审批。

真实模型使用既有 deepseek-v4-pro，通过真实 `prepare_draft` 入口验证 12 个合成案例，含请求、计划、执行失败、未执行、建议、条件计划及多轮纠正。首轮记录器未保存结构化工具返回，第二轮补齐后复测同一集合，提示词没有改变。两轮均生成 11 个草稿，1 个案例澄清影响范围；已生成草稿均逐句检查了时间、执行状态、否定、影响范围与优先级。缺草稿的案例不计入“已生成草稿忠实度”分母。

24 次有返回的逻辑聊天调用，估价 0.223848 元，返回结果缺失 usage 为 0；授权上限 20 元。估价沿用每百万输入/输出 9/27 元的保守费率，不是供应商账单。未调用付费 Embedding，也未部署在线 Judge。新增确定性测试验证已抽取事实经过映射、编辑和取消后不被改变；这些测试不能证明自然语言理解，语义证据来自另列的真实输出与非独立开发助手审阅。提示词不是事实保证，用户确认仍不可省略。

## 部署与配置

默认栈改为 PostgreSQL 17.11、Redis 7.0.15、一次性业务迁移、Service 和 Streamlit，沿用锁定 Python 依赖。默认一个 worker，只启用 Support Agent；上游 Agent 可显式启用并延迟导入。非 root 应用用户、回环端口、独立命名卷、显式共享密钥、关闭稳定模式热重载。

本地 Qdrant override 显式改 URL/collection，与云端隔离；CI 确定性向量具有不同契约，不允许写入正式 collection。本地正式导入复用冻结 v2 snapshot，需付费 opt-in 和上限；实际本阶段未执行付费导入。Hybrid override 为可选配置，未重跑 Phase 5 实验。旧 PostgreSQL 16/Phase 6 卷不迁移、不覆盖。

`verify_phase9_containers.py` 已在专属项目通过 13 组真实基础设施检查：迁移与错误 schema 拒绝、HTTP/SSE、Qdrant、审批/编辑/取消/重复批准、归属/偏好、四类容器重启、依赖停止恢复、独立数据库备份恢复、Streamlit 脚本交互以及运行日志过滤。证据为 `evaluation/phase9/containers-v4/`；退出后仅清理本次测试容器与卷。

PostgreSQL 备份恢复到同一专属实例的新数据库，10 张表的行数和内容摘要一致，涵盖工单、会话、Checkpoint 与偏好。Streamlit 使用镜像内 AppTest 驱动真实界面脚本并经 AgentClient/SSE 连接后端，验证 MFA 引用回答；未验证浏览器 WebSocket。CI 模型与向量为确定性替代，基础设施为实际容器，不将此结果作为真实模型质量指标。

默认 Compose 另用最小私有配置启动通过，云端既有 collection 的 manifest/count 和核心健康检查通过；不改写云端索引，不触发聊天或 Embedding 请求。记录在 `default-compose-v1.json`，其范围不包含真实模型业务调用。本地正式知识库付费导入与 Hybrid 运行未执行。

## 健康、日志与追踪

`/health/live` 与兼容 `/health` 仅检查进程。`/health/ready` 检查业务迁移、Checkpoint/Store 连接与表、初始化状态，不依赖 Langfuse 或模型调用。`/health/capabilities` 需要 Bearer，2 秒单依赖期限与 5 秒诊断缓存，明确 Redis 故障的新问答拒绝和审批/读取有限回退。

请求摘要使用 JSON INFO 日志，关联服务生成请求 ID、图运行 ID、HMAC 会话/草稿摘要、版本、意图、节点、工具、缓存/检索统计、阶段计时和已知/未知用量。SDK 普通日志不透传异常原文和请求 URL。请求根 span 与现有 callback 使用同一上下文；自动 callback 内容先删除，最终 exporter 再执行白名单，过滤失败不发送原文。未知用量不填零，共享检索保持既有单次用量归属。

按本地锁定 Langfuse SDK 4.12 接口实现，核对了 [官方屏蔽说明](https://langfuse.com/docs/observability/features/masking)；Compose 依赖使用 [官方启动顺序](https://docs.docker.com/compose/how-tos/startup-order/) 的健康与一次性完成契约。未引入独立指标平台。

本地真实 OTLP HTTP 接收了 4 类合成轨迹、12 个 span，父子关系完整，敏感标记未泄漏；600 个 span 入队约 0.032 秒，队列上限 256，接收端关闭后的清理等待约 3.32 秒。首次测试发现根记录被 SDK 默认过滤器丢弃，修复了导出范围；第二版模型标签错误，第三版根据实际模型调用记录。原失败原因保留。

上述 tracing-v3 是收尾前的 SDK 合成导出契约记录。收尾新增真实 HTTP Service、实际图、PostgreSQL/Redis/Qdrant 的业务链路，以及远程 Langfuse Observations API v2 查询；证据分别保存在 closeout/business-tracing-local-v1 和 closeout/remote-tracing-v1，旧记录不改写。

## CI 与运行证据

改造已有 `test.yml`：保留 Ruff、Pyrefly、pytest、Markdown，替换上游固定笑话容器测试为真实 PostgreSQL/Redis/Qdrant 的 Support Agent 验收。普通运行不依赖个人环境或付费密钥，保存脱敏记录并清理本次专属项目。移除了 Fork 不需要的 Codecov 密钥上传步骤。`deploy.yml` 和 `live-smoke-test.yml` 保留上游仓库条件，Fork 不默认发布镜像或部署 Azure。

离线全量 pytest 为 372 通过、4 项运行环境相关跳过；最终路由异常分支修复后，相关 23 项测试通过。Ruff、Pyrefly 与 Markdown 检查通过。容器验收通过独立脚本执行，不把 pytest 的跳过项计为通过。工作流尚未推送执行，不能据本地检查声称 GitHub Actions 已绿。

容器尝试的实际阻塞：初始 PATH 缺凭据辅助程序；Docker Hub 直连超时，通过本机代理解决；锁定依赖安装时 C 盘耗尽，Docker 元数据文件系统只读；释放空间后 BuildKit bbolt 缓存页损坏导致引擎启动失败。用户确认新安装环境仅含本次缓存并授权修复后，备份设置、停止 Docker 专属 WSL，将损坏数据盘移至本地忽略目录，让 Docker 重建空盘。引擎 29.8.2 已恢复，代理设置保留，Ubuntu 与项目源码不受影响。不得把引擎恢复或下载完成记作业务验收通过。

容器首轮发现 CI 模型继承了上游固定流式响应，导致 Streamlit 来源校验失败；补齐 `_astream` 与流式工具回归。随后新增错误检查错误地预期 HTTP 500，修正为既有澄清降级契约；补充 `router_unavailable` 标识时发现并修复局部 import 遮蔽。前三轮记录保留，第四轮全通过。运行日志实际覆盖缓存命中、路由异常、超时、断连及依赖故障，未泄漏合成异常标记、身份、工单描述或测试密钥。UI 隐私说明也移除了上游与当前行为不符的 LangSmith 全量记录声明。

## 剩余验收与限制

- 远程 Langfuse API 接收与查询已验证；网页面板辅助检查仍待登录，未创建公开分享。
- GitHub Actions 对应最终提交的运行记录待补录；以 closeout/github-actions.json 为准。
- 浏览器 WebSocket、本地正式知识库付费导入和 Hybrid 组合未运行，已通过范围见上述记录。
- Phase 8 的 854 个原始评测文件摘要不变；本地秘密值扫描无匹配，个人资料、配置及修复备份均被 Git 忽略。

本项目继续限定单 worker、客户端声明身份、共享 Bearer、固定模拟服务/设备和本地 DEMO 工单。没有完整用户认证、多租户权限、高可用、多实例图执行锁或生产 SLA。Phase 8 其他语义失败未通过本阶段部署改造被自动解决。

费用口径补充：前两轮复测沿用了客户端默认传输重试，回调记录不能证明物理 HTTP 尝试总数；manifest 中 physical_calls 是历史字段名，本阶段只按逻辑调用解释。0.223848 元仅按有返回 usage 估价，未核对供应商账单。后续脚本已关闭 SDK 自动重试并限制输出，不为修正计数口径再次付费刷跑。

## 收尾：资源边界与真实业务追踪

默认 Redis 显式配置 64 MiB 数据预算、noeviction、128 MiB 容器硬上限；默认、本地、CI 和 dev 合并结果一致。专属压力实验以 4 MiB 填至真实 OOM，再临时降为 3 MiB 保持跨连接稳定压力。缓存 SET 失败后仍返回实际 Qdrant 检索证据；独立 HTTP 新问答返回 503 且无图节点/模型调用；审批、读取及重复批准维持单张工单；未发生逐出或 OOMKill；删除专属测试键并恢复 4 MiB 后问答和缓存命中恢复。正式 64/128 MiB 只是演示起点，共享内存对问答可用性的影响保持公开。首轮压力消退导致的失败记录保留。

真实业务追踪包含知识问答、热缓存、模拟工具、草稿、批准、路由澄清降级和 Redis 不可达，共 7 个请求。每个请求的 trace ID、图 run ID、请求 ID 对应，跨审批请求通过会话/草稿 HMAC 和版本关联。审批请求没有模型调用，fixture 用量明确未知，热缓存外部 Embedding 请求为零。HTTP 200 的路由失败记录 router_unavailable。

本地接收 54 个实际业务 span，远程验收通过用户指定项目的 Observations API v2 查询，并扫描完整字段组中的敏感标记、身份、正文、凭据与内部地址；公开资产只保存白名单摘要和关联 ID。接收器不可达时业务正常返回，服务退出码为 0，关闭约 4 秒内完成。不会用 SDK 发送成功代替远端查询证据。[Langfuse 查询接口说明](https://langfuse.com/docs/api-and-data-platform/features/public-api)。

收尾离线测试 375 通过、4 项容器标记跳过；容器、内存与业务追踪另有独立记录。普通 CI 新增内存压力和本地业务追踪，且 pytest 默认隔离个人 Langfuse 配置。收尾没有聊天模型或 Embedding 付费调用，也未重跑草稿、Phase 8 留出评测、Hybrid 或正式知识库导入。
