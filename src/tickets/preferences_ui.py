import streamlit as st


def render_preferences(client, user_id):
    with st.sidebar.expander("回复偏好与我的工单"):
        st.caption(
            "当前为演示身份，由浏览器地址中的用户 ID 声明；不是登录认证。新建会话保留用户 ID。"
        )
        try:
            current = client.support_preferences(user_id)
            if current["status"] == "unavailable":
                st.warning("偏好存储暂不可用，回答使用默认表达。")
            else:
                value = current.get("preferences")
                st.caption(
                    "已保存："
                    + {"zh": "中文", "en": "English"}[value["language"]]
                    + " · "
                    + {"concise": "简洁", "detailed": "详细"}[value["detail"]]
                    if value
                    else "尚未保存偏好"
                )
                language = st.selectbox(
                    "回复语言",
                    ["zh", "en"],
                    format_func=lambda x: {"zh": "中文", "en": "English"}[x],
                    index=0 if not value or value["language"] == "zh" else 1,
                )
                detail = st.selectbox(
                    "详细程度",
                    ["concise", "detailed"],
                    format_func=lambda x: {"concise": "简洁", "detailed": "详细"}[x],
                    index=0 if not value or value["detail"] == "concise" else 1,
                )
                if st.button("保存回复偏好"):
                    client.support_preferences(user_id, {"language": language, "detail": detail})
                    st.success("回复偏好已保存，新会话也会使用；本轮明确要求优先。")
                if st.button("删除回复偏好"):
                    client.support_preferences(user_id, delete=True)
                    st.success("偏好已删除；历史消息、检查点和工单不会被删除。")
            if st.button("查看我的演示工单"):
                rows = client.support_tickets(user_id)["tickets"]
                if not rows:
                    st.info("当前用户没有已创建的业务工单。")
                for row in rows:
                    st.write(f"{row['ticket_id']} · {row['draft']['title']} · {row['state']}")
                st.caption("业务数据库中的演示记录，未接入真实企业系统；最多显示最近 20 条。")
        except Exception:
            st.error("偏好或工单服务暂不可用，本次变更未获确认。")
