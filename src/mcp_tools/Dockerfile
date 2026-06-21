FROM python:3.12-slim

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY pyproject.toml uv.lock ./
COPY src/mcp_tools/ ./src/mcp_tools/
COPY src/shared/ ./src/shared/
RUN uv pip install --system --no-cache --extra-index-url https://download.pytorch.org/whl/cpu --index-strategy unsafe-best-match ".[mcp_server]" \
    && useradd -r -u 1001 appuser \
    && chown -R appuser:appuser /app

USER appuser

EXPOSE 8010

CMD ["uvicorn", "mcp_tools.finsight_server:get_app", "--host", "0.0.0.0", "--port", "8010"]
