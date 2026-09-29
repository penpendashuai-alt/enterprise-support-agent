import streamlit as st
from pydantic import ValidationError

from client import AgentClient, AgentClientError
from tickets.models import ApprovalInput, TicketDraft


async def render_approval(client: AgentClient, thread_id: str, user_id: str, model: str):
    slot = st.empty()
    with slot.container():
        await _render_approval(client, thread_id, user_id, model, slot)


async def _render_approval(client, thread_id, user_id, model, slot):
    try:
        pending = await client.aget_pending_approval(thread_id)
    except AgentClientError:
        st.error("暂时无法读取工单审批状态，请刷新后重试。")
        return
    if not pending:
        slot.empty()
        return
    draft = pending["draft"]
    st.subheader("待确认的本地演示工单")
    st.caption("仅写入本地演示库，不提交真实企业系统。修改字段后，请先保存新版，再确认创建。")
    result = pending.get("ticket_result") or {}
    if result.get("message"):
        st.warning(result["message"])
    with st.form(f"ticket-{pending['draft_id']}-{pending['draft_version']}"):
        title = st.text_input("标题", value=draft["title"], max_chars=160)
        description = st.text_area("描述", value=draft["description"], max_chars=4000)
        service_name = st.text_input(
            "服务（可空）", value=draft["service_name"] or "", max_chars=100
        )
        impact = st.text_area("影响范围", value=draft["impact"], max_chars=4000)
        priority = st.selectbox(
            "演示优先级（未指定默认 P3，无 SLA 承诺）",
            ["P1", "P2", "P3", "P4"],
            index=["P1", "P2", "P3", "P4"].index(draft["priority"]),
        )
        approve = st.form_submit_button("确认创建", type="primary")
        locked = (
            result.get("status") in {"unknown", "conflict"}
            or pending["approval_status"] == "approved"
        )
        edit = st.form_submit_button("修改草稿", disabled=locked)
        cancel = st.form_submit_button("取消", disabled=locked)
    if not (approve or edit or cancel):
        return
    edited = {
        "title": title,
        "description": description,
        "service_name": (service_name or "").strip() or None,
        "impact": impact,
        "priority": priority,
    }
    if approve and edited != draft:
        st.warning("表单内容已改变，请先点击“修改草稿”保存新版，然后重新确认。")
        return
    try:
        approval = ApprovalInput(
            draft_id=pending["draft_id"],
            draft_version=pending["draft_version"],
            action="approve" if approve else "edit" if edit else "cancel",
            draft=TicketDraft.model_validate(edited) if edit else None,
        )
        response = await client.ainvoke(
            approval=approval, thread_id=thread_id, user_id=user_id, model=model
        )
    except (AgentClientError, ValidationError) as exc:
        st.error(f"审批未完成，请核对字段或刷新状态：{exc}")
        return
    st.session_state.messages.append(response)
    slot.empty()
    st.rerun()
