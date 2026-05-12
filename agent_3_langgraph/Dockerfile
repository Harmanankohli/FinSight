FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir \
    langgraph \
    langchain-core \
    yfinance \
    pandas \
    numpy \
    scipy \
    a2a-sdk \
    starlette \
    uvicorn \
    httpx

COPY agent_3_langgraph/ ./agent_3_langgraph/
COPY shared/ ./shared/

ENV HOST="0.0.0.0"

EXPOSE 8003

CMD ["uvicorn", "agent_3_langgraph.server:app", "--host", "0.0.0.0", "--port", "8003"]
