#
# SPDX-FileCopyrightText: Copyright (c) 1993-2025 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

# Build stage
FROM python:3.12-slim AS builder

# Install curl for uv installation
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl && \
    curl -LsSf https://astral.sh/uv/install.sh | sh && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

ENV PATH="/root/.local/bin:$PATH"

WORKDIR /app

# Copy dependency files
COPY pyproject.toml uv.lock ./

# Install dependencies
RUN uv sync --frozen --no-dev

# Production stage
FROM python:3.12-slim AS runner

WORKDIR /app

# Create non-root user first (before copying files)
# Using UID 1001 (regular user) without --system flag
RUN useradd --uid 1001 --create-home appuser

# Copy uv from builder
COPY --from=builder /root/.local /root/.local
ENV PATH="/root/.local/bin:$PATH"

# Copy installed dependencies and application with correct ownership
COPY --from=builder --chown=appuser:appuser /app/.venv /app/.venv
COPY --chown=appuser:appuser . .

# Create writable directories for runtime data and set ownership
RUN mkdir -p /app/data/uploads && chown -R appuser:appuser /app/data

USER appuser

# Use /app/data for writable files
ENV CONFIG_PATH=/app/data/config.json
ENV UPLOADS_DIR=/app/data/uploads

EXPOSE 8000

# Run in production mode (no --reload)
CMD ["uv", "run", "--", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]