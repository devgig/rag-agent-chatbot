#
# SPDX-FileCopyrightText: Copyright (c) 1993-2025 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#

# vLLM inference server for NVIDIA GB10 (Blackwell architecture)
FROM nvcr.io/nvidia/pytorch:25.10-py3

# Install vLLM with Blackwell (sm_121) support
RUN pip install --upgrade pip && \
    pip install vllm

# Set environment variables for GB10 optimization
ENV VLLM_WORKER_MULTIPROC_METHOD=spawn \
    VLLM_USE_MODELSCOPE=False \
    CUDA_VISIBLE_DEVICES=0

WORKDIR /app

# Expose OpenAI-compatible API port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=120s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Default command - will be overridden by deployment with specific model
CMD ["python3", "-m", "vllm.entrypoints.openai.api_server", \
     "--host", "0.0.0.0", \
     "--port", "8000"]
