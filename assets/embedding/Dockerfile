# Build stage — install deps and download model in one stage
# to avoid large multi-stage COPY layers that timeout on CI agents
FROM python:3.12-slim AS builder

WORKDIR /app

# IMPORTANT: install torch from the CPU-only wheel index BEFORE pulling
# sentence-transformers. Without this, pip resolves torch from PyPI's
# default index and downloads the GPU build which drags in ~3-4 GB of
# nvidia-cublas, nvidia-cudnn, nvidia-cufft, nvidia-nccl, triton, etc.
# This embedding service is CPU-only (runs on storage nodes with no GPU)
# so all of that is dead weight AND the resulting site-packages tree is
# large enough that the multi-stage COPY times out the Docker daemon
# socket with "error reading from server: EOF".
#
# Pinning torch (rather than letting sentence-transformers pick) protects
# against future torch releases bundling more CUDA libraries.
RUN pip install --no-cache-dir \
        --index-url https://download.pytorch.org/whl/cpu \
        "torch==2.6.0" \
    && pip install --no-cache-dir \
        sentence-transformers==2.3.1 \
        fastapi==0.109.0 \
        uvicorn==0.27.0 \
        pydantic==2.5.3 \
        prometheus-fastapi-instrumentator==7.0.2 \
    && python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')" \
    && cp -r /root/.cache/huggingface /app/.cache_hf

# Production stage
FROM python:3.12-slim AS runner

WORKDIR /app

# Create non-root user
RUN useradd --uid 1001 --create-home appuser

# Copy packages, uvicorn, and pre-downloaded model from builder
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin/uvicorn /usr/local/bin/uvicorn
COPY --from=builder --chown=appuser:appuser /app/.cache_hf /app/.cache_hf

# Copy application code
COPY --chown=appuser:appuser server.py .

USER appuser

ENV HF_HOME=/app/.cache_hf
ENV TRANSFORMERS_OFFLINE=1
ENV HF_HUB_OFFLINE=1

EXPOSE 8000

# Two workers lets us serve embeddings concurrently across CPU cores. The
# event loop in each worker still hands model.encode() to a thread pool so a
# single worker isn't fully serialized either — workers=2 is additional
# capacity on top of that. Each worker loads its own copy of the model
# (~600 MB resident), so total RAM is ~1.2 GB; fits comfortably in the pod's
# 4 Gi memory limit. Drop back to 1 worker if memory pressure ever shows up.
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
