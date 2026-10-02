# Phase 6：持久化、用户上下文与工单恢复

本阶段复用上游已有的 PostgreSQL Checkpointer / Store 接入，新增会话业务索引、显式回复偏好、异步工单 Repository、版本化业务迁移和故障恢复验收。知识库仍使用 Phase 5 选定的 Dense20 / v2，不调整检索参数或知识回答提示词。所有工单都是演示记录，服务状态和设备查询仍是 Mock。

## 数据边界与运行约束

| 数据 | 保存位置 | 范围与职责 |
| --- | --- | --- |
| 消息、实体、检索证据、草稿、中断和执行指针 | LangGraph Checkpointer | 按 thread_id 恢复执行；由组件 setup 管理内部表 |
| 回复语言、详细程度 | LangGraph Store | namespace 为 `(support, user_id, preferences)`，key 为 `explicit` |
| 用户声明与会话归属 | users、support_sessions | 列表及归属校验，不复制执行状态 |
| 已批准的工单 | tickets | 业务事实、唯一约束、幂等核查；与检查点分别提交 |
| 企业知识证据 | Phase 5 Qdrant / ES 数据集 | 不存入用户长期偏好 |

`user_id` 是客户端声明的演示身份，**不是登录认证**。持有访问权限的人可以声明其他 ID；当前实现不构成生产多租户授权。共享 `AUTH_SECRET` 也不能把 ID 绑定到个人身份。部署仅支持一个应用 worker：进程内执行锁负责同线程串行，数据库唯一约束只证明工单创建防重，不能证明图执行在多 worker 下安全。

身份 ID / thread_id 支持 1–128 位 ASCII 字母、数字及 `_.:-`，首位必须是字母或数字。新会话沿用浏览器 URL 中的 user_id；后端不再为 Support 请求静默生成 user_id。拥有检查点却未登记归属的旧线程会被拒绝认领。

## 本地 PostgreSQL

实测环境为 Windows、Python 3.13、PostgreSQL 17.11、psycopg 3.3.6 / pool 3.3.3、langgraph-checkpoint-postgres 3.1.2。没有 Docker 的机器可使用原生开发实例，不安装系统服务、不需要管理员权限。

```powershell
# 使用当前项目环境；uv sync --frozen 的用户将 venv 改为 .venv
$env:PYTHONPATH = 'src'
.\venv\Scripts\python.exe scripts/local_postgres.py start --activate
.\venv\Scripts\python.exe scripts/migrate_support.py
.\venv\Scripts\python.exe src/run_service.py
# 另一个终端
.\venv\Scripts\streamlit.exe run src/streamlit_app.py
```

启动脚本从 [EDB 官方 Windows 二进制分发](https://www.enterprisedb.com/download-postgresql-binaries)下载固定的 17.11，使用 HTTPS；记录的 SHA256 是本地清单摘要，并非与供应商公布摘要比对。它只绑定 127.0.0.1:15432，数据与随机凭据保存在 `.cache/phase6-pg/`。`--activate` 会先私下备份原 `.env`，再写入连接参数；不打印密码。再次启动会复用实例。停止命令为 `python scripts/local_postgres.py stop`。

Docker 替代入口：编辑 `.env` 的 POSTGRES 参数后运行 `docker compose -f compose.postgres.yaml up -d postgres`，再运行迁移脚本。此配置只启动数据库，使用数据卷、健康检查及 localhost 端口绑定。本机 **未执行 Docker 构建/启动**，不能把原生实测结果视为 Compose 验收。宿主机应用连接 `127.0.0.1:15432`；同一 Compose 网络内的应用连接 `postgres:5432`。勿与原生实例同时占用 15432。全栈生产部署属于 Phase 9。

Saver、Store、业务 Repository 各有一个池；单 worker 默认合计 3 条连接，调大参数时按三池计算。使用 `make_conninfo` 正确处理密码中的空格、引号和 URI 特殊字符，不拼接 URI。池启动显式等待连接、使用连接检查、超时与关闭流程。业务迁移需要显式执行，服务启动只验证版本/摘要；缺失或不一致则启动失败，**不自动回落 SQLite**。LangGraph 的两个 setup 分别在 lifespan 执行一次，不在包装器重复执行。

Windows 上当前 Uvicorn 默认循环与 psycopg 不兼容，服务显式指定返回 SelectorEventLoop 实例的工厂。原生启动及独立服务进程验收均覆盖此路径。

## 接口与偏好

| 接口 | 用户上下文与行为 |
| --- | --- |
| POST `/support-agent/invoke`、`/support-agent/stream` | JSON 必填 user_id；检查归属后读取或恢复图；禁止非空 agent_config |
| POST `/support-agent/history` | JSON 包含 thread_id、user_id；拒绝其他用户/其他 agent 读取 Support 线程 |
| GET `/support-agent/approval` | query 包含 thread_id、user_id；未知线程 404，客户端按无待审批处理 |
| GET `/support-agent/threads` | user_id、limit；读取业务索引，返回该用户 Support 会话 |
| GET/PUT/DELETE `/support-agent/preferences` | query user_id；显式查看、覆盖或删除偏好 |
| GET `/support-agent/tickets` | query user_id，limit 1–100，默认 20；只返回该用户工单 |
| `/agui/support-agent/run` | 保持拒绝，不开放替代审批通道；其他 agent 也不能借用 Support 线程 |

非流式请求缺少身份为 422，归属不符为 403。SSE 已开启后以 `type=error` 事件报告拒绝，不输出消息或检查点内容。归属校验在读取图之前执行；审批写入的 user_id 来自服务 RunnableConfig，查询工具参数不允许模型自带 user_id。

PUT 的完整 JSON 只支持 `{"language":"zh|en","detail":"concise|detailed"}`，额外字段拒绝。Store 保存 schema_version、explicit_user_setting 来源及更新时间；覆盖采用最后写入生效，不提供 CAS。偏好只改变模型回答表达，本轮明确语言/详细程度要求优先。确定性的审批、错误和工单核对模板目前保持中文。不会从历史聊天自动推断或恢复已删除偏好，也不会将草稿、批准决定、设备信息或知识文档写成长期记忆。

Store 读取失败返回 unavailable，模型使用默认表达；写入/删除失败返回 503，不显示成功。删除只影响后续偏好读取，不删除消息、检查点或工单，也不撤销正在执行的请求已读取的偏好。

工单查询返回 `source=business_database`、`is_demo=true`、`is_mock=false`；表示实际读取了演示业务库，不代表已接入企业工单系统。固定 INC 示例默认关闭，只有显式 `SUPPORT_DEMO_SAMPLES=true` 才开放并标记 fixed_mock_sample。

## 工单事务和恢复

`draft_id`、`idempotency_key`、ticket_id 有数据库唯一约束。创建事务验证会话归属，使用 INSERT ON CONFLICT DO NOTHING 后读取已存记录，并核对 user/thread/version/key/fingerprint/source。冲突拒绝覆盖，不在失败的唯一约束事务内继续查询。

工单提交与图检查点不是一个事务。业务提交成功后即使进程退出，重试仍会先按请求核查原记录，再完成图状态。写入超时不能直接视为未创建：核查匹配则成功，核查无记录才为可重试失败，核查不可用则保持 unknown。unknown 时不允许修改或取消绕过对账。生产接口没有提交后崩溃开关；受控 `os._exit(73)` 仅存在于独立测试服务脚本。

## SQLite 兼容与导入

`DATABASE_TYPE=sqlite` 仍是明确可选的开发模式；不持久化跨进程的 InMemoryStore。PostgreSQL 故障不会分流写到它。SQLite 业务适配层增加会话归属，旧无归属记录不会默认公开。

正式切换前保留 `.env`、checkpoints.db、tickets.db 及 SQLite 的 WAL/SHM；不要在运行中的旧服务旁仅复制主库文件充当一致备份。当前工作区保留原文件，切换前配置位于 `.cache/phase6-pg/env-before-phase6.private`。如需处理旧的无用户归属待审批，使用 Phase 5 提交 `58907942337022749bc4ebbab1323c1909bacb58` 的单独 checkout 和原 SQLite 环境完成、取消或归档，再切回新环境。禁止把旧消息重放当作原审批恢复。

工单导入不迁移内部 Checkpoint 表，也不恢复旧进程内偏好。显式映射文件示例：`{"DEMO-原始32位编号":"declared-user-id"}`；未映射记录保留待处理，不能创建万能共享用户。

```powershell
$env:PYTHONPATH = 'src'
# .env 指向目标 PostgreSQL；映射和报告放在私有目录
.\venv\Scripts\python.exe scripts/import_sqlite_tickets.py --source tickets.db --mapping .cache/ticket-owners.json --output .cache/import-preflight.json
# 审阅预检后，显式执行；每次使用新的输出文件
.\venv\Scripts\python.exe scripts/import_sqlite_tickets.py --source tickets.db --mapping .cache/ticket-owners.json --output .cache/import-applied.json --apply
```

源库以只读 URI 打开。预检不写业务记录；执行按批事务导入，冲突拒绝覆盖，发生并发不一致则回滚整批。保留编号、草稿版本、幂等键、指纹和原始时间；PostgreSQL 时间可能采用等价的时区表示。原 thread_id 保存到 source_thread_id，活跃 thread_id 为 NULL，不伪造可恢复会话。相同导入复用原记录；报告列出新增、复用、冲突、未处理数量。

## 验收与复现

Phase 5 基线记录在 [manifest](../evaluation/baselines/phase5/manifest.json)，可从该 Git 提交恢复源码和报告。新证据保存在 [evaluation/phase6](../evaluation/phase6/)。

```powershell
$env:PYTHONPATH = 'src'
.\venv\Scripts\python.exe -m pytest -q
.\venv\Scripts\ruff.exe check src scripts tests
.\venv\Scripts\pyrefly.exe check
# 专用测试实例要求 CREATEDB；创建并保留独立 phase6_test_* 数据库
.\venv\Scripts\python.exe scripts/run_phase6_storage.py --connection-json .cache/phase6-pg/connection.json --output .cache/storage-new.json
# 仅在可停止的本地开发实例上附加以下参数，实测数据库停机/重启
# --pg-ctl .cache/phase6-pg/pgsql/bin/pg_ctl.exe --pg-data .cache/phase6-pg/data
# 付费回归：需单独授权费用；本阶段授权总上限 10 元
.\venv\Scripts\python.exe scripts/run_phase6_live.py --connection-json .cache/phase6-pg/connection.json --output evaluation/phase6/live-new.json
```

连接 JSON 是包含 host、port、user、password、dbname 的私有文件，不应提交 Git。存储脚本不用模型，真实 HTTP 服务子进程使用确定性 DraftModel；真实模型脚本使用实际模型及检索，并记录使用量。不能把前者当模型语义准确率，或把后者的小样本当全面质量改进。

存储实测最终记录为 [storage-final.json](../evaluation/phase6/storage-final.json)：4 个独立连接、16 次竞争请求最终一行；版本/内容/归属冲突；只读源及重复导入；实际服务重启恢复相同 history 和 pending，批准前零工单；业务 commit 后退出码 73，恢复返回同一编号；实际 PostgreSQL 停机、Store 降级及业务 503、重启恢复；删除偏好后不会被旧检查点恢复。测试数据库总工单为 5 条：并发样本 1、导入 2、重启审批 1、提交后崩溃样本 1。此前 `storage-retry2.json` 也通过，最终运行增加了消息历史和停机目标实例校验。

首次存储运行在服务启动时暴露循环工厂返回类的问题；第二次运行的本地 HTTP 被代理转成 502。修正循环实例和测试 HTTP 的 trust_env 后重跑通过。前两份阶段记录保留为失败过程证据，不能计为恢复成功。

真实模型首轮 9 个场景、20 次调用，发现默认中文指令与英文偏好冲突。补充明确的偏好覆盖规则后，4 个偏好场景复测通过；两轮累计保守估价 **0.348176 元 / 10 元上限**。明细见 [逐项审阅](../evaluation/phase6/answer-review.md)，首次失败原始输出仍然保留。实际 `src/run_service.py` 启动、11 个 agent 元数据及 PostgreSQL 偏好读取已通过，见 [入口记录](../evaluation/phase6/service-startup.json)。

工作区原 `tickets.db` 只有用户/会话表，没有旧 tickets 表，实际源预检记录为格式不适用，未导入或认领记录。导入正确性使用独立的有效 SQLite 样本验证，不能宣称已迁移用户真实历史。见 [原库只读检查](../evaluation/phase6/legacy-preflight.json)。

最终测试 **328 passed、4 skipped**，Ruff、Pyrefly（0 errors）、Markdown 和离线锁文件检查通过。82 个冻结 RAG/知识/数据集/Phase 4–5 记录文件与基线哈希一致。检查及源码完整性记录见 [validation.json](../evaluation/phase6/validation.json) 和 [integrity.json](../evaluation/phase6/integrity.json)。Phase 5 的 P1 短问证据不完整、冲突制度回答可能擅自选择更严格临时规则仍属于已知语义问题，不因本阶段存储通过而改记成功。Docker、生产登录授权、多 worker 图执行、云数据库和全栈部署未验收。

Phase 3–5 的历史付费场景脚本按各阶段基线 checkout 复现；它们不代表当前身份协议，也不能沿用以前的 50 元授权启动新评测。本阶段使用上述独立 Phase 6 脚本和 10 元预算。
