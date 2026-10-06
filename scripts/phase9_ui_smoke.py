"""Run Streamlit's real script against the container service using its client."""

from streamlit.testing.v1 import AppTest

app = AppTest.from_file("src/streamlit_app.py", default_timeout=60).run()
assert not app.exception
app.chat_input[0].set_value("CI_KNOWLEDGE").run()
assert not app.exception
assert any("MFA" in row.value for row in app.markdown)
print('{"streamlit_script_to_service": "passed", "browser_websocket": "not_tested"}')
