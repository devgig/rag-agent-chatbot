# Spark Chat: A Local RAG Chatbot for DGX Spark

## Project Overview

Spark Chat is a fully local RAG-powered chatbot built for DGX Spark. It uses a direct RAG pipeline — inline vector search followed by a single LLM generation pass — to answer questions grounded in uploaded documents.

The system focuses on document ingestion and retrieval-augmented generation (RAG), allowing users to upload documents and ask questions grounded in their content. All processing runs locally on DGX Spark hardware.

> **Note**: This demo uses the DGX Spark's unified memory, so ensure that no other GPU workloads are running on your Spark using `nvidia-smi`.

This project is designed to be customizable, serving as a framework that developers can extend.

## Key Features
  - **Direct RAG Pipeline**: Inline vector search + single LLM call in one pass (~3s end-to-end)

  - **Swappable Models**: Models are served via an OpenAI-compatible API. Any OpenAI-compatible model can be integrated

  - **Vector Indexing & Retrieval**: Milvus-powered document retrieval with batched embeddings for fast ingestion

  - **Real-time LLM Streaming**: Custom streaming infrastructure with WebSocket auto-reconnection and token batching

  - **JWT Authentication**: Google OAuth with RS256 JWT validation via JWKS

  - **LRU Caching**: Bounded in-memory caches with TTL expiration prevent memory leaks on long-running servers

  - **Same-Origin API Routing**: Backend served behind `/api/backend-svc` on the frontend hostname via Istio Gateway URLRewrite, eliminating CORS entirely in production

  - **Configurable File Limits**: Environment-driven upload size limits for production deployments

  - **Auto-scaling**: KEDA-based backend scaling (1–5 replicas) based on queue depth

## System Overview
<img src="../assets/assets/system-diagram.svg" alt="System Diagram" style="max-width:800px;border-radius:5px;justify-content:center">

## Architecture

### Components

| Component | Technology | Description |
|-----------|------------|-------------|
| **Frontend** | React, Vite, Tailwind CSS, nginx | Chat UI with document upload, source selection, and real-time streaming |
| **Backend** | Python 3.12, FastAPI, LangGraph | REST + WebSocket API handling RAG pipeline, ingestion, and chat management |
| **Embedding** | sentence-transformers (CPU) | Generates vector embeddings for document chunks |
| **LLM** | vLLM (GPU) | Serves the chat model via OpenAI-compatible API |
| **Vector Store** | Milvus | HNSW-indexed vector storage with cosine similarity search |
| **Database** | PostgreSQL | Conversation history, chat metadata, and source tracking |
| **Service Mesh** | Istio Ambient Mesh | Traffic routing, gateway URL rewrite |

### Default Models

| Model | Quantization | Model Type | Weights | Actual Usage | Namespace |
|-------|--------------|------------|---------|--------------|-----------|
| Nemotron 3 Nano 30B | NVFP4 | Chat (MoE) | ~15 GB | ~72 GB GPU, ~4 Gi RAM | `llm` |
| all-MiniLM-L6-v2 | FP32 | Embedding (384d) | ~80 MB | ~332 Mi RAM (CPU only) | `rag-agent` |

**GPU memory:** vLLM pre-allocates ~72 GB via `--gpu-memory-utilization=0.55` for weights + KV cache + CUDA graphs, leaving ~56 GB for OS/K3s. The embedding model runs entirely on CPU.

### Inference Performance

Metrics sourced from vLLM Prometheus endpoint (`/metrics`) on DGX Spark GB10.

#### Time to First Token (TTFT)

Time from request arrival to first generated token. Dominated by prompt prefill.

| Percentile | Latency |
|------------|---------|
| p50 | 100–250 ms |
| p95 | < 750 ms |
| Average | ~1.5 s |

The first request after a cold start may take 20–40s due to CUDA graph warmup.

#### Token Generation Throughput (TPOT)

Sustained output token rate during active generation.

| Metric | Value |
|--------|-------|
| Peak generation | **56.8 tok/s** |
| Sustained generation | **~57 tok/s** |
| Prompt prefill | up to **280 tok/s** |

vLLM logs report 10-second averaged throughput, which dilutes active generation across idle intervals and appears lower than the actual per-request rate.

#### End-to-End Request Latency

Total time from request arrival to final token delivered.

| Percentile | Latency |
|------------|---------|
| p50 | < 1.5 s |
| p90 | < 5 s |
| Average | ~4.2 s |

Average request size: ~1,337 prompt tokens, ~150 generation tokens.

#### Dynamic Batching

vLLM chunked prefill is enabled with `max_num_batched_tokens=2048`, allowing prompt processing and generation to be interleaved across concurrent requests.

| Metric | Value |
|--------|-------|
| KV cache capacity | 3.1M tokens |
| Max concurrent requests (at 16K context) | 184x |
| Peak KV cache usage | 0.1% |

#### Cache & Utilization

| Metric | Value |
|--------|-------|
| Prefix cache hit rate | 0% (enabled, no repeated prefixes observed) |
| Completion reasons | 17 stop, 1 length, 0 errors |
| Total tokens processed | 26,149 prompt + 2,868 generation |

### RAG Pipeline Flow

```
User Query (via WebSocket)
  │
  ├─ 1. Vector Search (inline)
  │     └─ Milvus similarity_search_with_relevance_scores()
  │     └─ Filtered by selected sources, k=5, threshold=0.4
  │
  ├─ 2. Context Formatting
  │     └─ Jinja2 template injects retrieved documents into system prompt
  │
  └─ 3. Single LLM Call (streaming)
        └─ OpenAI-compatible API → vLLM
        └─ temperature=0, top_p=1, stream=True
        └─ Tokens streamed to frontend via WebSocket
  │
  └─ 4. Persist to PostgreSQL
```

### Document Ingestion Flow

```
File Upload (POST /ingest)
  │
  ├─ Validation: file type (.pdf, .txt, .docx, .md, .csv, .json, .html, .rtf, .doc)
  ├─ Parsing: PyPDF, UnstructuredLoader, or raw text fallback
  ├─ Chunking: 1000 chars / 200 char overlap (RecursiveCharacterTextSplitter)
  ├─ Embedding: Batched (32 texts/request) via embedding service
  └─ Indexing: Upserted into Milvus collection with source metadata
```

### Backend API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/ws/chat/{chat_id}` | WebSocket | Real-time chat with streaming responses |
| `/ingest` | POST | Upload and ingest documents |
| `/ingest/status/{task_id}` | GET | Check ingestion progress |
| `/sources` | GET | List available document sources |
| `/delete/sources/{source_name}` | DELETE | Remove a document source |
| `/chats` | GET | List all conversations |
| `/chat/new` | POST | Create a new chat |
| `/chat/rename` | POST | Rename a chat |
| `/chat/delete` | POST | Delete a chat |
| `/health` | GET | Kubernetes health check |

---

## Quick Start

#### 1. Clone the repository
```bash
git clone <repository-url>
cd rag-agent-chatbot
```

#### 2. Deploy to Kubernetes
Build container images, push to your registry, and apply Kustomize manifests. See the [README](../README.md) for detailed deployment instructions.

#### 3. Try it out
Upload a document using the "Upload Documents" button in the sidebar under "Context", select it in the "Select Sources" section, then ask questions about its content.

## Customizations

### Environment Configuration

| Variable | Description | Default |
|----------|-------------|---------|
| `MODELS` | Comma-separated model names | `nemotron-nano` |
| `CORS_ALLOWED_ORIGINS` | Comma-separated allowed origins | `http://localhost:3000` |
| `MAX_UPLOAD_SIZE_MB` | Maximum file upload size in MB | `50` |
| `MAX_TOTAL_UPLOAD_MB` | Total upload limit across all files | `200` |
| `MAX_WS_MESSAGE_BYTES` | WebSocket message size limit | `65536` |
| `MAX_WS_CONNECTIONS_PER_USER` | Max concurrent WebSocket connections per user | `5` |
| `POSTGRES_HOST` | PostgreSQL hostname | `postgres` |
| `MILVUS_ADDRESS` | Milvus connection URI | `tcp://milvus:19530` |
| `RELEVANCE_SCORE_THRESHOLD` | Minimum embedding similarity for retrieval | `0.4` |
| `LOG_LEVEL` | Logging verbosity | `INFO` |

### Customizing the RAG Pipeline

The RAG search logic lives directly in `assets/backend/agent.py` in the `generate()` method. You can customize source filtering, context formatting, and prompt construction there.

### Kubernetes Namespaces

| Namespace | Purpose |
|-----------|---------|
| `rag-agent` | Backend, embedding service, frontend |
| `llm` | LLM model serving (vLLM) |
| `milvus-system` | Vector database |
| `postgres-system` | PostgreSQL |
