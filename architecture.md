# RAG Architecture

This document explains how Retrieval-Augmented Generation (RAG) works in this multi-agent chatbot system.

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
| Agent | LangGraph | Orchestrates tool calls and LLM interactions |
| Vector DB | Milvus | Stores and searches document embeddings |
| Embedding | Qwen3-Embedding-4B | Converts text to vectors |
| LLM | gpt-oss-120b (vLLM) | Generates responses |
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

Each chunk is embedded using the Qwen3-Embedding model:

```python
# vector_store.py
class CustomEmbeddings:
    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        # POST to Qwen3-Embedding service
        response = requests.post(
            "http://qwen3-embedding:8000/v1/embeddings",
            json={"input": texts, "model": "Qwen3-Embedding-4B"}
        )
        return [item["embedding"] for item in response.json()["data"]]
```

### 5. Vector Storage

Embeddings are stored in Milvus with metadata:

| Field | Type | Description |
|-------|------|-------------|
| `pk` | Int64 | Auto-generated primary key |
| `embedding` | FloatVector | 1024-dimensional embedding |
| `page_content` | VarChar | Original text chunk |
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
3. LangGraph Agent (generate node)
   │
   ├─── LLM decides: "I need to search documents"
   │
   ▼
4. Tool Call: search_documents(query="user question")
   │
   ▼
5. MCP RAG Server
   │
   ├─── Get selected sources from config
   ├─── Embed query → Qwen3-Embedding
   ├─── Vector search → Milvus (top-k=8)
   └─── Format results with source attribution
   │
   ▼
6. Tool Result → Agent
   │
   ▼
7. LangGraph Agent (generate node, 2nd iteration)
   │
   ├─── LLM receives: original question + retrieved context
   └─── Generates grounded response
   │
   ▼
8. Stream Response → WebSocket → Frontend
   │
   ▼
9. Save to PostgreSQL
```

### Retrieval Details

```python
# vector_store.py
def get_documents(self, query: str, k: int = 8, sources: List[str] = None):
    # Build filter for source selection
    if sources:
        filter_expr = ' || '.join([f'source == "{s}"' for s in sources])

    # Similarity search
    retriever = self._store.as_retriever(
        search_type="similarity",
        search_kwargs={"k": k, "expr": filter_expr}
    )
    return retriever.invoke(query)
```

**Key parameters:**
- `k=8`: Returns top 8 most similar chunks
- `sources`: Optional filter for specific documents
- `search_type="similarity"`: L2/cosine distance in vector space

---

## MCP Tool Integration

The RAG functionality is exposed as an MCP (Model Context Protocol) tool:

```python
# tools/mcp_servers/rag.py
@mcp.tool()
async def search_documents(query: str) -> str:
    """Search indexed documents for relevant context."""

    # Get user-selected sources
    config = rag_agent.config_manager.read_config()
    sources = config.selected_sources or []

    # Retrieve documents
    docs = vector_store.get_documents(query, sources=sources)

    # Format with source attribution
    context_parts = []
    for i, doc in enumerate(docs, 1):
        source = doc.metadata.get("source", "unknown")
        context_parts.append(f"[Document {i} - {source}]\n{doc.page_content}")

    return "\n\n".join(context_parts)
```

The LLM decides when to call this tool based on the user's question:
- Questions about document content → calls `search_documents`
- General questions → responds directly
- Image questions → calls `explain_image`

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

## Agent State Machine

The LangGraph agent manages the RAG workflow:

```
                    ┌─────────────┐
                    │    START    │
                    └──────┬──────┘
                           │
                           ▼
                    ┌─────────────┐
              ┌────▶│  generate   │◀────┐
              │     └──────┬──────┘     │
              │            │            │
              │            ▼            │
              │     ┌─────────────┐     │
              │     │  continue?  │     │
              │     └──────┬──────┘     │
              │            │            │
              │      YES   │   NO       │
              │            ▼            │
              │     ┌─────────────┐     │
              │     │  tool_node  │     │
              │     └──────┬──────┘     │
              │            │            │
              └────────────┘            │
                                        ▼
                                 ┌─────────────┐
                                 │     END     │
                                 └─────────────┘
```

**Loop control:**
- Maximum 3 iterations
- Exits when LLM has no more tool calls
- Each iteration adds tool results to message history

---

## Streaming Architecture

Responses stream token-by-token to the frontend:

```python
# main.py - WebSocket handler
async for event in agent.query(query_text, chat_id, image_data):
    await websocket.send_json(event)
```

**Event types:**

| Event | Description |
|-------|-------------|
| `node_start` | Agent node begins execution |
| `tool_start` | Tool invocation begins |
| `tool_end` | Tool invocation completes |
| `token` | Streamed LLM token |
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
    FieldSchema("embedding", DataType.FLOAT_VECTOR, dim=1024),
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
| `CONFIG_PATH` | Runtime config file | `./config.json` |

### Runtime Config (config.json)

```json
{
  "selected_model": "gpt-oss-120b",
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

### Why MCP for tools?
- Clean separation between agent and tool implementations
- Tools run as separate processes (isolation)
- Easy to add new tools without modifying agent code

### Why LangGraph over LangChain agents?
- Explicit state machine control
- Better streaming support
- Clearer iteration limits
- More predictable behavior

---

## Performance Considerations

1. **Embedding latency**: Qwen3-Embedding runs locally, ~50-100ms per query
2. **Vector search**: Milvus IVF_FLAT index, <10ms for top-k retrieval
3. **Streaming**: Token-by-token delivery minimizes perceived latency
4. **Background ingestion**: Large uploads don't block the UI

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

```python
# vector_store.py
def get_documents(self, query: str, k: int = 10):  # Increase k for more context
```

### Adding metadata filters

```python
# Milvus supports complex expressions
filter_expr = 'source == "doc.pdf" && file_path contains "reports"'
```
