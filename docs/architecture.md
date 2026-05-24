# RAG Architecture

This document explains how Retrieval-Augmented Generation (RAG) works in this system — from document upload through query answering — and how each component fits together.

## Overview

The RAG implementation enables the chatbot to answer questions using content from uploaded documents. It combines vector similarity search with LLM generation to provide accurate, contextual responses grounded in your data.

Documents are scoped per user: each chunk carries a `user_id` (uploader) and `visibility` ("public" or "private"). Private documents are only visible to their owner; public documents are visible to everyone. The user selects a single active context source, and all queries are strictly filtered to that source — no silent corpus fallback.

```
┌─────────────────────────────────────────────────────────────────┐
│                     HIGH-LEVEL ARCHITECTURE                      │
└─────────────────────────────────────────────────────────────────┘

  ┌──────────┐     ┌──────────┐     ┌──────────┐     ┌──────────┐
  │ Frontend │────▶│ FastAPI  │────▶│ LangGraph│────▶│   LLM    │
  │  (React) │◀────│ Backend  │◀────│  Agent   │◀────│ (KServe) │
  └──────────┘     └──────────┘     └──────────┘     └──────────┘
                         │                │
                    ┌────┴────┐      ┌────┴────┐
                    ▼         ▼      ▼         ▼
              ┌──────────┐ ┌─────┐ ┌──────────┐
              │PostgreSQL│ │Redis│ │  Milvus  │
              │(history, │ │(L2  │ │(vectors) │
              │ sources, │ │cache│ │          │
              │ prefs)   │ │)    │ │          │
              └──────────┘ └─────┘ └──────────┘
```

## Components

| Component | Technology | Purpose |
|-----------|------------|---------|
| Frontend | React 19 + Vite | User interface, document upload, single-select context |
| Backend | FastAPI | REST API, SSE token streaming |
| Agent | LangGraph | Orchestrates inline search + LLM generation |
| Vector DB | Milvus | Stores and searches document embeddings with per-user visibility |
| Embedding | all-MiniLM-L6-v2 (22M, 384-dim) | Converts text to vectors |
| LLM | `nvidia/Llama-3.1-Nemotron-Nano-8B-v1` served by KServe (vLLM runtime, `kserve` namespace) — reached via ExternalName `nemotron-nano-8b` in `rag-agent` ns | Generates responses |
| Storage | PostgreSQL | Chat history (row-per-message), document source metadata, user preferences |
| Cache | Redis (Bitnami HA + Sentinel, shared cluster) | L2 cache and partial-response store on cancel |

---

## End-to-End Walkthrough

### How a query flows from browser to answer

This section traces a single user question through every component, showing how the system ensures the answer comes only from the selected document.

```
┌─────────────────────────────────────────────────────────────────────────┐
│                       COMPLETE QUERY LIFECYCLE                          │
└─────────────────────────────────────────────────────────────────────────┘

 Browser                   Istio Gateway              Backend Pod
 ───────                   ─────────────              ───────────
    │                           │                          │
    │  POST /api/backend-svc/   │                          │
    │  chat/{id}/query          │                          │
    │  Authorization: Bearer    │                          │
    │  ─────────────────────▶   │                          │
    │                           │  JWT validated           │
    │                           │  URL rewrite: strip      │
    │                           │  /api/backend-svc        │
    │                           │  consistentHash on       │
    │                           │  Authorization → same    │
    │                           │  pod for same user       │
    │                           │  ──────────────────────▶ │
    │                           │                          │
    │                           │                    ┌─────┴──────┐
    │                           │                    │  1. Read    │
    │                           │                    │  user prefs │
    │                           │                    │  from PG    │
    │                           │                    │  (selected  │
    │                           │                    │   source)   │
    │                           │                    └─────┬──────┘
    │                           │                          │
    │                           │                    ┌─────┴──────┐
    │                           │                    │  2. Embed   │
    │                           │                    │  query via  │
    │                           │                    │  MiniLM-L6  │
    │                           │                    └─────┬──────┘
    │                           │                          │
    │                           │                    ┌─────┴──────┐
    │                           │                    │  3. Milvus  │
    │                           │                    │  search     │
    │                           │                    │  with expr: │
    │                           │                    │  visibility │
    │                           │                    │  + source   │
    │                           │                    │  filter     │
    │                           │                    └─────┬──────┘
    │                           │                          │
    │                           │                    ┌─────┴──────┐
    │                           │                    │  4. Python  │
    │                           │                    │  enforcement│
    │                           │                    │  (safety    │
    │                           │                    │  net strips │
    │                           │                    │  any leaked │
    │                           │                    │  docs)      │
    │                           │                    └─────┬──────┘
    │                           │                          │
    │                           │                    ┌─────┴──────┐
    │                           │                    │  5. Format  │
    │                           │                    │  context    │
    │                           │                    │  into system│
    │                           │                    │  prompt     │
    │                           │                    └─────┬──────┘
    │                           │                          │
    │                           │                    ┌─────┴──────┐
    │                           │                    │  6. Stream  │
    │                           │                    │  LLM call   │
    │                           │                    │  via KServe │
    │   ◀── SSE: token events ──┼────────────────────│  (vLLM)    │
    │                           │                    └─────┬──────┘
    │                           │                          │
    │                           │                    ┌─────┴──────┐
    │   ◀── SSE: history + done─┼────────────────────│  7. Persist │
    │                           │                    │  to PG + L2 │
    │                           │                    └────────────┘
```

#### Step 1: Read user preferences

The agent reads the user's `selected_sources` from the `user_preferences` table in PostgreSQL. This is a direct DB read on every query — no stale cache.

```python
prefs = await self.conversation_store.get_user_preferences(user_id)
sources = prefs.get("selected_sources") or []
```

The frontend enforces single-select (radio buttons), so `sources` is always a list with zero or one element.

#### Step 2: Embed the query

The user's question is embedded using all-MiniLM-L6-v2 (384-dim, running on CPU) to produce a query vector for similarity search.

#### Step 3: Milvus vector search with source filter

The vector store builds a compound Milvus filter expression:

```python
filter_expr = _build_visibility_filter(user_id)
# → (visibility == "public" || visibility == "" || user_id == "<user>")

if selected_sources:
    quoted = ", ".join(f'"{s}"' for s in selected_sources)
    filter_expr = f"({filter_expr}) && source in [{quoted}]"
```

This produces a filter like:
```
(visibility == "public" || visibility == "" || user_id == "user@example.com")
  && source in ["resume.pdf"]
```

The search retrieves the top-k=5 candidate chunks using HNSW/COSINE similarity, then drops any below `RELEVANCE_SCORE_THRESHOLD` (default 0.4).

#### Step 4: Python-side source enforcement

After the relevance threshold filter, a Python-side safety net enforces the source constraint:

```python
if selected_sources:
    allowed = set(selected_sources)
    above_threshold = [d for d in above_threshold if d.metadata.get("source") in allowed]
```

If Milvus's `expr` filter silently fails (e.g., due to a langchain_milvus version quirk), leaked documents are stripped and a warning is logged. This is defense-in-depth — the Milvus filter should handle it, but the Python check guarantees it.

#### Step 5: Format context into system prompt

Retrieved chunks are wrapped in `<document>` tags with source attribution. Content is sanitized to prevent prompt injection:

```xml
<document index="1" source="resume.pdf">
  chunk content here...
</document>
```

If no documents pass the filters, the system prompt tells the LLM it has no context, and the model responds accordingly ("I couldn't find information about that in your uploaded documents").

#### Step 6: Stream LLM response via KServe

The system prompt (with embedded document context) and user question are sent to the KServe InferenceService:

```
Backend Pod
    │
    │  POST http://nemotron-nano-8b:8000/v1/chat/completions
    │       (ExternalName → nemotron.kserve.svc.cluster.local)
    │
    ▼
KServe InferenceService (kserve namespace)
    │  vLLM runtime
    │  nvidia/Llama-3.1-Nemotron-Nano-8B-v1
    │  temperature=0, top_p=1, stream=True
    │
    ▼
Token stream back to backend → SSE events to browser
```

The backend constructs `http://{selected_model}:8000/v1` where `selected_model` (e.g., `nemotron-nano-8b`) resolves via an ExternalName Service to the KServe endpoint in the `kserve` namespace. This decouples model serving from the application — the backend doesn't know or care that KServe/vLLM is behind the URL.

#### Step 7: Persist and close

After streaming completes, the agent appends the user message and assistant response to the `messages` table in PostgreSQL (one row per message), writes through to the Redis L2 cache, and emits final SSE events (`history`, `done`) to the frontend.

---

## Document Ingestion Pipeline

When a user uploads documents, they go through this processing pipeline:

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│   Upload    │────▶│    Parse    │────▶│    Chunk    │────▶│    Embed    │
│   Files     │     │  Documents  │     │    Text     │     │   & Store   │
└─────────────┘     └─────────────┘     └─────────────┘     └─────────────┘
```

### 1. File Upload

**Endpoint:** `POST /ingest`

Files are uploaded via multipart form with a `visibility` field ("public" or "private", default "private") and processed asynchronously:

```python
@app.post("/ingest")
async def ingest_files(
    files: List[UploadFile],
    visibility: str = Form("private"),
):
    task_id = str(uuid.uuid4())
    background_tasks.add_task(process_and_ingest_files_background, ...)
    return {"task_id": task_id, "status": "queued"}
```

Files are streamed to disk in 64KB chunks with size limits enforced mid-stream. Magic byte validation rejects files whose content doesn't match their extension.

### 2. Document Parsing

Uses multiple parsers with fallbacks:

```
Primary:   PyPDF (for PDFs)
Fallback:  UnstructuredLoader (format-agnostic)
Final:     Raw text read
```

### 3. Text Chunking

Documents are split using `RecursiveCharacterTextSplitter`:

```python
self.text_splitter = RecursiveCharacterTextSplitter(
    chunk_size=1000,      # Characters per chunk
    chunk_overlap=200     # Overlap between chunks
)
```

- **1000 chars**: Fits comfortably in LLM context while retaining meaning
- **200 overlap**: Preserves context across chunk boundaries
- **Recursive splitting**: Respects semantic boundaries (paragraphs, sentences, words)

### 4. Embedding and Storage

Each chunk is embedded using all-MiniLM-L6-v2 and stored in Milvus with per-user metadata:

```python
for chunk in splits:
    chunk.metadata["user_id"] = user_id
    chunk.metadata["visibility"] = visibility  # "public" or "private"
    chunk.metadata["source"] = source_name     # per-file, not per-batch
```

Source metadata is also recorded in PostgreSQL (`document_sources` table) so the frontend can list available sources with ownership badges.

### 5. Source Registration

After indexing, each file is registered in PostgreSQL:

```python
await postgres_storage.add_document_source(
    source_name=file_name,
    user_id=user_id,
    visibility=visibility,
    chunk_count=chunk_count,
)
```

The `/sources` endpoint returns all sources visible to the caller (public + their own private), with each source tagged as `"public"` or `"private"` and a `can_delete` flag based on uploader identity.

---

## Source Selection

Users select a single document source as their active context using radio buttons in the sidebar:

```
┌────────────────────────────────────────┐
│           Select Context               │
├────────────────────────────────────────┤
│ ○ annual_report_2024.pdf   [public]   │
│ ● resume.pdf               [private]  │
│ ○ product_specs.docx       [public]   │
└────────────────────────────────────────┘
```

The selected source is stored per-user in the `user_preferences` table in PostgreSQL (not a local config file). When the user selects a different source, the frontend POSTs a single-element list to `/selected_sources`, and the next query reads it fresh from the database.

**Private vs Public badges:**
- **Private**: Only the uploader can see and query this document
- **Public**: Visible to all users
- The delete button appears based on `can_delete` (uploader identity), independent of visibility

**Strict filtering — no corpus fallback:**

When a source is selected, the Milvus filter restricts results to that source only. If the selected source has no relevant chunks for the query, the LLM receives empty context and responds accordingly — it does not silently search other documents.

---

## Agent Graph

The LangGraph agent runs the RAG workflow in a single node:

```
    ┌─────────────┐
    │    START    │
    └──────┬──────┘
           │
           ▼
    ┌─────────────┐
    │  generate   │  ← inline vector search + LLM call
    └──────┬──────┘
           │
           ▼
    ┌─────────────┐
    │     END     │
    └─────────────┘
```

The `generate` node performs the full RAG pipeline in one pass:
1. Read selected source from user preferences (PostgreSQL)
2. Embed query and search Milvus with visibility + source filter
3. Enforce source constraint in Python (safety net)
4. Format retrieved context with source attribution
5. Render system prompt with context
6. Stream LLM response via KServe back to client
7. Persist messages to PostgreSQL + write-through to Redis L2

---

## Streaming Architecture

Responses stream token-by-token to the frontend via Server-Sent Events:

```python
# main.py - SSE endpoint
@app.post("/chat/{chat_id}/query")
async def stream_chat_query(...):
    async def event_stream():
        async for event in agent.query(query_text, chat_id, user_id):
            yield _sse(event)
    return StreamingResponse(event_stream(), media_type="text/event-stream")
```

**Event types:**

| Event | Description |
|-------|-------------|
| `node_start` | Generate node begins execution |
| `node_end` | Generate node completes |
| `token` | Streamed LLM token |
| `usage` | Token usage stats (prompt, completion, total) |
| `history` | Full conversation history (authoritative, replaces client state) |
| `done` | Stream complete |
| `error` | Error notification |

---

## Pod Loss and Conversation Resilience

Every layer is designed so that losing a backend pod — rolling deploy, KEDA scale-down, crash, or node eviction — does not lose conversation state.

```
                    Pod dies
                       │
      ┌────────────────┼────────────────┐
      ▼                ▼                ▼
 Mid-stream         Between          Cold start
 (generating)       queries          (new pod)
      │                │                │
      ▼                ▼                ▼
 Partial saved     Nothing lost     L1 empty,
 to Redis          (already in      L2 (Redis)
                   PG + L2)         warms L1
```

### Why nothing is lost

| State | Stored in | Survives pod loss? |
|-------|-----------|-------------------|
| Completed messages | PostgreSQL (`messages` table, one row per message) | Yes — persisted immediately after each turn |
| Recent messages (hot cache) | L1 in-process LRU (per pod) | No — but L2 has them |
| Recent messages (warm cache) | L2 Redis (shared across pods, 8h TTL) | Yes — write-through on every save |
| Partial response (mid-stream) | Redis (`sparkchat:partial:{chat_id}`, 8h TTL) | Yes — buffered tokens written on disconnect |
| User preferences (selected source) | PostgreSQL (`user_preferences` table) | Yes |
| Chat metadata (names) | PostgreSQL (`chat_metadata` table) | Yes |

### SSE makes pod loss cheap

Each query is an independent `POST → SSE stream`. The connection only lasts for that one response — there is no long-lived session pinned to a pod. If the pod dies mid-stream:

1. The backend detects the disconnect and writes the partial response buffer to Redis
2. The frontend shows the partial with a `(stopped)` marker
3. The user's next query goes to any healthy pod (Istio consistent hashing will prefer the same pod, but falls back gracefully)
4. The new pod reads conversation history from L2 (Redis) or PostgreSQL — L1 warms on first read

### KEDA scaling

KEDA scales the backend from 1–5 replicas based on load. When a new pod comes up, it has an empty L1 cache but reads from L2 (Redis) on first request, warming L1 for subsequent reads. When a pod scales down, completed conversations are already in PostgreSQL and Redis — nothing is lost.

### Istio session affinity

```yaml
# consistentHash on Authorization header
trafficPolicy:
  loadBalancer:
    consistentHash:
      httpHeaderName: authorization
```

Istio hashes on the JWT in the `Authorization` header, routing a user's requests to the same pod whenever possible. This maximizes L1 cache hits. When the hash ring changes (pod added/removed), users may land on a different pod — L2 catches the L1 miss, and L1 warms transparently.

---

## KServe Integration

The LLM is served by a KServe `InferenceService` in the `kserve` namespace, completely decoupled from this application:

```
rag-agent namespace                          kserve namespace
─────────────────                            ────────────────
                                             InferenceService: nemotron
Backend Pod                                    ├─ vLLM runtime
  │                                            ├─ nvidia/Llama-3.1-Nemotron-Nano-8B-v1
  │  http://nemotron-nano-8b:8000/v1           ├─ GPU: nvidia.com/gpu: 1
  │          │                                 └─ OpenAI-compatible /v1/chat/completions
  │          ▼
  │  ExternalName Service
  │  nemotron-nano-8b.rag-agent.svc
  │    → nemotron.kserve.svc.cluster.local
  │          │
  │          ▼
  └────────► KServe predictor pod (vLLM)
```

**Why KServe:**
- **Decoupled lifecycle**: Model serving is managed independently of the application. Changing the model, tuning vLLM flags, or adjusting GPU memory doesn't require an app deploy.
- **GPU sharing path**: KServe InferenceServices can share the GPU via time-slicing or MPS once configured at the GPU Operator level.
- **Scale-to-zero**: Infrequently used models can scale down, freeing GPU memory.
- **Standard API**: The backend uses the OpenAI-compatible `/v1/chat/completions` endpoint — any model behind that API is a drop-in replacement.

**ExternalName bridging**: The backend constructs `http://{selected_model}:8000/v1` using the model name from the `MODELS` env var. An ExternalName Service in `rag-agent` namespace maps this short name to the KServe service in `kserve` namespace, so no cross-namespace URL is hardcoded in the application.

---

## Data Storage

### PostgreSQL Schema

```sql
-- Chat sessions
CREATE TABLE conversations (
    chat_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, chat_id)
);

-- Messages (one row per message, replaces old JSONB blob)
CREATE TABLE messages (
    chat_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    position INT NOT NULL,
    role TEXT NOT NULL,         -- 'human', 'ai', 'system', 'tool'
    content TEXT NOT NULL,
    PRIMARY KEY (chat_id, position)
);

-- Chat display names
CREATE TABLE chat_metadata (
    chat_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    name TEXT,
    PRIMARY KEY (user_id, chat_id)
);

-- Indexed document sources
CREATE TABLE document_sources (
    source_name TEXT NOT NULL,
    user_id TEXT NOT NULL,
    visibility TEXT NOT NULL DEFAULT 'private',  -- 'public' or 'private'
    file_path TEXT,
    task_id TEXT,
    chunk_count INT DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (source_name, user_id)
);

-- Per-user preferences (selected source, current chat)
CREATE TABLE user_preferences (
    user_id TEXT PRIMARY KEY,
    selected_sources TEXT,       -- JSON array, e.g. '["resume.pdf"]'
    current_chat_id TEXT,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);
```

### Milvus Collection

```python
# Collection: "context"
# Schema fields (fixed):
fields = [
    FieldSchema("pk", DataType.INT64, is_primary=True, auto_id=True),
    FieldSchema("vector", DataType.FLOAT_VECTOR, dim=384),
    FieldSchema("text", DataType.VARCHAR, max_length=65535),
    FieldSchema("source", DataType.VARCHAR, max_length=500),
    FieldSchema("file_path", DataType.VARCHAR, max_length=1000),
    FieldSchema("filename", DataType.VARCHAR, max_length=500),
]
# Dynamic fields (stored in JSON, filterable):
#   user_id: str     — uploader identity (JWT sub)
#   visibility: str  — "public" or "private"
#
# enable_dynamic_field=True
```

**Index:** HNSW with COSINE metric (`M=16`, `efConstruction=256`, search `ef=64`)

---

## Configuration

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `MILVUS_ADDRESS` | Milvus connection string | `tcp://localhost:19530` |
| `POSTGRES_HOST` | PostgreSQL hostname | `localhost` |
| `POSTGRES_DB` | Database name | `chatbot` |
| `MODELS` | Available LLM models (comma-separated) | (required) |
| `RELEVANCE_SCORE_THRESHOLD` | Min relevance score [0-1] for retrieved chunks | `0.4` |
| `MAX_UPLOAD_SIZE_MB` | Maximum file upload size in MB | `50` |
| `MAX_STREAMS_PER_USER` | Max concurrent SSE streams per user | `5` |
| `CACHE_L2_TTL_SECONDS` | Redis L2 cache TTL | `28800` (8h) |
| `LOG_LEVEL` | Logging verbosity | `INFO` |

User preferences (selected source, current chat) are stored per-user in PostgreSQL, not in a config file.

---

## Key Design Decisions

### Why single-select context instead of multi-select?

Multi-select caused confusion: users expected the system to focus on one document but results bled across all selected sources. Single-select (radio buttons) makes the active context explicit — the user knows exactly which document the LLM is answering from.

### Why strict source filtering with no corpus fallback?

The previous design silently searched all documents when the selected source had no relevant hits. This produced answers from unrelated documents, eroding trust. Now, if the selected source has nothing relevant, the LLM says so — which is the correct answer.

### Why Python-side source enforcement on top of Milvus expr?

The Milvus `expr` filter was never independently verified on this codebase (with a single user, the visibility filter never actually excluded anything). The Python safety net guarantees source isolation regardless of whether `langchain_milvus` passes `expr` through correctly, and logs a warning if Milvus leaks documents.

### Why per-user document scoping?

Documents carry `user_id` and `visibility` metadata. Private documents are invisible to other users at the Milvus filter level. This enables multi-tenant use without separate collections.

### Why Milvus?

- Open-source, self-hosted (no cloud dependency)
- Excellent performance for similarity search
- Supports filtering with expressions (visibility + source)
- Dynamic fields for per-user metadata without schema migrations
- HNSW index with COSINE metric

### Why direct RAG pipeline instead of tool calling?

- Eliminates ~20s of overhead from MCP subprocess stdio, LangGraph checkpointing, and multi-iteration state serialization
- Single-pass search + LLM call takes ~3s end-to-end
- Simpler architecture with fewer failure modes

### Why KServe instead of in-repo vLLM Deployment?

- Decoupled lifecycle — model changes don't require app deploys
- Path to GPU sharing (time-slicing, MPS, scale-to-zero)
- Standard InferenceService CRD managed by cluster operators
- See `docs/llm-selection-journey.md` Phase 7 for the full rationale

### Why LangGraph?

- Structured async execution with streaming support
- Clean state management for conversation flow
- Extensible if multi-step workflows are needed later

---

## Performance Considerations

1. **Direct RAG pipeline**: Inline vector search + single LLM call in one pass (~3s end-to-end)
2. **Embedding latency**: all-MiniLM-L6-v2 runs locally on CPU, ~50-100ms per query
3. **Vector search**: Milvus HNSW index with COSINE metric, <10ms for top-k retrieval
4. **Async Milvus ops**: All synchronous pymilvus calls run in `asyncio.to_thread()` to avoid blocking the event loop
5. **No checkpointer overhead**: Graph runs without a MemorySaver since each query is stateless
6. **Row-per-message persistence**: `append_messages()` writes only the new turn's messages, avoiding full-history rewrites
7. **Two-tier caching**: L1 in-process LRU (300s) + L2 Redis (8h) with Istio session affinity for warm L1 hits
8. **Streaming**: Token-by-token SSE delivery with `requestAnimationFrame`-based throttle for smooth rendering
9. **Background ingestion**: Large uploads don't block the UI; status polled via `/ingest/status/{task_id}`
10. **Per-user stream cap**: `MAX_STREAMS_PER_USER` prevents a single user from exhausting backend connections

---

## Extending the RAG System

### Adding a new document loader

```python
# vector_store.py — inside _load_documents()
if file_ext == '.custom':
    docs = CustomLoader(file_path).load()
```

### Changing the embedding model

```python
# vector_store.py
class CustomEmbeddings:
    def __init__(self, model: str = "your-model", host: str = "http://your-host:8000"):
        # Update model and endpoint
```

### Adjusting retrieval parameters

```bash
# Tune the relevance score threshold (0.0 = accept everything, 1.0 = exact match only)
# Check DEBUG logs for per-chunk scores to find the right value for your data
export RELEVANCE_SCORE_THRESHOLD=0.5
```

### Switching the LLM

Change the KServe InferenceService in the `kserve` namespace to serve a different model, update the `served-model-name`, and set `MODELS` in the backend to match. The backend's OpenAI-compatible client works with any model behind `/v1/chat/completions`.
