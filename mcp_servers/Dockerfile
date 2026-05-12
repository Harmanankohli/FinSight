FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir \
    mcp>=1.0.0 \
    yfinance \
    pandas \
    numpy \
    httpx \
    beautifulsoup4 \
    lxml \
    praw \
    vaderSentiment

COPY mcp_servers/ ./mcp_servers/
COPY shared/ ./shared/
