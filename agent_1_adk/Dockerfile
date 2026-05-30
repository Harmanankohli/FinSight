FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir \
    google-adk \
    a2a-sdk \
    starlette \
    uvicorn \
    httpx \
    langfuse \
    opentelemetry-api \
    opentelemetry-sdk \
    opentelemetry-instrumentation-starlette \
    opentelemetry-instrumentation-httpx \
    openinference-instrumentation-google-adk \
    python-dotenv \
    aiosqlite \
    sentence-transformers \
    chromadb

COPY agent_1_adk/ ./agent_1_adk/
COPY shared/ ./shared/

ENV HOST="0.0.0.0"

EXPOSE 8001

CMD ["python", "-m", "agent_1_adk.main"]
