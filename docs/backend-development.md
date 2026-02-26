# Backend

FastAPI Python application serving as the API backend for Spark Chat.

## Overview

The backend handles:
- LLM integration via OpenAI-compatible APIs
- Document ingestion and vector storage for RAG
- WebSocket connections for real-time chat streaming
- Chat history management via PostgreSQL
- MCP (Model Context Protocol) tool server integration

## Architecture

```
FastAPI App (main.py)
├── WebSocket: /ws/chat/{chat_id}  (real-time chat)
├── REST: /ingest, /sources, /chats, /models, etc.
├── ChatAgent (agent.py)           (LangGraph state machine)
│   ├── generate → should_continue → tool_node → loop
│   ├── MCP Client (client.py)     (RAG tool server)
│   └── AsyncOpenAI with timeouts  (LLM API calls)
├── PostgreSQLStorage              (LRU-cached conversation store)
│   ├── Bounded LRU caches         (max 200 entries, TTL expiration)
│   ├── Batch save worker          (1s flush interval)
│   └── Connection retry logic     (exponential backoff)
└── VectorStore (vector_store.py)  (Milvus integration)
    ├── Batched embeddings          (32 texts per request)
    ├── Persistent connections      (reused across operations)
    └── Sanitized filter expressions (injection prevention)
```

## Key Performance Features

- **LRU Cache with Eviction**: Bounded caches (200 entries max) with periodic eviction prevent memory leaks on long-running servers
- **Connection Resilience**: PostgreSQL pool initialization retries with exponential backoff (5 attempts)
- **LLM Request Timeouts**: 120s timeout on all LLM API calls prevents hung connections
- **Batched Embeddings**: Embedding requests are batched (32 per request) to reduce HTTP round-trips during document ingestion
- **Persistent Milvus Connections**: Single connection reused across flush, delete, and query operations
- **Input Validation**: File upload size limits (configurable via `MAX_UPLOAD_SIZE_MB`) and sanitized Milvus filter expressions
- **Configurable CORS**: Origins configurable via `CORS_ALLOWED_ORIGINS` environment variable

## Local Development

### Prerequisites
- Python 3.12 or higher
- [uv](https://docs.astral.sh/uv/) package manager
- PostgreSQL database (local or remote)
- Milvus vector database (local or remote)

### Setup

1. **Install uv** (if not already installed):
   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```

2. **Install dependencies**:
   ```bash
   cd assets/backend
   uv sync
   ```

3. **Configure environment variables**:
   ```bash
   POSTGRES_HOST=localhost
   POSTGRES_DB=chatbot
   POSTGRES_USER=chatbot_user
   POSTGRES_PASSWORD=your_password
   MILVUS_ADDRESS=localhost:19530
   MODELS=gpt-oss-120b
   CORS_ALLOWED_ORIGINS=http://localhost:3000
   MAX_UPLOAD_SIZE_MB=50
   ```

4. **Start development server**:
   ```bash
   uv run uvicorn main:app --reload --host 0.0.0.0 --port 8000
   ```

   API docs: [http://localhost:8000/docs](http://localhost:8000/docs)

### Available Commands

- `uv run uvicorn main:app --reload` - Start development server
- `uv run pytest` - Run tests
- `uv run ruff check .` - Run linting
- `uv run mypy .` - Run type checking

### Database Setup

For local development, run PostgreSQL and Milvus using Docker:

```bash
# PostgreSQL
docker run -d --name postgres \
  -e POSTGRES_DB=chatbot \
  -e POSTGRES_USER=chatbot_user \
  -e POSTGRES_PASSWORD=your_password \
  -p 5432:5432 postgres:15

# Milvus
docker run -d --name milvus \
  -p 19530:19530 -p 9091:9091 \
  milvusdb/milvus:latest
```

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `POSTGRES_HOST` | PostgreSQL hostname | `postgres` |
| `POSTGRES_PORT` | PostgreSQL port | `5432` |
| `POSTGRES_DB` | Database name | `chatbot` |
| `POSTGRES_USER` | Database user | `chatbot_user` |
| `POSTGRES_PASSWORD` | Database password | `chatbot_password` |
| `MILVUS_ADDRESS` | Milvus connection URI | `tcp://milvus:19530` |
| `MODELS` | Comma-separated model names | (required) |
| `CONFIG_PATH` | Runtime config file path | `./config.json` |
| `UPLOADS_DIR` | File upload directory | `uploads` |
| `CORS_ALLOWED_ORIGINS` | Comma-separated CORS origins | `http://localhost:3000` |
| `MAX_UPLOAD_SIZE_MB` | Max upload file size in MB | `50` |

## Docker Troubleshooting

### Common Commands
```bash
docker logs -f backend          # View logs
docker restart backend          # Restart container
docker exec -it backend bash    # Access shell
curl http://localhost:8000/health  # Check health
```
