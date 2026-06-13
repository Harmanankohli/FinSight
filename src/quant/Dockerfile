FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml .
RUN pip install --no-cache-dir ".[quant]"

COPY src/quant/ ./quant/
COPY src/shared/ ./shared/

RUN useradd -r -u 1001 appuser && chown -R appuser /app
USER appuser

ENV HOST="0.0.0.0"

EXPOSE 8003

CMD ["uvicorn", "quant.server:app", "--host", "0.0.0.0", "--port", "8003"]
