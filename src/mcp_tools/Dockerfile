FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml .
RUN pip install --no-cache-dir ".[mcp_server]"

COPY src/mcp_tools/ ./mcp_tools/
COPY src/shared/ ./shared/

RUN useradd -r -u 1001 appuser && chown -R appuser /app
USER appuser

EXPOSE 8010

CMD ["uvicorn", "mcp_tools.finsight_server:get_app", "--host", "0.0.0.0", "--port", "8010"]
