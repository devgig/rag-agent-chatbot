# RAG Architecture

This document explains how Retrieval-Augmented Generation (RAG) works in this rag-agent chatbot system.

## Overview

The RAG implementation enables the chatbot to answer questions using content from uploaded documents. It combines vector similarity search with LLM generation to provide accurate, contextual responses grounded in your data.

```
┌─────────────────────────────────────────────────────────────────┐
│                     HIGH-LEVEL ARCHITECTURE                      │
└─────────────────────────────────────────────────────────────────┘

  ┌──────────┐     ┌──────────┐     ┌──────────┐     ┌──────────┐
  │ Frontend │────▶│ FastAPI  │────▶│ LangGraph│────▶│   LLM    │
  │  (React) │◀────│ Backend  │◀────│  Agent   │◀────│ (vLLM)   │
  └──────────┘     └──────────┘     └──────────┘     └──────────┘
                         │                │
                         ▼                ▼
                   ┌──────────┐     ┌──────────┐
                   │PostgreSQL│     │  Milvus  │
                   │(history) │     │(vectors) │
                   └──────────┘     └──────────┘
```

## Components

| Component | Technology | Purpose |
|-----------|------------|---------|
| Frontend | React 19 + Vite | User interface, document upload, chat |
| Backend | FastAPI | REST API, WebSocket streaming |
| Agent | LangGraph | Orchestrates inline search + LLM generation |
| Vector DB | Milvus | Stores and searches document embeddings |
| Embedding | all-MiniLM-L6-v2 (22M, 384-dim) | Converts text to vectors |
| LLM | Nemotron 3 Nano 30B MoE NVFP4 (vLLM, `llm` namespace) | Generates responses (~56 tok/s) |
| Storage | PostgreSQL | Chat history, document metadata |

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

Files are uploaded via multipart form and processed asynchronously:

```python
# main.py
@app.post("/ingest")
async def ingest_documents(files: List[UploadFile]):
    task_id = str(uuid.uuid4())
    background_tasks.add_task(process_and_ingest_files_background, ...)
    return {"task_id": task_id, "status": "queued"}
```

### 2. Document Parsing

Uses `UnstructuredLoader` for format-agnostic parsing with fallbacks:

```
Primary:   UnstructuredLoader (PDFs, DOCX, PPT, HTML, etc.)
Fallback:  PyPDF2 (PDF-specific)
Final:     Raw text read
```

### 3. Text Chunking

Documents are split using `RecursiveCharacterTextSplitter`:

```python
# vector_store.py
self.text_splitter = RecursiveCharacterTextSplitter(
    chunk_size=1000,      # Characters per chunk
    chunk_overlap=200     # Overlap between chunks
)
```

**Why these settings?**
- **1000 chars**: Fits comfortably in LLM context while retaining meaning
- **200 overlap**: Preserves context across chunk boundaries
- **Recursive splitting**: Respects semantic boundaries (paragraphs → sentences → words)

### 4. Embedding Generation

Each chunk is embedded using the all-MiniLM-L6-v2 model (22M params, 384-dim):

```python
# vector_store.py
class CustomEmbeddings:
    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        # POST to embedding service (separate pipeline/deployment)
        response = requests.post(
            "http://embedding:8000/v1/embeddings",
            json={"input": texts, "model": "all-MiniLM-L6-v2"}
        )
        return [item["embedding"] for item in response.json()["data"]]
```

### 5. Vector Storage

Embeddings are stored in Milvus with metadata:

| Field | Type | Description |
|-------|------|-------------|
| `pk` | Int64 | Auto-generated primary key |
| `embedding` | FloatVector | 384-dimensional embedding |
| `text` | VarChar | Original text chunk |
| `source` | VarChar | Document filename |
| `file_path` | VarChar | Full file path |

---

## Query Flow

When a user asks a question, here's what happens:

```
┌─────────────────────────────────────────────────────────────────────────┐
│                           QUERY FLOW                                     │
└─────────────────────────────────────────────────────────────────────────┘

1. User Message
   │
   ▼
2. WebSocket → FastAPI Backend
   │
   ▼
3. LangGraph Agent (generate node — single pass)
   │
   ├─── Read selected sources from config
   ├─── Embed query → all-MiniLM-L6-v2 (via embedding service)
   ├─── Vector search → Milvus HNSW/COSINE index (top-k=5 candidates)
   ├─── Filter by relevance score threshold (drop low-similarity chunks)
   ├─── Format context with source attribution
   ├─── Render system prompt with context
   └─── Single LLM call: system prompt + user question → streamed response
   │
   ▼
4. Stream Response → WebSocket → Frontend
   │
   ▼
5. Send Token Usage (prompt + completion totals) → Frontend
   │
   ▼
6. Save to PostgreSQL
```

### Retrieval Details

```python
# vector_store.py
def get_documents(self, query: str, k: int = 5, sources: List[str] = None):
    # Build filter for source selection
    if sources:
        filter_expr = ' || '.join([f'source == "{s}"' for s in sources])

    # Similarity search with COSINE relevance scoring (HNSW index)
    results_with_scores = self._store.similarity_search_with_relevance_scores(
        query, k=k, expr=filter_expr
    )

    # Drop chunks below the relevance threshold
    filtered = [(doc, score) for doc, score in results_with_scores
                if score >= RELEVANCE_SCORE_THRESHOLD]

    return [doc for doc, score in filtered]
```

**Key parameters:**
- `k=5`: Retrieves up to 5 candidate chunks from Milvus (reduced from 8 to minimize prompt tokens for faster LLM generation)
- `RELEVANCE_SCORE_THRESHOLD` (env: `RELEVANCE_SCORE_THRESHOLD`, default `0.4`): COSINE similarity score [0, 1] cutoff — chunks scoring below this are discarded before reaching the LLM
- `sources`: Optional filter for specific documents
- Scores are logged per-chunk at DEBUG level for threshold tuning
- All Milvus operations are wrapped in `asyncio.to_thread()` to avoid blocking the async event loop

---

## Source Selection

Users can select which documents to include in RAG searches:

```
┌────────────────────────────────────────┐
│           Document Sources             │
├────────────────────────────────────────┤
│ ☑ annual_report_2024.pdf              │
│ ☑ product_specs.docx                  │
│ ☐ meeting_notes.txt                   │
│ ☑ api_documentation.md                │
└────────────────────────────────────────┘
```

Selected sources are stored in `config.json` and used to filter Milvus queries:

```python
# Milvus filter expression
'source == "annual_report_2024.pdf" || source == "product_specs.docx"'
```

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
1. Read selected sources from config
2. Query Milvus vector store (via `asyncio.to_thread`)
3. Format retrieved context with source attribution
4. Render system prompt with context
5. Stream LLM response back to client

---

## Streaming Architecture

Responses stream token-by-token to the frontend:

```python
# main.py - WebSocket handler
async for event in agent.query(query_text, chat_id):
    await websocket.send_json(event)
```

**Event types:**

| Event | Description |
|-------|-------------|
| `node_start` | Generate node begins execution |
| `node_end` | Generate node completes |
| `token` | Streamed LLM token |
| `usage` | Token usage stats (prompt, completion, total) |
| `history` | Full conversation history |
| `error` | Error notification |

---

## Data Storage

### PostgreSQL Schema

```sql
-- Chat sessions
CREATE TABLE chats (
    id UUID PRIMARY KEY,
    name VARCHAR(255),
    created_at TIMESTAMP,
    updated_at TIMESTAMP
);

-- Conversation messages
CREATE TABLE messages (
    id UUID PRIMARY KEY,
    chat_id UUID REFERENCES chats(id),
    role VARCHAR(50),      -- 'user', 'assistant', 'tool'
    content TEXT,
    created_at TIMESTAMP
);

-- Indexed document sources
CREATE TABLE sources (
    id UUID PRIMARY KEY,
    name VARCHAR(255),
    file_path TEXT,
    indexed_at TIMESTAMP
);
```

### Milvus Collection

```python
# Collection: "context"
fields = [
    FieldSchema("pk", DataType.INT64, is_primary=True, auto_id=True),
    FieldSchema("embedding", DataType.FLOAT_VECTOR, dim=384),
    FieldSchema("page_content", DataType.VARCHAR, max_length=65535),
    FieldSchema("source", DataType.VARCHAR, max_length=255),
    FieldSchema("file_path", DataType.VARCHAR, max_length=1024),
    FieldSchema("filename", DataType.VARCHAR, max_length=255),
]
```

---

## Configuration

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `MILVUS_ADDRESS` | Milvus connection string | `tcp://localhost:19530` |
| `POSTGRES_HOST` | PostgreSQL hostname | `localhost` |
| `POSTGRES_DB` | Database name | `chatbot` |
| `MODELS` | Available LLM models | (comma-separated list) |
| `RELEVANCE_SCORE_THRESHOLD` | Min relevance score [0-1] for retrieved chunks | `0.4` |
| `CONFIG_PATH` | Runtime config file | `./config.json` |

### Runtime Config (config.json)

```json
{
  "selected_model": "nemotron-nano",
  "selected_sources": ["doc1.pdf", "doc2.pdf"]
}
```

---

## Key Design Decisions

### Why Milvus?
- Open-source, self-hosted (no cloud dependency)
- Excellent performance for similarity search
- Supports filtering with expressions
- Scales horizontally

### Why 1000-char chunks with 200 overlap?
- Balances context preservation with retrieval precision
- Overlap prevents losing information at boundaries
- Fits multiple chunks in LLM context window

### Why direct RAG pipeline instead of MCP tool calling?
- Eliminates ~20s of overhead from MCP subprocess stdio, LangGraph checkpointing, and multi-iteration state serialization
- Single-pass search + LLM call takes ~3s end-to-end
- Simpler architecture with fewer failure modes

### Why LangGraph?
- Structured async execution with streaming support
- Clean state management for conversation flow
- Extensible if multi-step workflows are needed later

---

## Performance Considerations

1. **Direct RAG pipeline**: Inline vector search + single LLM call in one pass (~3s end-to-end), eliminating the former ~20s MCP subprocess overhead
2. **Embedding latency**: all-MiniLM-L6-v2 runs locally on CPU, ~50-100ms per query
3. **Vector search**: Milvus HNSW index with COSINE metric, <10ms for top-k retrieval
4. **Async Milvus ops**: All synchronous pymilvus calls run in `asyncio.to_thread()` to avoid blocking the event loop
5. **No checkpointer overhead**: Graph runs without a MemorySaver since each query is stateless
6. **Efficient history persistence**: `append_messages()` uses the LRU cache when warm, avoiding redundant DB fetches on every turn
7. **Streaming**: Token-by-token delivery with `requestAnimationFrame`-based throttle for smooth rendering
8. **Background ingestion**: Large uploads don't block the UI
9. **Task cleanup**: Indexing task status entries are evicted after 1 hour to prevent unbounded memory growth
10. **Usage tracking**: Token usage (prompt/completion/total) reported after each response

---

## Extending the RAG System

### Adding a new document loader

```python
# utils.py
def load_document(file_path: str) -> List[Document]:
    if file_path.endswith('.custom'):
        return CustomLoader(file_path).load()
    # ... existing loaders
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

```python
# vector_store.py - increase candidate pool (default is 5)
def get_documents(self, query: str, k: int = 10):  # More candidates before threshold filter
```

### Adding metadata filters

```python
# Milvus supports complex expressions
filter_expr = 'source == "doc.pdf" && file_path contains "reports"'
```
