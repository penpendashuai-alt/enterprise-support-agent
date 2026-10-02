# Phase 6 真实模型审阅

本记录由同一编码助手逐条阅读实际输出后完成，不是独立人工盲评。用于检验本阶段偏好、业务边界和必要回归，不作为大规模语义准确率。

## 首轮：live.json

| 场景 | 结论 | 实际观察 |
| --- | --- | --- |
| saved_english | 失败，后续已修复 | 已保存英文，回答仍为中文 |
| same_user_new_thread | 失败，后续已修复 | 新线程同样忽略英文偏好 |
| current_request_overrides | 本次符合要求 | 本轮明确中文及三个要点，实际输出中文三点；单独不能证明偏好优先级正确 |
| deleted_not_revived | 通过 | 删除后旧线程的 DHCP 回答为中文默认表达；存储与注入单测共同验证未复活 |
| approval_boundary | 通过 | 先输出未提交草稿；服务端结构化批准后创建 PostgreSQL 工单 |
| business_ticket_query | 通过 | 查询返回相同编号与原始字段，明确业务演示库、未提交真实企业系统 |
| dense20_vpn809 | 通过本轮检查 | 检索到 VPN 809 指南，引用有据；明确状态是 Mock，LAPTOP-001 格式不符，没有猜造设备 |
| known_p1_short_question | 已知失败继续保留 | 仅保留 ticket-priority 单块证据；回答没有编造分钟级 SLA，但缺少 P1 判定，不能算完整回答 |
| mock_service_source | 通过 | operational 明确标注固定模拟数据，不声称实时状态 |

首轮共 20 次聊天模型调用，21,723 输入 token、2,571 输出 token，Embedding 22 token，保守估价 0.264935 元。

## 偏好修复：live-preferences-retry.json

端到端确定性测试确认 Store 偏好确实进入模型上下文，问题在于新偏好与原默认“中文简洁”表达指令冲突。只增强偏好注入的优先级说明：保存偏好覆盖默认表达；提问语言本身不等于用户明确修改偏好。未修改知识检索实现、冻结参数或原 HANDLER_PROMPT 常量。

| 场景 | 结论 | 实际观察 |
| --- | --- | --- |
| saved_english | 通过 | DNS 问题用简洁英文回答 |
| same_user_new_thread | 通过 | 新线程 VPN 问题用简洁英文回答，未继承旧草稿 |
| current_request_overrides | 通过 | 保存英文时，本轮明确中文详细三点，实际输出中文三点 |
| deleted_not_revived | 通过 | 删除后在旧线程回答 DHCP，使用默认中文 |

复测增加 0.083241 元，**本阶段累计保守估价 0.348176 元，授权上限 10 元**。按 DeepSeek V4 Pro 高峰未缓存 9 元/百万输入、27 元/百万输出和 Embedding 0.5 元/百万 token 估算；未扣除缓存、空闲时段或免费额度，非供应商账单。原始 token 用量见两份 JSON。原生 PostgreSQL 与确定性存储验收不调用付费模型。

Phase 5 冲突制度回答可能擅自扩展临时规则的问题本轮未重测、未修复，继续引用原失败记录；不得将存储验收或本次 9 个小样本推广为该问题已解决。
