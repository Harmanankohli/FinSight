FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml .
RUN pip install --no-cache-dir ".[quant]"

COPY agent_3_langgraph/ ./agent_3_langgraph/
COPY shared/ ./shared/

RUN useradd -r -u 1001 appuser && chown -R appuser /app
USER appuser

ENV HOST="0.0.0.0"

EXPOSE 8003

CMD ["uvicorn", "agent_3_langgraph.server:app", "--host", "0.0.0.0", "--port", "8003"]
