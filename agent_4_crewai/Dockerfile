FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir \
    crewai \
    a2a-sdk \
    starlette \
    uvicorn \
    httpx \
    pyyaml

COPY agent_4_crewai/ ./agent_4_crewai/
COPY shared/ ./shared/

ENV HOST="0.0.0.0"
ENV GROQ_API_KEY=""

EXPOSE 8004

CMD ["uvicorn", "agent_4_crewai.server:app", "--host", "0.0.0.0", "--port", "8004"]
