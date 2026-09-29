from unittest.mock import AsyncMock

import pytest
from streamlit.testing.v1 import AppTest

from schema import AgentInfo, ChatMessage


@pytest.mark.parametrize("action", ["approve", "edit", "cancel"])
def test_approval_form_refresh_and_actions(mock_agent_client, action):
    mock_agent_client.agent = "support-agent"
    mock_agent_client.info.default_agent = "support-agent"
    mock_agent_client.info.agents.append(AgentInfo(key="support-agent", description="Support"))
    payload = {
        "kind": "ticket_approval",
        "draft_id": "draft-" + "1" * 32,
        "draft_version": 1,
        "approval_status": "pending",
        "draft": {
            "title": "VPN",
            "description": "809",
            "service_name": "VPN",
            "impact": "本人",
            "priority": "P1",
        },
    }
    mock_agent_client.aget_pending_approval.return_value = payload
    mock_agent_client.ainvoke = AsyncMock(return_value=ChatMessage(type="ai", content="操作结果"))
    at = AppTest.from_file("../../src/streamlit_app.py", default_timeout=15).run()
    assert not at.exception
    assert {"确认创建", "修改草稿", "取消"} <= {button.label for button in at.button}
    at.run()
    assert mock_agent_client.aget_pending_approval.await_count >= 2
    if action == "edit":
        next(field for field in at.text_area if field.label == "描述").set_value("新的描述")
    label = {"approve": "确认创建", "edit": "修改草稿", "cancel": "取消"}[action]

    async def submit(**kwargs):
        mock_agent_client.aget_pending_approval.return_value = (
            None
            if action != "edit"
            else {
                **payload,
                "draft_version": 2,
                "draft": {**payload["draft"], "description": "新的描述"},
            }
        )
        return ChatMessage(type="ai", content="已完成操作")

    mock_agent_client.ainvoke.side_effect = submit
    next(button for button in at.button if button.label == label).click().run()
    assert not at.exception
    request = mock_agent_client.ainvoke.call_args.kwargs
    assert request["approval"].action == action
    assert request["approval"].draft_version == 1
    assert request["thread_id"] == at.session_state.thread_id
    if action == "edit":
        assert request["approval"].draft.description == "新的描述"
        assert "确认创建" in {button.label for button in at.button}
    else:
        assert "确认创建" not in {button.label for button in at.button}


def test_unsaved_edits_cannot_be_approved(mock_agent_client):
    mock_agent_client.agent = "support-agent"
    mock_agent_client.info.default_agent = "support-agent"
    mock_agent_client.info.agents.append(AgentInfo(key="support-agent", description="Support"))
    mock_agent_client.aget_pending_approval.return_value = {
        "draft_id": "draft-" + "2" * 32,
        "draft_version": 1,
        "approval_status": "pending",
        "draft": {
            "title": "VPN",
            "description": "809",
            "service_name": None,
            "impact": "本人",
            "priority": "P3",
        },
    }
    at = AppTest.from_file("../../src/streamlit_app.py", default_timeout=15).run()
    next(field for field in at.text_area if field.label == "描述").set_value("未保存的修改")
    next(button for button in at.button if button.label == "确认创建").click().run()
    assert not at.exception
    mock_agent_client.ainvoke.assert_not_called()
    assert any("先点击" in warning.value for warning in at.warning)
