# 单实例演示部署

本项目默认运行一个 Support Agent worker、PostgreSQL、Redis 和 Streamlit，连接已初始化的云端 Qdrant v2 collection。模型与 Embedding 仍可调用远程服务；容器部署不等于离线部署。实际验证范围和未完成项见 [Phase 9 记录](phase9_deployment_observability.md)。

## 前置条件

- Docker Desktop / Linux Docker Engine 和支持 `!reset` 的 Compose v2.24.4 或更新版本。
- 建议为镜像构建预留至少 20 GB 磁盘空间。首次锁定依赖安装较大。
- 聊天模型凭据、DashScope `text-embedding-v3` 凭据，以及既有 v2 Qdrant collection。不得覆盖旧 collection。
- 随机的 `AUTH_SECRET` 和数据库密码。共享 Bearer 密钥只提供服务访问门槛；`user_id` 仍由客户端声明，不能当作生产用户登录。

镜像固定为 Python 3.13.14、PostgreSQL 17.11、Redis 7.0.15、Qdrant 1.19.0。Python 依赖用 uv 0.12.5 按 `uv.lock` 安装。新的 `phase9_postgres` 卷与历史 PostgreSQL 16/Phase 6 卷分离；不要把旧大版本数据目录直接挂入新镜像。

## 默认启动路径

复制 `docker/compose.env.example` 为仓库根目录的 `.env.compose`，填写占位值。不要复制整个个人 `.env`，也不要提交该文件。

```sh
docker compose --env-file .env.compose -p esa-demo config --quiet
docker compose --env-file .env.compose -p esa-demo up -d --build --wait
```

`--env-file` 用于 Compose 插值，`ESA_ENV_FILE=.env.compose` 用于容器环境注入；两者应指向同一文件。Compose 显式 `environment` 覆盖 env 文件，容器 PostgreSQL 和 Redis 地址固定为服务名，不能使用宿主机的 `localhost`。不要将 `config` 的完整输出公开，它会展开凭据。

启动顺序为 PostgreSQL/Redis 健康 → 一次性业务迁移完成 → Service 初始化 Checkpoint/Store 并校验 schema → 核心就绪 → Streamlit。业务迁移失败时 Service 不启动；应用不会静默退回 SQLite。再次启动会复用已应用迁移，不清空数据。

打开 `http://127.0.0.1:8501`。Service 为 `http://127.0.0.1:8080`，健康端点为 `/health/live` 和 `/health/ready`。详细能力端点 `/health/capabilities` 要求 Bearer 令牌。Streamlit 自动接收同一 `AUTH_SECRET` 并由 AgentClient 传递。

默认只开放两个回环端口，数据库与 Redis 不映射到宿主机。Service/App 使用 UID 10001；PostgreSQL/Qdrant 各用自己的命名卷。Redis 关闭 RDB/AOF，只保存可丢弃缓存和速率状态，工单与幂等记录的权威为 PostgreSQL。

Redis 默认 `REDIS_MAXMEMORY=64mb`、`maxmemory-policy=noeviction`，容器硬上限 `REDIS_CONTAINER_MEMORY=128m`。两项可在私有 Compose env 文件中调整；数据预算必须低于容器上限，留出进程、连接、缓冲与分配器开销。64/128 MiB 是经过本项目演示检查的起点，不是通用容量比例。缓存与限流共用实例，因此保持 noeviction，内存满时新问答可能被拒绝；不能通过淘汰限流状态绕过保护。

默认 Dense20/v2 使用 800/100 分块契约和 0.65 dense 门槛；启动不自动重新向量化。缺少、空或不兼容索引会显示检索不可用，其余只读工具和工单流程可独立工作。

## 本地 Qdrant

设置 `LOCAL_QDRANT_COLLECTION=enterprise_support_dense_local_v2`，用新的名称。

```sh
docker compose --env-file .env.compose -p esa-demo -f compose.yaml -f compose.local.yaml up -d qdrant
```

本地覆盖将 URL 显式设为 `http://qdrant:6333`，并将 collection 与云端分开。初始化是单独操作，需要确认本次 Embedding 预算；不是每次启动自动运行：

```sh
docker compose --env-file .env.compose -p esa-demo -f compose.yaml -f compose.local.yaml run --rm index python scripts/import_local_index.py --paid --budget 1
docker compose --env-file .env.compose -p esa-demo -f compose.yaml -f compose.local.yaml up -d --wait
```

命令中的 `1` 是调用者授权的人民币费用上限示例。导入复用冻结的 `snapshot_v2.json` 和 immutable collection/manifest 检查；维度、模型、分块或快照不匹配时拒绝混写。运行中失败保留未就绪 manifest，需排查后恢复导入或选用新 collection。云端索引不会被删除。

## CI 确定性组合

此组合不读个人 `.env`，没有付费模型调用。PostgreSQL、Redis、Qdrant 都是真实容器；聊天和向量是显式测试替代，不能报告为真实模型质量。

```sh
docker compose --env-file docker/ci.env -f compose.yaml -f compose.local.yaml -f compose.ci.yaml build
uv run python scripts/verify_phase9_containers.py --project esa-p9-test-manual01 --output evaluation/phase9/manual01
```

每次使用新的项目名和输出目录。脚本只接受 `esa-p9-test-*`，拒绝已有容器项目，并在退出时清理自己创建的容器和卷。它会停止依赖、重启服务，并把数据库备份恢复到同一专属 PostgreSQL 内的新数据库；不对日常数据库执行演练。

CI collection 固定为 `enterprise_support_dense_ci_v1`，其模型契约为 `deterministic-ci/fixture-v1`，不会被当作 DashScope 向量读取。模型模式需同时显式设置 `CI_TEST_MODE=true` 和 `USE_FAKE_MODEL=true`，缺少生产密钥不会自动变为测试成功。

## 收尾验收入口

以下脚本每次使用新项目和新目录，退出时只清理本次容器与卷：

```sh
uv run python scripts/verify_phase9_redis_memory.py --project esa-p9-test-memory-manual01 --output evaluation/phase9/closeout/redis-memory-manual01
uv run python scripts/verify_phase9_business_tracing.py --project esa-p9-test-tracing-manual01 --output evaluation/phase9/closeout/business-tracing-manual01 --receiver local
```

内存实验初始预算为 4 MiB，填充合成键直到实际 OOM，再将专属实例临时预算设为 3 MiB，避免填充连接释放缓冲导致压力消退；恢复时只删测试键并恢复 4 MiB。宿主机与普通实例不被填满。本地 OTLP 接收器仅用于验收运行，远程验证需显式选择用户自己的项目。

复制 `docker/tracing.env.example` 到私有 `.env.phase9-tracing`，填入指定项目区域地址和项目读写 API keys，再运行：

```sh
uv run python scripts/verify_phase9_business_tracing.py --project esa-p9-test-remote-manual01 --output evaluation/phase9/closeout/remote-tracing-manual01 --receiver remote --credentials .env.phase9-tracing
```

脚本在 CI 组合后叠加 `compose.tracing.yaml`，只向指定项目发送合成业务元数据，并通过 Observations API v2 查询关联和过滤结果；普通 CI 不使用远程密钥。响应中的 `X-Request-ID`、启用追踪时的 `X-Trace-ID` 和已执行图的 `X-Run-ID` 可用于核查。API keys 不应放入 `.env.example` 或提交到 Git。远程网页面板需要用户登录，脚本不创建公开分享。

## 可选组合与开发

`compose.hybrid.yaml` 添加 Elasticsearch 9.5.3 smartcn 并设置 `ES_URL=http://elasticsearch:9200` 与 hybrid 模式。使用前按 Phase 5 流程建立与 dense 相同 snapshot 的 lexical index；单独启动 ES 不等于索引已配对。该组合不默认启用，也不意味着已重新验收七组检索实验。

`compose.dev.yaml` 才启用热重载、只读源码挂载和 PostgreSQL/Redis 回环调试端口。稳定配置不运行 watch。宿主机 Python 开发沿用 `.env.example` 与 [Phase 6](phase6_memory_postgres.md) 的显式迁移入口，`DEPLOYMENT_MODE=false` 可明确选择 SQLite 开发模式。

上游示例代码保留，但默认不会导入或初始化它们。设置 `ENABLED_AGENTS='["support-agent","chatbot"]'` 显式启用；列表、入口和加载范围一致。其他示例可能需要自己的外部依赖。

## 更新和停止

更新前先备份、记录 Git 提交与配置版本，设置 `CODE_VERSION` 为构建提交。重新执行默认 `up -d --build --wait`；迁移和镜像回滚限制见 [运维说明](operations.md)。

```sh
docker compose --env-file .env.compose -p esa-demo down
```

`down` 保留命名卷；`down --volumes` 会删除数据，只可用于明确的临时演练项目。不要对日常部署使用后者。容器停止宽限 40 秒，Uvicorn 处理请求的优雅退出期限 30 秒，追踪关闭最多等待约 3 秒。取消不保证撤销已经提交的数据库事务。
