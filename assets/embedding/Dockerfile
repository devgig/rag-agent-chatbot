# Build stage
FROM python:3.12-slim AS builder

WORKDIR /app

# Install dependencies
RUN pip install --no-cache-dir \
    sentence-transformers>=3.4.0 \
    fastapi==0.109.0 \
    uvicorn==0.27.0 \
    pydantic==2.5.3 \
    prometheus-fastapi-instrumentator==7.0.2

# Production stage
FROM python:3.12-slim AS runner

WORKDIR /app

# Create non-root user
RUN useradd --uid 1001 --create-home appuser

# Copy installed packages from builder
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin/uvicorn /usr/local/bin/uvicorn

# Copy application code
COPY --chown=appuser:appuser server.py .

# Pre-download the model during build to avoid runtime network dependency
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('Qwen/Qwen3-Embedding-0.6B')" \
    && cp -r /root/.cache/huggingface /app/.cache_hf \
    && chown -R appuser:appuser /app/.cache_hf

USER appuser

ENV HF_HOME=/app/.cache_hf
ENV TRANSFORMERS_OFFLINE=1
ENV HF_HUB_OFFLINE=1

EXPOSE 8000

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
