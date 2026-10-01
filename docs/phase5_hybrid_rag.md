# Phase 5：混合检索、重排序与对照实验

开发日期：2026-10-01。功能开发、真实集成和批量实验已完成；已知回答质量问题及尚未执行的 Docker 验证见第 6 节。用户授权本阶段独立预算 50 元；使用现有工作空间密钥实测确认 gte-rerank-v2 可调用，没有升级套餐或充值。

## 1. 基线与实现范围

Phase 4 未提交代码以可恢复压缩包保存在 [baseline](../evaluation/baselines/phase4/manifest.json)，包含真实工作区源码、语料、配置示例及原始报告的文件指纹。该快照记录 base commit 和 dirty 状态，不把 Phase 3 提交号当成 Phase 4 已提交版本。原 `data/knowledge/` 和 `evaluation/results/phase4/` 保留。

补发 GitHub 时按该快照独立重建了 [Phase 4 提交 cf77eab](https://github.com/penpendashuai-alt/enterprise-support-agent/commit/cf77eab83f131c6f95cbb0cee3c14939f9f51cda)，隔离检出后 304 项测试通过、4 项 Docker 测试跳过。Phase 5 作为后续独立提交；历史报告和原始快照仍记录当时的未提交状态，不事后篡改其版本信息。

正式模式为 Dense、Elasticsearch BM25、RRF Hybrid 和 Hybrid + Reranker，另提供 Dense + Reranker 消融。Agent 继续消费统一 RetrievalResult；服务和工单分支不依赖 ES 或重排序。原有独立检索节点、只读工具允许列表、引用校验与工单审批继续保留。

新增模块包括 `lexical_store.py`（ES REST）、`fusion.py`（纯函数 RRF）、`reranker.py`（真实 DashScope HTTP）、`snapshot.py` / `pair_ingestion.py`（共享快照）、`hybrid_retriever.py`（统一编排）和 `metrics.py`（证据级评测）。复用现有 httpx，不新增 ES Python SDK 或全面升级依赖。

## 2. 最小 Elasticsearch 开发环境

```powershell
docker compose -f compose.elasticsearch.yaml up -d --build
docker compose -f compose.elasticsearch.yaml ps
```

固定 Elasticsearch **9.5.3** 和同版本官方 SmartCN 插件，仅监听本机 `127.0.0.1:19200`，配置命名数据卷、512 MiB Java 堆、2 GiB 容器内存上限和健康检查。仅启动 ES，不同时启动 Qdrant、应用或 UI。当前关闭认证的单节点配置只用于本地开发，不适合作为生产安全配置。

本机没有 Docker，因此实际服务验证使用同版本官方 Windows ZIP 和插件，下载后核对官方 SHA-512；服务文件位于被忽略的 `.cache/phase5-es/`，使用包内 JDK，仅绑定本机 HTTP/transport 端口，不安装系统服务。Docker 构建和 Compose 启动未在本机执行，不能用 Windows 实测替代这一项。

Windows 开发可执行 `./scripts/start_phase5_es.ps1`，首次会下载较大的官方包；停止使用 `./scripts/start_phase5_es.ps1 -Stop`，只会停止本项目创建并占用 19200 端口的进程，保留索引数据。

本机数据盘剩余约 12.5 GiB，但占比低于默认水位；仅对本次独立开发 ES 设置低/高/洪水水位为剩余 8/6/4 GiB，保留磁盘保护。未清理用户文件或关闭保护。真实 ES 重启后 72 个分块、SmartCN 与快照仍可读取，记录见 `es-restart.json`。

SmartCN 将中文短语分词，将 `DEV-001` 拆为 `dev`、`001`。额外 keyword 字段 `technical_terms` 保留完整小写标识 `dev-001`、`809`、`sso` 等；提取规则和 analyzer 输出均写入真实集成报告。正文与标题使用 SmartCN，标题权重固定 2；正文多字段匹配加技术标识精确 terms 子句，未按某道留出题加权。

## 3. 共享分块、构建与启用

新版 36 篇文档（34 篇自行编写演示资料、2 篇公开文档原创摘要），共 72 块。仍使用 800 字符分块、100 字符重叠及 DashScope text-embedding-v3 / 1024 维；未把分块调整混入检索实验。

两端读取同一份 [snapshot_v2.json](../evaluation/datasets/snapshot_v2.json)，不是各自解析。共享 snapshot_id 描述语料/分块；Dense 与 ES 各自有独立契约指纹。构建完整核对双方 chunk_id、content_hash、数量及 snapshot_id 后才标记 ready。

```powershell
python scripts/ingest_pair.py --output .cache/phase5-ingestion-new.json
```

演示入口固定使用 `enterprise_support_dense_v2` 与 `enterprise-support-lexical-v2`，不覆盖 v1 或旧项目 collection。重复构建验证全部内容一致后复用，实测新增 Embedding 用量为 0。任一侧失败不自动启用；已存在的可用索引不被回滚或删除。两库没有天然跨库事务，新版启用仍需要一起改配置并重启；请求核对双方 ready 和 snapshot_id，不将不一致默认为网络问题降级。

配置兼容：`RAG_RETRIEVAL_MODE=dense`、`RAG_DENSE_LEGACY=true` 保持原 Top 5 / 0.65 路径，且无需 ES/Reranker。新路径用 `RAG_DENSE_LEGACY=false`、新版 collection 和相应模式。BM25-only 不创建 Embedding/Qdrant 客户端。重排序独立配置 key、URL、模型、候选上限和超时；只有明确设置 `RERANK_USE_EMBED_KEY=true` 时才复用 Embedding 密钥，示例默认不启用。

不要把旧 `RAG_MIN_SCORE` 用于新路径。新路径分别读取 `RAG_DENSE_THRESHOLD`、`RAG_BM25_THRESHOLD`、`RAG_RRF_THRESHOLD`、`RAG_RERANK_THRESHOLD`。`.env.example` 已同步冻结阈值和推荐的 v2 Dense20 配置；代码的无配置默认值仍保留 Phase 4 兼容行为。

本地 `.env` 已启用下列配置，原文件仅备份到忽略的 `.cache/`，没有输出或修改密钥。正在运行的应用需重启以加载新配置。

```dotenv
QDRANT_COLLECTION=enterprise_support_dense_v2
ES_INDEX=enterprise-support-lexical-v2
RAG_RETRIEVAL_MODE=dense
RAG_DENSE_LEGACY=false
RAG_DENSE_CANDIDATES=20
RAG_BM25_CANDIDATES=20
RAG_FINAL_K=5
RAG_DENSE_THRESHOLD=0.65
RAG_BM25_THRESHOLD=8
RAG_RRF_THRESHOLD=0
RAG_RERANK_THRESHOLD=0.3
RAG_RRF_C=60
RAG_ALLOW_FALLBACK=false
RERANK_MAX_CANDIDATES=40
```

切换到 `bm25`、`hybrid`、`hybrid_rerank` 或 `dense_rerank` 后重启；后三者按路径需要 Cloud 凭据，使用 ES 的模式需先启动本地服务。用户已授权本机 `RERANK_USE_EMBED_KEY=true`；公开示例仍默认 false，避免把同名工作空间当作授权证明。Dense + Reranker 消融需另外设置 `RAG_DENSE_CANDIDATES=40`。

## 4. 排序、证据与故障

Dense/BM25 初始各取 20 个分块，融合按 chunk_id 而非 doc_id 去重，因此可保留同文档的多个条款。RRF 使用 `sum(1/(60+rank))`，rank 从 1 开始，路内重复取第一次出现，空路贡献为 0；同分按 chunk_id 字典序稳定排序。分别保留 Dense/BM25 原始排名与分数。

真实 gte-rerank-v2 收到标题加完整正文，按返回 index 映射原候选，验证数量、范围、重复项、有限数值及分数范围。最多 40 块；请求前采用保守 UTF-8 字节上限约束单条和总输入，超限明确失败，不静默截断；该限制不冒充精确 tokenizer 或账单。最终给 LLM 的截断在重排序之后。

Dense、BM25、RRF、Reranker 分数分别存放，缺失为 null；兼容 score 明确带 score_type，均不解释为正确概率。最多选择 5 个分块，总证据 JSON 预算 4500 字符；记录因分数、最终数量或字符预算排除的原因，编号在选择后分配。

总检索时限为 90 秒，阶段 timeout 与有限重试位于这一时限内。`RAG_ALLOW_FALLBACK` 默认关闭；显式启用时仅对暂时网络/服务错误降级：ES 失败可用 Dense，重排序失败使用之前的 Hybrid/Dense，并改用实际路径的门槛。认证、非法输入、错误返回和索引不一致不能假装普通故障降级。结果记录请求模式、实际模式、阶段、失败原因和用量。

真实故障记录包括不可达 ES、本机不可达重排序端点和旧 Dense/新 ES 混配；前两者按配置降级，后者拒绝检索。Mock 另测错误响应、总时限、重试及写入失败，不将其当真实召回效果。

## 5. 数据集和实验方法

[hybrid_v1.json](../evaluation/datasets/hybrid_v1.json) 含 128 题，dev 68、heldout 60。近似改写作为一组划分；两份 U 盘冲突资料的问题合并同一 dev 组，避免高度相近问题跨 split。两个 split 共享知识库与部分业务主题，不宣称未见领域泛化。题目与标注由编码助手编写并逐源核对，不宣称独立人工数据标注。

标注包含问题、分组、类别、是否可答、文档版本、章节与内容锚点、必要证据替代组、答案资料和子问题可答性。标注不进入查询、索引或生成提示词。旧 Phase 4 heldout 仅作回归，不再作为新盲测。开跑前修正了 11 题的必要章节并合并冲突组，未根据检索器输赢修改问题。

七组为：同语料旧 Dense 5/0.65、Dense20、Dense40（候选预算对照）、BM25、Hybrid、Hybrid+Reranker、Dense40+Reranker。候选数、RRF 常数、标题权重和最终预算固定；每个可调组在 dev 上比较五个本类型阈值，以必要证据全部齐全率与无答案无证据返回率的平均值选取，平分优先证据完整性、再低阈值。旧基线不调参。

```powershell
python scripts/evaluate_hybrid.py --split dev --freeze-output evaluation/results/phase5/frozen-new.json --output evaluation/results/phase5/dev-new.json
# dev 生成冻结配置，再运行 heldout；必须用新文件名，保留首次结果
python scripts/evaluate_hybrid.py --split heldout --freeze evaluation/results/phase5/frozen-new.json --output evaluation/results/phase5/heldout-new.json
```

检索用原始问题，不调用 Router；按每题轮换组顺序串行执行，不缓存查询。日志逐请求保存到压缩 JSONL，包含完整候选、排名/分数、选择原因、证据、用量和阶段延迟。故障立即停止付费批次，保留已执行记录。正常与降级分开统计，延迟包含所有已记录请求并标注样本数；P50/P95 是当前网络观察，不是生产 SLA。

文档 Recall/MRR 保留 Phase 4 口径：先取 5 块、映射文档、稳定去重，不补取。新增证据 Recall@5、候选池证据召回、选择后必要证据覆盖和全部齐全率；多组等价证据取覆盖最好的一组，不重复奖励同一必要证据。无答案不进入正例 Recall 分母，“返回证据”只记为拒答门槛代理指标，不当成模型已错误作答。

另预先选定 9 道生成审阅题，所有组使用相同真实聊天模型、温度、提示词和原始问题；从已记录候选重新按冻结门槛选择，避免 Router 成为隐含变量。生成保留原文，并输出隐藏组标签的审阅文件；同一编码助手知道材料背景，不能声称独立盲评。端到端 Router 与审批在单独真实场景和自动化回归中验证。

## 6. 结果、默认选择与限制

### 6.1 检索对照

dev 为 60 道可回答题、8 道无答案题；heldout 为 52 道可回答题、8 道无答案题。七组共完成 **896 次真实检索**（dev 476 + heldout 420），批量中错误、超时和降级均为 0。只描述本次样本，不能估计生产可靠性。dev 门槛选择后离线重放；heldout 首次结果未改写。完整分母、每题原文和阶段时间见 `comparison.json` 与两份原始 `.jsonl.gz`。

冻结源码指纹按文件字节计算。仓库通过 `.gitattributes` 保留 `src/rag/*.py` 的既有换行字节，避免不同平台 checkout 自动转换 LF/CRLF 后误报源码变化；没有为发布修改冻结记录或检索逻辑。

| 方案 | 冻结门槛 | dev 必要证据齐全 | heldout 证据 Recall@5 | heldout 必要证据齐全 | 无答案仍返回证据 / 8 | heldout P50 / P95 秒 |
| --- | --- | --- | --- | --- | --- | --- |
| 同语料旧 Dense5 | 0.65 | 90.00% | 100.00% | 44/52，84.62% | 3 | 1.791 / 1.991 |
| Dense20 | 0.65 | 90.00% | 100.00% | 44/52，84.62% | 3 | 1.800 / 2.252 |
| Dense40 | 0.65 | 90.00% | 100.00% | 44/52，84.62% | 3 | 1.814 / 2.457 |
| BM25 | 8 | 96.67% | 98.08% | 49/52，94.23% | 4 | 0.021 / 0.024 |
| Hybrid | 0 | 98.33% | 99.04% | 51/52，98.08% | 8 | 1.791 / 2.205 |
| Hybrid + Reranker | 0.3 | 68.33% | 100.00% | 36/52，69.23% | 1 | 1.990 / 2.371 |
| Dense40 + Reranker | 0.3 | 68.33% | 100.00% | 36/52，69.23% | 1 | 2.005 / 2.186 |

所有组 heldout 文档 Recall@5 和候选池必要证据召回均为 100%。Dense20 与旧 Dense5 质量相同，扩大至 40 也没有提升，不能将候选扩容包装为召回收益。Hybrid 的齐全率比 Dense20 高 13.46 个百分点，但无答案返回证据更多；该指标不等于模型已错误作答。

heldout Hybrid 并集为 20–34 块，均值 28.65；Dense40 对照和消融每题 40 块。BM25 平均 19.1 块。Dense20 必要块有 11 次被门槛排除；Hybrid 1 次被最终 Top 5 排除；Hybrid Reranker 22 次被门槛排除。本次没有必要块因字符预算流失。数字是必要块排除事件数，不是失败题数；旧 Dense5 未提供新 selection 元数据。

Reranker 提高了证据 MRR（0.9615，对比 Dense20 0.9006），但门槛损失抵消排序收益。P1 多问是明确案例：dev 原始问题下优先级说明块 Dense 分数约 0.6663，可通过 Dense 门槛；rerank 分数约 0.196，被 0.3 删除，而审批边界块保留。不是“没有召回 P1 文档”。

**默认仍选 Dense20**，选择在 heldout 运行前已经写入冻结文件：按 dev 证据齐全和无答案排除率的预设平衡分选择，在与旧 Dense5、Dense40 质量持平时采用统一可观测的新路径且控制候选数。选择理由不是质量提升；原 Dense5 仍是合理的低复杂度选项。没有因为 heldout 的 BM25 平衡分更高而重新挑默认，也不根据 9 道生成样本改门槛。

延迟是 Windows 本地 ES 对比欧洲 Qdrant Cloud、北京模型服务的观察，BM25 的本地网络优势不能全部归因于算法。组顺序逐题轮换，首请求为冷客户端、之后复用连接，未清除服务器缓存。期间共享机器有文档编辑和聊天回归，非隔离性能实验；HTTP 自动重试上限 3，报告按逻辑请求计量，未单独记录每次底层尝试。故障实验单列，不与正常检索平均。

### 6.2 最终回答与业务回归

9 道预选问题 × 7 组，63 个回答，固定 `deepseek-v4-pro`、温度 0、最大输出 1600 tokens、相同提示词。每组 7 道可答（共 16 个请求要点）、2 道无答案；P1 的 SLA 不可答部分要求明确说明资料边界。完整表示请求要点齐全，是否另有无依据陈述单列。

| 方案 | 有据要点 / 16 | 完整回答 / 9 | 含无依据陈述 / 9 | 整题错误拒答 / 7 | 无答案错误作答 / 2 | 编号校验拒绝 / 9 |
| --- | --- | --- | --- | --- | --- | --- |
| 旧 Dense5 | 13 | 8 | 0 | 1 | 0 | 0 |
| Dense20 | 13 | 8 | 0 | 1 | 0 | 0 |
| Dense40 | 13 | 8 | 0 | 1 | 0 | 0 |
| BM25 | 15 | 8 | 0 | 1 | 0 | 1 |
| Hybrid | 16 | 8 | 0 | 0 | 0 | 1 |
| Hybrid + Reranker | 11 | 6 | 1 | 1 | 0 | 1 |
| Dense40 + Reranker | 11 | 6 | 2 | 1 | 0 | 1 |

回答全部保存并由编码助手按来源逐条核对，不是独立人工盲评。编号不合法时后端替换为拒答提示，降低误输出风险但损失可用性。首次生成报告保留的是校验后最终原文，未保存校验前模型文本，不能据此推断被拒回答的具体内容；脚本已为后续新实验补上原始模型输出字段，未覆盖首次结果。

两个重排序组在 SMTP 550/451 案例中只保留错误解释块，仍补写下一步处理，属于当前证据不支持的陈述；不能用知识库其他未提供分块事后辩护。Dense40 + Reranker 还出现把 Git 用户配置与认证账号混用的扩展。无答案样本没有编造金额/小时数，但 Hybrid 的停车题被编号校验替换，所以不能声称所有无答案回答都完美。

真实 Agent 的 11 个场景（13 轮）结构断言通过，工单审批业务 6/6 通过。逐场景见 [Agent 语义审阅](../evaluation/results/phase5/agent-answer-review.md)：**P1 短问仍不完整；U 盘冲突回答自行增加“暂按更严格规则执行”，属于无依据扩展。** L5-02 虽正确拒绝绕过审批，但 Router 实际检索的是审批流程；恶意块的摄入测试另见 7 组生成回答。保留失败，不将结构通过率当作语义通过率。

### 6.3 费用、检查与复现

本阶段成功 API 报告用量估价合计 **2.091581 元**，低于用户授权的 50 元。明细覆盖导入、重复复用、联通、故障、两轮检索、生成、Agent 与工单回归；派生汇总及原始日志没有重复计费。按 Embedding 0.5 元/百万输入、Reranker 0.8 元/百万输入、聊天高峰缓存未命中输入 9 元/百万和输出 27 元/百万保守估价，不扣免费额度、缓存或节假日优惠。账户余额、失败请求未报告用量及原有 Qdrant 套餐固定费用不可由 API 用量证明；不是供应商账单。

验证结果：`pytest -q` **323 passed / 4 skipped**（4 项既有 Docker 测试需要显式 `--run-docker`），Ruff 检查和格式、Pyrefly（0 错误）、离线锁文件检查通过。真实 ES 使用 Windows 官方包验证；Docker/Compose 构建仍未执行，不能列为通过。生成审阅和独立人工标注复核仍有非独立性限制。以下命令均在项目根目录和已安装依赖的 Python 环境执行，真实模型命令收费且必须使用新文件名：

```powershell
python scripts/evaluate_generation.py --freeze evaluation/results/phase5/frozen-v1.json --retrieval evaluation/results/phase5/dev-v1.jsonl.gz evaluation/results/phase5/heldout-v1.jsonl.gz --output evaluation/results/phase5/generation-new.json
python scripts/run_phase5_agent.py --freeze evaluation/results/phase5/frozen-v1.json --group dense20 --output evaluation/results/phase5/agent-new.json
python scripts/run_phase5_agent.py --freeze evaluation/results/phase5/frozen-v1.json --group dense20 --extended --output evaluation/results/phase5/agent-extended-new.json
python scripts/run_phase5_business.py --output evaluation/results/phase5/business-new.json
# Offline only: rebuild first-run comparison, answer-review aggregation and ledger
python scripts/summarize_phase5.py
```

当前独立脚本每批有逻辑请求/输出 tokens 限额和 5 元预算余量；不是跨并发进程的原子费用账本，重跑建议串行。`summarize_phase5.py` 针对本次固定命名的原始资产；新增批次需显式纳入费用明细，不能认为历史脚本输出目录自动等同于供应商总账。

### 6.4 结论边界与后续方向

本阶段完成的是可比较、可追溯的检索实现和实验，并没有消除所有回答质量问题。尚待独立人工复核标注；题目共享小型合成知识库，不能宣称真实企业上线收益。冻结 RRF 网格最高 0.025，不能有效区分部分弱相关双路命中，因此当前 Hybrid 拒答结论只适用于这组设置，不代表 RRF 的最佳可达效果。未扫描所有候选数、分块或标题权重。

P1 短问、冲突临时规则、编号校验引起的错误拒答和不受证据支持的合理化补写已经成为后续回归资产。邻近分块补充、按子问题检查证据及更严格语义支持性校验应作为独立实验；若据此改动提示词/检索，需要新未使用问题支持泛化结论，不能继续把本次 heldout 当盲测。

2026-10-01 官方价格页仍列 text-embedding-v3 为 0.5 元/百万输入 tokens，但同时注明北京免费额度用完后不可调用、推荐 v4。当前 v3 实测可用；这里的金额只用于统一估价，不证明账户剩余额度或未来可用性。更换 Embedding 必须重建契约匹配的索引，不能因接口错误自动换模型混用向量。

## 7. 官方参考

- [Elasticsearch Windows 安装和自带 JDK](https://www.elastic.co/docs/deploy-manage/deploy/self-managed/install-elasticsearch-with-zip-on-windows)
- [Elasticsearch Docker 安装](https://www.elastic.co/docs/deploy-manage/deploy/self-managed/install-elasticsearch-with-docker)
- [Smart Chinese 插件](https://www.elastic.co/docs/reference/elasticsearch/plugins/analysis-smartcn)
- [DashScope 文本排序接口](https://help.aliyun.com/zh/model-studio/text-rerank-api)
- [排序模型限制](https://help.aliyun.com/en/model-studio/rerank)
