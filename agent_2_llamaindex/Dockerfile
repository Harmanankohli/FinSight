FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml .
RUN pip install --no-cache-dir -e ".[dev]" || pip install --no-cache-dir \
    llama-index \
    llama-index-vector-stores-chroma \
    llama-index-embeddings-huggingface \
    llama-index-llms-groq \
    chromadb \
    sentence-transformers \
    rank-bm25 \
    a2a-sdk \
    fastapi \
    uvicorn \
    httpx

COPY agent_2_llamaindex/ ./agent_2_llamaindex/
COPY shared/ ./shared/

ENV GROQ_API_KEY=""
ENV EMBED_MODEL="all-MiniLM-L6-v2"
ENV GROQ_MODEL="mixtral-8x7b-32768"
ENV CHROMA_DIR="./chroma_db"
ENV HOST="0.0.0.0"

EXPOSE 8002

CMD ["uvicorn", "agent_2_llamaindex.server:app", "--host", "0.0.0.0", "--port", "8002"]
