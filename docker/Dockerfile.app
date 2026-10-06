FROM python:3.13.14-slim
WORKDIR /app
ENV UV_PROJECT_ENVIRONMENT=/usr/local/ UV_COMPILE_BYTECODE=1 UV_NO_CACHE=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/app/src
COPY pyproject.toml uv.lock ./
RUN pip install --no-cache-dir uv==0.12.5 && uv sync --frozen --only-group client
RUN useradd --create-home --uid 10001 app
COPY src/client/ ./src/client/
COPY src/schema/ ./src/schema/
COPY src/tickets/ ./src/tickets/
COPY src/rag/ ./src/rag/
COPY src/voice/ ./src/voice/
COPY src/streamlit_app.py ./src/
COPY scripts/phase9_ui_smoke.py ./scripts/
USER 10001:10001
CMD ["streamlit", "run", "src/streamlit_app.py", "--server.address=0.0.0.0", "--server.runOnSave=false", "--browser.gatherUsageStats=false"]
