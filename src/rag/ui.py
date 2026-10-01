import streamlit as st


def render_sources(data: dict):
    if data.get("kind") != "knowledge_answer":
        return
    for title, items in [
        ("回答引用", data.get("citations", [])),
        ("候选资料（未作为回答引用）", data.get("candidates", [])),
    ]:
        if not items:
            continue
        with st.expander(title, expanded=title == "回答引用"):
            for item in items:
                label = f"[{item['number']}] " if item.get("number") else ""
                st.markdown(f"**{label}{item['title']}**")
                source = (
                    "自行编写的演示制度/指南"
                    if item["source_type"] == "synthetic"
                    else "公开技术资料摘要"
                )
                st.caption(f"{source} · 文档版本 {item['document_version']} · {item['location']}")
                st.text(item["text"])
                if item.get("url", "") and item["url"].startswith("https://"):
                    st.link_button("原始公开来源", item["url"])
                st.caption(
                    f"文档 {item['doc_id']} · 分块 {item['chunk_id']} · 索引 {item['index_version'][:12]}"
                )
