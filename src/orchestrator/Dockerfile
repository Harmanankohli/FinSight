FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml .
RUN pip install --no-cache-dir ".[orchestrator]"

COPY src/orchestrator/ ./orchestrator/
COPY src/shared/ ./shared/

RUN useradd -r -u 1001 appuser && chown -R appuser /app
USER appuser

ENV HOST="0.0.0.0"

EXPOSE 8001

CMD ["python", "-m", "orchestrator.main"]
