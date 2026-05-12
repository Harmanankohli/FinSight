FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir \
    google-adk \
    httpx \
    pydantic

COPY agent_1_adk/ ./agent_1_adk/
COPY shared/ ./shared/

ENV AGENT_LLAMAINDEX_URL="http://agent-llamaindex:8002"
ENV AGENT_LANGGRAPH_URL="http://agent-langgraph:8003"
ENV AGENT_CREWAI_URL="http://agent-crewai:8004"

CMD ["python", "-m", "agent_1_adk.orchestrator"]
