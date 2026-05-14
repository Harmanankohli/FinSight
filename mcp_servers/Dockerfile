FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir \
    mcp>=1.0.0 \
    yfinance \
    pandas \
    numpy \
    httpx \
    feedparser \
    beautifulsoup4 \
    lxml \
    vaderSentiment \
    sentence-transformers

COPY mcp_servers/ ./mcp_servers/
COPY agent_cards/ ./agent_cards/
COPY shared/ ./shared/

CMD ["uvicorn", "mcp_servers.finsight_server:get_app", "--host", "0.0.0.0", "--port", "8010"]
