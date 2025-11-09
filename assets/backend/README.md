# Backend

FastAPI Python application serving as the API backend for the chatbot demo.

## Overview

The backend handles:
- Multi-model LLM integration (local models)
- Document ingestion and vector storage for RAG
- WebSocket connections for real-time chat streaming
- Image processing and analysis
- Chat history management
- Model Control Protocol (MCP) integration

## Key Features

- **Multi-model support**: Integrates various LLM providers and local models
- **RAG pipeline**: Document processing, embedding generation, and retrieval
- **Streaming responses**: Real-time token streaming via WebSocket
- **Image analysis**: Multi-modal capabilities for image understanding
- **Vector database**: Efficient similarity search for document retrieval
- **Session management**: Chat history and context persistence

## Architecture

FastAPI application with async support, integrated with vector databases for RAG functionality and WebSocket endpoints for real-time communication.

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
   Create a `.env` file or set environment variables:
   ```bash
   # Database Configuration
   POSTGRES_HOST=localhost
   POSTGRES_DB=chatbot
   POSTGRES_USER=chatbot_user
   POSTGRES_PASSWORD=your_password

   # Vector Database
   MILVUS_ADDRESS=localhost:19530

   # Model Configuration
   MODELS=gpt-oss-120b
   ```

4. **Start development server**:
   ```bash
   uv run uvicorn main:app --reload --host 0.0.0.0 --port 8000
   ```

   The API will be available at [http://localhost:8000](http://localhost:8000)

   API documentation: [http://localhost:8000/docs](http://localhost:8000/docs)

### Available Commands

- `uv run uvicorn main:app --reload` - Start development server with auto-reload
- `uv run pytest` - Run tests
- `uv run ruff check .` - Run linting
- `uv run mypy .` - Run type checking

### Development Workflow

1. Make changes to Python files
2. Server automatically reloads on file changes (with `--reload` flag)
3. Test API endpoints using the interactive docs at `/docs`
4. Ensure PostgreSQL and Milvus are accessible

### Database Setup

For local development, you can run PostgreSQL and Milvus using Docker:

```bash
# PostgreSQL
docker run -d \
  --name postgres \
  -e POSTGRES_DB=chatbot \
  -e POSTGRES_USER=chatbot_user \
  -e POSTGRES_PASSWORD=your_password \
  -p 5432:5432 \
  postgres:15

# Milvus
docker run -d \
  --name milvus \
  -p 19530:19530 \
  -p 9091:9091 \
  milvusdb/milvus:latest
```

## Docker Troubleshooting

### Container Issues
- **Port conflicts**: Ensure port 8000 is not in use
- **Memory issues**: Backend requires significant RAM for model loading
- **Startup failures**: Check if required environment variables are set

### Model Loading Problems
```bash
# Check model download status
docker logs backend | grep -i "model"

# Verify model files exist
docker exec -it cbackend ls -la /app/models/

# Check available disk space
docker exec -it backend df -h
```

### Common Commands
```bash
# View backend logs
docker logs -f backend

# Restart backend container
docker restart backend

# Rebuild backend
docker-compose up --build -d backend

# Access container shell
docker exec -it backend /bin/bash

# Check API health
curl http://localhost:8000/health
```

### Performance Issues
- **Slow responses**: Check GPU availability and model size
- **Memory errors**: Increase Docker memory limit or use smaller models
- **Connection timeouts**: Verify WebSocket connections and firewall settings
