# 五分钟企业支持演示

本演示为确定性模式：真实 Streamlit 浏览器、FastAPI、LangGraph、PostgreSQL、Redis 和 Qdrant；聊天和向量使用显式测试替代，知识库只有一块合成 MFA 制度。服务状态是模拟数据，工单实际保存到隔离演示数据库。固定示例不代表模型理解任意自然语言，更不代表真实模型质量。

## 启动与准备

安装 Docker Desktop/Engine 与 Compose，按 [README 快速开始](../README.md) 启动 `esa-p10-demo`。首次执行前确认项目名未被其他用途占用；同名再次启动会复用已有演示数据。迁移与 index 容器成功退出是预期，Service/App 应健康。

本次 Windows 验收为了隔离现有端口，使用：

```powershell
$env:SERVICE_PORT = '18120'
$env:STREAMLIT_PORT = '18121'
docker compose --env-file docker/ci.env -p esa-p10-demo -f compose.yaml -f compose.local.yaml -f compose.ci.yaml up -d --build --wait
```

此时访问 <http://127.0.0.1:18121>，Service 为 <http://127.0.0.1:18120>。没有设置端口变量则为 8501/8080。后续 Compose 命令在同一环境执行。`docker/ci.env` 仅含公开测试凭据；不要把此配置作为互联网部署凭据。

选择 support-agent/fake，保留流式输出，先点击 New Chat；用合成身份，保存自己的会话恢复链接。刷新会恢复已持久化消息与待审批草稿，尚未保存的表单输入不会恢复。页面顶部显示确定性模式说明。

## 操作脚本

| 时间 | 操作 | 应看到什么、如何解释 |
| --- | --- | --- |
| 0:00–0:45 | 输入 `演示制度要求什么？`，展开“回答引用” | 回答启用 MFA；引用包含 ci-mfa、ci-mfa-1、ci-v1 和原文。这是一块测试知识，不是正式知识库质量展示 |
| 0:45–1:20 | 输入 `查询服务状态`，再输入 `VPN` | 先要求提供服务名，再运行 query_service_status，显示模拟 operational 状态；观察工具执行中到完成的更新 |
| 1:20–2:00 | 输入 `创建演示工单：VPN 连接失败，影响本人，尚未重启，优先级 P3。` | 出现版本 1 摘要与审批表单；此时还没有创建工单 |
| 2:00–3:00 | 描述改为 `VPN 连接失败，尚未重启。希望管理员先检查配置。`；先试确认，再点击“修改草稿” | 未保存修改时确认会被阻止；保存后显示版本 2，需要重新确认。刷新仍能恢复 v2 |
| 3:00–3:40 | 点击“确认创建” | 出现 DEMO 工单号、open 状态，表单消失。只写演示业务库 |
| 3:40–4:20 | 复制本次工单号，输入 `查询工单 DEMO-实际编号` | 查询业务库中的实际标题/描述/状态；编号不能用截图中的旧编号代替 |
| 4:20–5:00 | 输入 `演示一次模型故障`，刷新页面 | 显示无法可靠识别请求的降级提示，历史仍在；HTTP 成功返回不等于业务任务完成 |

不需要对测试替代进行真实模型评分。未识别的输入可能落入固定 MFA 回答路径，因此不要用本模式测试通用问答或安全拒答能力。需要真实模型时使用独立配置和预算，见 [部署](deployment.md)。

## 幂等的后台核对

批准前通过 `/support-agent/approval?user_id=合成身份&thread_id=会话编号` 读取 pending 的 draft_id 和 draft_version。浏览器批准后，使用同样身份和原批准内容 POST `/support-agent/invoke`：

```json
{
  "user_id": "你的合成身份",
  "thread_id": "本次会话编号",
  "approval": {
    "draft_id": "原草稿编号",
    "draft_version": 2,
    "action": "approve"
  }
}
```

测试栈接口要求 `Authorization: Bearer ci-only-shared-key`。重放应返回同一工单；前后 GET `/support-agent/tickets?user_id=合成身份` 应保持记录数不变。独立数据库核对示例（身份换成本次实际合成身份）：

```sh
docker compose --env-file docker/ci.env -p esa-p10-demo -f compose.yaml -f compose.local.yaml -f compose.ci.yaml exec -T postgres psql -U support_demo -d enterprise_support -Atc "SELECT count(*), min(draft_version), max(draft_version) FROM tickets WHERE user_id='phase10-demo-user';"
```

本次结果为 `1|2|2`，重放前后各 1 条，接口返回相同工单号。该结果只能证明本次同一请求核查，不能外推所有并发情形；更广泛回归见 [审批测试](../tests/service/test_ticket_approval.py) 和 [容器验收](../evaluation/phase9/closeout/README.md)。

## 实际验收与截图

2026-10-06～07 使用内置浏览器完成以上流程。浏览器到 Streamlit 为 WebSocket；AgentClient 到 Service 为 HTTP/SSE。实际操作验证连接和消息/工具状态更新，未做网络抓包；独立 SSE 探针得到 3 个 message、7 个 token 事件与 DONE。AppTest 为附加回归，不替代此次真实浏览器操作。

- [知识引用截图](../media/enterprise-support/knowledge.png)：显示确定性模式与来源。
- [新版草稿截图](../media/enterprise-support/approval.png)：修改后的版本 2 与审批表单，页面顶部模式提示在当前滚动区域之外。
- [浏览器与后台记录](../evaluation/phase10/demo-verification.json)：包含恢复、批准、查询、注入故障与数据库核查。

演示发现并修复：同一草稿更新和 interrupt 两个事件造成摘要重复显示，UI 仅对相邻相同内容去重，保留新版本；固定演示命令的工单号匹配补充大写十六进制，兼容实际创建的 DEMO 编号。未改变审批和数据库业务契约。

## 结束与边界

使用同一项目的 `down` 停止并保留卷；再次 `up` 可恢复。只有明确需要清除本次隔离演示数据时才对该项目加 `--volumes`，不可清理日常数据卷或云端索引。

本次无付费调用，没有正式知识库的新导入、Hybrid 部署或真实 LLM 新演示。完整登录、租户授权、多 worker 和高可用均未实现。复述时将“流程验收通过”与“模型质量指标”分开。
