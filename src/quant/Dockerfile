FROM python:3.12-slim

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY pyproject.toml uv.lock ./
COPY src/quant/ ./src/quant/
COPY src/shared/ ./src/shared/
RUN uv pip install --system --no-cache --extra-index-url https://download.pytorch.org/whl/cpu --index-strategy unsafe-best-match ".[quant]" \
    && useradd -r -u 1001 appuser \
    && chown -R appuser:appuser /app

USER appuser

ENV HOST="0.0.0.0"
EXPOSE 8003

CMD ["uvicorn", "quant.server:app", "--host", "0.0.0.0", "--port", "8003"]
