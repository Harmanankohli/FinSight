FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml .
RUN pip install --no-cache-dir ".[analytics]"
COPY src/analytics/ ./analytics/
COPY src/shared/ ./shared/
RUN useradd -r -u 1001 appuser && chown -R appuser /app
USER appuser
ENV HOST="0.0.0.0"
EXPOSE 8005
CMD ["uvicorn", "analytics.server:app", "--host", "0.0.0.0", "--port", "8005"]
