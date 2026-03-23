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
"""FastAPI backend server for the chatbot application.

This module provides the main HTTP API endpoints and WebSocket connections for:
- Real-time chat via WebSocket
- File upload and document ingestion
- Configuration management (models, sources, chat settings)
- Chat history management
- Vector store operations
"""

import asyncio
import json
import os
import time
import uuid
from collections import defaultdict
from contextlib import asynccontextmanager
from typing import List, Optional, Dict, Set

from fastapi import Depends, FastAPI, File, HTTPException, UploadFile, BackgroundTasks, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from starlette.websockets import WebSocketState
from prometheus_fastapi_instrumentator import Instrumentator

from agent import ChatAgent
from auth import get_current_user, verify_websocket_token
from config import ConfigManager
from logger import logger, log_request, log_response, log_error
from models import ChatIdRequest, ChatRenameRequest, SelectedModelRequest
from postgres_storage import PostgreSQLConversationStorage
from utils import process_and_ingest_files_background
from vector_store import create_vector_store_with_config

POSTGRES_HOST = os.getenv("POSTGRES_HOST", "postgres")
POSTGRES_PORT = int(os.getenv("POSTGRES_PORT", 5432))
POSTGRES_DB = os.getenv("POSTGRES_DB", "chatbot")
POSTGRES_USER = os.getenv("POSTGRES_USER", "chatbot_user")
POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD")
MILVUS_ADDRESS = os.getenv("MILVUS_ADDRESS", "tcp://milvus.milvus-system.svc.cluster.local:19530")
CONFIG_PATH = os.getenv("CONFIG_PATH", "./config.json")
MAX_UPLOAD_SIZE_MB = int(os.getenv("MAX_UPLOAD_SIZE_MB", "50"))
MAX_UPLOAD_SIZE_BYTES = MAX_UPLOAD_SIZE_MB * 1024 * 1024
MAX_TOTAL_UPLOAD_BYTES = int(os.getenv("MAX_TOTAL_UPLOAD_MB", "200")) * 1024 * 1024
MAX_WS_MESSAGE_BYTES = int(os.getenv("MAX_WS_MESSAGE_BYTES", str(64 * 1024)))  # 64KB
MAX_WS_CONNECTIONS_PER_USER = int(os.getenv("MAX_WS_CONNECTIONS_PER_USER", "5"))
WS_AUTH_TIMEOUT = int(os.getenv("WS_AUTH_TIMEOUT", "10"))  # seconds
ALLOWED_UPLOAD_EXTENSIONS = {'.pdf', '.txt', '.docx', '.doc', '.md', '.rtf', '.csv', '.json', '.html'}

# WebSocket connection tracking
_ws_connections: Dict[str, Set[str]] = defaultdict(set)  # email -> set of connection IDs

config_manager = ConfigManager(CONFIG_PATH)

postgres_storage = PostgreSQLConversationStorage(
    host=POSTGRES_HOST,
    port=POSTGRES_PORT,
    database=POSTGRES_DB,
    user=POSTGRES_USER,
    password=POSTGRES_PASSWORD
)

vector_store = create_vector_store_with_config(config_manager, uri=MILVUS_ADDRESS)

agent: ChatAgent | None = None

TASK_TTL_SECONDS = 3600  # 1 hour
indexing_tasks: Dict[str, tuple] = {}  # task_id -> (status, timestamp)


def _record_task(task_id: str, status: str) -> None:
    """Record a task status and evict entries older than TASK_TTL_SECONDS."""
    now = time.time()
    # Evict stale entries
    stale = [tid for tid, (_, ts) in indexing_tasks.items() if now - ts > TASK_TTL_SECONDS]
    for tid in stale:
        del indexing_tasks[tid]
    indexing_tasks[task_id] = (status, now)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager for startup and shutdown tasks."""
    global agent
    logger.debug("Initializing PostgreSQL storage and agent...")
    
    try:
        await postgres_storage.init_pool()
        logger.info("PostgreSQL storage initialized successfully")
        logger.debug("Initializing ChatAgent...")
        agent = await ChatAgent.create(
            vector_store=vector_store,
            config_manager=config_manager,
            postgres_storage=postgres_storage
        )
        logger.info("ChatAgent initialized successfully.")

    except Exception as e:
        logger.error(f"Failed to initialize PostgreSQL storage: {e}")
        raise

    yield
    
    try:
        await postgres_storage.close()
        logger.debug("PostgreSQL storage closed successfully")
    except Exception as e:
        logger.error(f"Error closing PostgreSQL storage: {e}")


app = FastAPI(
    title="Chatbot API",
    description="Backend API for LLM-powered chatbot with RAG capabilities",
    version="1.0.0",
    lifespan=lifespan
)

_default_origins = [
    "http://localhost:3000",
    "http://sparkchat.bytecourier.local",
    "http://sparkchat.bytecourier.com",
    "https://sparkchat.bytecourier.com",
]
_env_origins = os.getenv("CORS_ALLOWED_ORIGINS", "")
CORS_ORIGINS = [o.strip() for o in _env_origins.split(",") if o.strip()] if _env_origins else _default_origins

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)

Instrumentator().instrument(app).expose(app)


@app.get("/health")
async def health_check():
    """Health check endpoint for Kubernetes probes."""
    return {"status": "healthy"}


# --- WebSocket (first-message auth) ---

def _validate_ws_origin(websocket: WebSocket) -> bool:
    """Validate WebSocket Origin header against allowed origins."""
    origin = websocket.headers.get("origin", "")
    if not origin:
        return True  # Allow missing origin (non-browser clients)
    return any(origin == o for o in CORS_ORIGINS)


@app.websocket("/ws/chat/{chat_id}")
async def websocket_endpoint(websocket: WebSocket, chat_id: str):
    """WebSocket endpoint for real-time chat communication.

    Uses first-message authentication: client connects, sends
    {"type": "auth", "token": "<jwt>"} as the first message,
    and receives {"type": "auth_ok"} on success.
    """
    # Validate origin header
    if not _validate_ws_origin(websocket):
        await websocket.close(code=4003, reason="Origin not allowed")
        return

    conn_id = str(uuid.uuid4())
    user_email: Optional[str] = None

    try:
        await websocket.accept()

        # Wait for auth message with timeout
        try:
            data = await asyncio.wait_for(
                websocket.receive_text(),
                timeout=WS_AUTH_TIMEOUT,
            )
        except asyncio.TimeoutError:
            await websocket.close(code=4001, reason="Authentication timeout")
            return

        try:
            auth_msg = json.loads(data)
        except json.JSONDecodeError:
            await websocket.close(code=4001, reason="Invalid auth message")
            return

        if auth_msg.get("type") != "auth" or not auth_msg.get("token"):
            await websocket.close(code=4001, reason="Expected auth message")
            return

        user_email = verify_websocket_token(auth_msg["token"])
        if not user_email:
            await websocket.close(code=4001, reason="Invalid or expired token")
            return

        # Enforce per-user connection limit
        if len(_ws_connections[user_email]) >= MAX_WS_CONNECTIONS_PER_USER:
            await websocket.close(code=4029, reason="Too many connections")
            return

        _ws_connections[user_email].add(conn_id)

        await websocket.send_json({"type": "auth_ok"})
        logger.debug(f"WebSocket authenticated for chat_id: {chat_id}")

        history_messages = await postgres_storage.get_messages(chat_id)
        history = [postgres_storage._message_to_dict(msg) for msg in history_messages]
        await websocket.send_json({"type": "history", "messages": history})

        while True:
            data = await websocket.receive_text()
            if len(data.encode("utf-8")) > MAX_WS_MESSAGE_BYTES:
                await websocket.send_json({"type": "error", "content": "Message too large"})
                continue
            client_message = json.loads(data)
            new_message = client_message.get("message")

            try:
                async for event in agent.query(query_text=new_message, chat_id=chat_id):
                    await websocket.send_json(event)
            except Exception as query_error:
                logger.error(f"Error in agent.query: {str(query_error)}", exc_info=True)
                await websocket.send_json({"type": "error", "content": "An error occurred processing your request"})

            final_messages = await postgres_storage.get_messages(chat_id)
            final_history = [postgres_storage._message_to_dict(msg) for msg in final_messages]
            await websocket.send_json({"type": "history", "messages": final_history})

    except WebSocketDisconnect:
        logger.debug(f"Client disconnected from chat {chat_id}")
    except Exception as e:
        logger.error(f"WebSocket error for chat {chat_id}: {str(e)}", exc_info=True)
    finally:
        if user_email:
            _ws_connections[user_email].discard(conn_id)



@app.post("/ingest")
async def ingest_files(files: Optional[List[UploadFile]] = File(None), background_tasks: BackgroundTasks = None, current_user: str = Depends(get_current_user)):
    """Ingest documents for vector search and RAG.
    
    Args:
        files: List of uploaded files to process
        background_tasks: FastAPI background tasks manager
        
    Returns:
        Task information for tracking ingestion progress
    """
    try:
        if not files:
            raise HTTPException(status_code=400, detail="No files provided")

        log_request({"file_count": len(files)}, "/ingest")

        task_id = str(uuid.uuid4())

        file_info = []
        total_size = 0
        for file in files:
            ext = os.path.splitext(file.filename or "")[1].lower()
            if ext not in ALLOWED_UPLOAD_EXTENSIONS:
                raise HTTPException(
                    status_code=400,
                    detail=f"File type '{ext}' not allowed. Allowed: {', '.join(sorted(ALLOWED_UPLOAD_EXTENSIONS))}"
                )
            content = await file.read()
            if len(content) > MAX_UPLOAD_SIZE_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail=f"File '{file.filename}' exceeds maximum size of {MAX_UPLOAD_SIZE_MB}MB"
                )
            total_size += len(content)
            if total_size > MAX_TOTAL_UPLOAD_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail="Total upload size exceeds limit"
                )
            file_info.append({
                "filename": file.filename,
                "content": content
            })
        
        _record_task(task_id, "queued")

        background_tasks.add_task(
            process_and_ingest_files_background,
            file_info,
            vector_store,
            config_manager,
            task_id,
            indexing_tasks,
            postgres_storage
        )
        
        response = {
            "message": f"Files queued for processing. Indexing {len(files)} files in the background.",
            "files": [file.filename for file in files],
            "status": "queued",
            "task_id": task_id
        }
        
        log_response(response, "/ingest")
        return response
            
    except Exception as e:
        log_error(e, "/ingest")
        raise HTTPException(
            status_code=500,
            detail="An internal error occurred"
        )


@app.get("/ingest/status/{task_id}")
async def get_indexing_status(task_id: str, current_user: str = Depends(get_current_user)):
    """Get the status of a file ingestion task.
    
    Args:
        task_id: Unique task identifier
        
    Returns:
        Current task status
    """
    if task_id in indexing_tasks:
        status, _ = indexing_tasks[task_id]
        return {"status": status}
    else:
        raise HTTPException(status_code=404, detail="Task not found")


@app.get("/sources")
async def get_sources(current_user: str = Depends(get_current_user)):
    """Get all available document sources from PostgreSQL."""
    try:
        sources = await postgres_storage.get_source_names()
        return {"sources": sources}
    except Exception as e:
        logger.error(f"Unhandled error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="An internal error occurred")


@app.delete("/sources/{source_name}")
async def delete_source(source_name: str, current_user: str = Depends(get_current_user)):
    """Delete a document source and its embeddings from Milvus.

    Args:
        source_name: Name of the source to delete

    Returns:
        Deletion result with count of removed embeddings
    """
    try:
        # Delete embeddings from Milvus (sync pymilvus call — run off event loop)
        deleted_count = await asyncio.to_thread(vector_store.delete_documents_by_source, source_name)

        if deleted_count < 0:
            raise HTTPException(status_code=500, detail=f"Error deleting embeddings for source: {source_name}")

        # Delete source record from PostgreSQL
        source_deleted = await postgres_storage.delete_document_source(source_name)

        # Also remove from config if present (for backwards compatibility)
        config = config_manager.read_config()
        if source_name in config.sources:
            config.sources.remove(source_name)
            config_manager.write_config(config)
        if source_name in config.selected_sources:
            config.selected_sources.remove(source_name)
            config_manager.write_config(config)

        return {
            "status": "success",
            "message": f"Deleted source '{source_name}' with {deleted_count} embeddings",
            "source_name": source_name,
            "embeddings_deleted": deleted_count,
            "source_record_deleted": source_deleted
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Unhandled error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="An internal error occurred")


@app.get("/selected_sources")
async def get_selected_sources(current_user: str = Depends(get_current_user)):
    """Get currently selected document sources for RAG."""
    try:
        config = config_manager.read_config()
        return {"sources": config.selected_sources}
    except Exception as e:
        logger.error(f"Unhandled error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="An internal error occurred")


@app.post("/selected_sources")
async def update_selected_sources(selected_sources: List[str], current_user: str = Depends(get_current_user)):
    """Update the selected document sources for RAG.
    
    Args:
        selected_sources: List of source names to use for retrieval
    """
    try:
        config_manager.updated_selected_sources(selected_sources)
        return {"status": "success", "message": "Selected sources updated successfully"}
    except Exception as e:
        logger.error(f"Unhandled error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="An internal error occurred")


@app.get("/selected_model")
async def get_selected_model(current_user: str = Depends(get_current_user)):
    """Get the currently selected LLM model."""
    try:
        model = config_manager.get_selected_model()
        return {"model": model}
    except Exception as e:
        logger.error(f"Unhandled error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="An internal error occurred")


@app.post("/selected_model")
async def update_selected_model(request: SelectedModelRequest, current_user: str = Depends(get_current_user)):
    """Update the selected LLM model.
    
    Args:
        request: Model selection request with model name
    """
    try:
        logger.debug(f"Updating selected model to: {request.model}")
        config_manager.updated_selected_model(request.model)
        return {"status": "success", "message": "Selected model updated successfully"}
    except Exception as e:
        logger.error(f"Unhandled error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="An internal error occurred")


@app.get("/available_models")
async def get_available_models(current_user: str = Depends(get_current_user)):
    """Get list of all available LLM models."""
    try:
        models = config_manager.get_available_models()
        return {"models": models}
    except Exception as e:
        logger.error(f"Unhandled error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="An internal error occurred")


@app.get("/chats")
async def list_chats(current_user: str = Depends(get_current_user)):
    """Get list of all chat conversations."""
    try:
        chat_ids = await postgres_storage.list_conversations()
        return {"chats": chat_ids}
    except Exception as e:
        logger.error(f"Unhandled error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="An internal error occurred")


@app.get("/chat_id")
async def get_chat_id(current_user: str = Depends(get_current_user)):
    """Get the current active chat ID, creating a conversation if it doesn't exist."""
    try:
        config = config_manager.read_config()
        current_chat_id = config.current_chat_id
        
        if current_chat_id and await postgres_storage.exists(current_chat_id):
            return {
                "status": "success",
                "chat_id": current_chat_id
            }
        
        new_chat_id = str(uuid.uuid4())
        
        await postgres_storage.save_messages_immediate(new_chat_id, [])
        await postgres_storage.set_chat_metadata(new_chat_id, f"Chat {new_chat_id[:8]}")
        
        config_manager.updated_current_chat_id(new_chat_id)
        
        return {
            "status": "success",
            "chat_id": new_chat_id
        }
    except Exception as e:
        logger.error(f"Unhandled error: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="An internal error occurred"
        )


@app.post("/chat_id")
async def update_chat_id(request: ChatIdRequest, current_user: str = Depends(get_current_user)):
    """Update the current active chat ID.
    
    Args:
        request: Chat ID update request
    """
    try:
        config_manager.updated_current_chat_id(request.chat_id)
        return {
            "status": "success",
            "message": f"Current chat ID updated to {request.chat_id}",
            "chat_id": request.chat_id
        }
    except Exception as e:
        logger.error(f"Unhandled error: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="An internal error occurred"
        )


@app.get("/chat/{chat_id}/metadata")
async def get_chat_metadata(chat_id: str, current_user: str = Depends(get_current_user)):
    """Get metadata for a specific chat.
    
    Args:
        chat_id: Unique chat identifier
        
    Returns:
        Chat metadata including name
    """
    try:
        metadata = await postgres_storage.get_chat_metadata(chat_id)
        return metadata
    except Exception as e:
        logger.error(f"Unhandled error: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="An internal error occurred"
        )


@app.post("/chat/rename")
async def rename_chat(request: ChatRenameRequest, current_user: str = Depends(get_current_user)):
    """Rename a chat conversation.
    
    Args:
        request: Chat rename request with chat_id and new_name
    """
    try:
        await postgres_storage.set_chat_metadata(request.chat_id, request.new_name)
        return {
            "status": "success",
            "message": f"Chat {request.chat_id} renamed to {request.new_name}"
        }
    except Exception as e:
        logger.error(f"Unhandled error: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="An internal error occurred"
        )


@app.post("/chat/new")
async def create_new_chat(current_user: str = Depends(get_current_user)):
    """Create a new chat conversation and set it as current."""
    try:
        new_chat_id = str(uuid.uuid4())
        await postgres_storage.save_messages_immediate(new_chat_id, [])
        await postgres_storage.set_chat_metadata(new_chat_id, f"Chat {new_chat_id[:8]}")
        
        config_manager.updated_current_chat_id(new_chat_id)
        
        return {
            "status": "success",
            "message": "New chat created",
            "chat_id": new_chat_id
        }
    except Exception as e:
        logger.error(f"Unhandled error: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="An internal error occurred"
        )


@app.delete("/chat/{chat_id}")
async def delete_chat(chat_id: str, current_user: str = Depends(get_current_user)):
    """Delete a specific chat and its messages.
    
    Args:
        chat_id: Unique chat identifier to delete
    """
    try:
        success = await postgres_storage.delete_conversation(chat_id)
        
        if success:
            return {
                "status": "success",
                "message": f"Chat {chat_id} deleted successfully"
            }
        else:
            raise HTTPException(
                status_code=404,
                detail=f"Chat {chat_id} not found"
            )
    except Exception as e:
        logger.error(f"Unhandled error: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="An internal error occurred"
        )


@app.delete("/chats/clear")
async def clear_all_chats(current_user: str = Depends(get_current_user)):
    """Clear all chat conversations and create a new default chat."""
    try:
        chat_ids = await postgres_storage.list_conversations()
        cleared_count = 0
        
        for chat_id in chat_ids:
            if await postgres_storage.delete_conversation(chat_id):
                cleared_count += 1
        
        new_chat_id = str(uuid.uuid4())
        await postgres_storage.save_messages_immediate(new_chat_id, [])
        await postgres_storage.set_chat_metadata(new_chat_id, f"Chat {new_chat_id[:8]}")
        
        config_manager.updated_current_chat_id(new_chat_id)
        
        return {
            "status": "success",
            "message": f"Cleared {cleared_count} chats and created new chat",
            "new_chat_id": new_chat_id,
            "cleared_count": cleared_count
        }
    except Exception as e:
        logger.error(f"Unhandled error: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="An internal error occurred"
        )


@app.delete("/collections/{collection_name}")
async def delete_collection(collection_name: str, current_user: str = Depends(get_current_user)):
    """Delete a document collection from the vector store.
    
    Args:
        collection_name: Name of the collection to delete
    """
    try:
        success = await asyncio.to_thread(vector_store.delete_collection, collection_name)
        if success:
            return {"status": "success", "message": f"Collection '{collection_name}' deleted successfully"}
        else:
            raise HTTPException(status_code=404, detail=f"Collection '{collection_name}' not found or could not be deleted")
    except Exception as e:
        logger.error(f"Unhandled error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="An internal error occurred")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)