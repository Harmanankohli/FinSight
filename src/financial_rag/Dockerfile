FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml .
RUN pip install --no-cache-dir ".[rag]"

COPY src/financial_rag/ ./financial_rag/
COPY src/shared/ ./shared/

RUN useradd -r -u 1001 appuser && chown -R appuser /app
USER appuser

ENV HOST="0.0.0.0"

EXPOSE 8002

CMD ["uvicorn", "financial_rag.server:app", "--host", "0.0.0.0", "--port", "8002"]
