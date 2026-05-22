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

Multi-tenant: every endpoint takes the JWT ``sub`` as ``current_user`` and
scopes its reads/writes by that value. Cross-user reads/writes/deletes are
impossible through the public API; chat IDs and source names are not enough
to access another user's data.

Streams chat responses over Server-Sent Events. Pairs with an in-process L1
cache (in ``postgres_storage.py``) and a Redis-backed L2 cache (in
``cache.py``) so a user pinned to one pod via Istio consistent-hash sees
warm L1 reads, while a cold pod still gets fast L2 reads instead of going
all the way to PostgreSQL.
"""

import asyncio
import json
import os
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Optional, Dict

import filetype
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from prometheus_fastapi_instrumentator import Instrumentator

from agent import ChatAgent
from auth import close_jwks, get_current_user, init_jwks
from cache import (
    CACHE_L2_TTL_SECONDS,
    clear_partial_response,
    get_partial_response,
    redis_cache,
    save_partial_response,
)
from config import ConfigManager
from logger import logger, log_request, log_response, log_error
from models import ChatIdRequest, ChatRenameRequest, QueryRequest, SelectedModelRequest
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
MAX_QUERY_BYTES = int(os.getenv("MAX_QUERY_BYTES", str(64 * 1024)))  # 64KB
MAX_STREAMS_PER_USER = int(os.getenv("MAX_STREAMS_PER_USER", "5"))
# Stream-slot TTL — long enough to cover any realistic single chat response
# (RAG + LLM generation), short enough that a crashed pod's counters auto-decay
# within ~10 minutes so users aren't stuck unable to start new streams.
STREAM_SLOT_TTL_SECONDS = int(os.getenv("STREAM_SLOT_TTL_SECONDS", "600"))
UPLOAD_READ_CHUNK_BYTES = 64 * 1024  # how much we slurp per read() from the request stream

ALLOWED_UPLOAD_EXTENSIONS = {'.pdf', '.txt', '.docx', '.doc', '.md', '.rtf', '.csv', '.json', '.html'}

# Magic-byte → extension matrix used to detect spoofed filenames. The
# ``filetype`` library doesn't recognise plain-text formats (no reliable
# magic), so for those we just guard against binary content in disguise.
_EXT_TO_MIME = {
    '.pdf': {'application/pdf'},
    '.docx': {'application/vnd.openxmlformats-officedocument.wordprocessingml.document'},
    '.doc': {'application/msword'},
    '.rtf': {'application/rtf', 'text/rtf'},
}
_TEXT_LIKE_EXTENSIONS = {'.txt', '.md', '.csv', '.json', '.html'}


def _sanitize_filename(name: Optional[str]) -> str:
    """Strip path separators, control chars, and leading dots/hyphens.

    Returns ``upload`` if the result would be empty. Caller still must use
    realpath + parent-dir startswith to fully guard against traversal.
    """
    if not name:
        return "upload"
    base = os.path.basename(name)
    base = "".join(c for c in base if ord(c) >= 0x20 and c not in "/\\")
    base = base.lstrip(".-")
    return base or "upload"


def _validate_magic_bytes(path: str, ext: str) -> Optional[str]:
    """Return ``None`` on success, an error string on mismatch.

    Binary formats (pdf, docx, doc, rtf) are matched via ``filetype``.
    Text formats are accepted iff the first 8 KB is valid UTF-8 with no
    NUL bytes — sufficient to reject EXE/binary-renamed-to-.txt attempts.
    """
    if ext in _TEXT_LIKE_EXTENSIONS:
        with open(path, "rb") as f:
            head = f.read(8192)
        if b"\x00" in head:
            return f"content appears binary, expected {ext}"
        try:
            head.decode("utf-8")
        except UnicodeDecodeError:
            return f"content is not valid UTF-8, expected {ext}"
        return None

    expected = _EXT_TO_MIME.get(ext)
    if expected is None:
        # Shouldn't happen — caller already filtered by ALLOWED_UPLOAD_EXTENSIONS
        return None

    detected = filetype.guess(path)
    if detected is None:
        return f"magic bytes unrecognised, expected {ext}"
    if detected.mime not in expected:
        return f"magic bytes ({detected.mime}) don't match extension {ext}"
    return None

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
# task_id -> (status, timestamp, owner_user_id) — owner gate so other users
# can't read your ingestion progress
indexing_tasks: Dict[str, tuple] = {}


def _record_task(task_id: str, status: str, owner: str) -> None:
    """Record a task status and evict entries older than TASK_TTL_SECONDS."""
    now = time.time()
    stale = [
        tid for tid, entry in indexing_tasks.items()
        if now - entry[1] > TASK_TTL_SECONDS
    ]
    for tid in stale:
        del indexing_tasks[tid]
    indexing_tasks[task_id] = (status, now, owner)


def _update_task_status(task_id: str, status: str) -> None:
    """Update an existing task's status, preserving its owner."""
    entry = indexing_tasks.get(task_id)
    if entry is None:
        return
    _, _, owner = entry
    indexing_tasks[task_id] = (status, time.time(), owner)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager for startup and shutdown tasks."""
    global agent
    logger.debug("Initializing PostgreSQL storage, Redis cache, and agent...")

    try:
        await postgres_storage.init_pool()
        logger.info("PostgreSQL storage initialized successfully")
        await redis_cache.connect()
        # JWKS is best-effort at startup: if the auth backend is briefly
        # unreachable we still come up and lazy-fetch on the first request.
        await init_jwks()
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

    try:
        await close_jwks()
    except Exception as e:
        logger.error(f"Error closing JWKS subsystem: {e}")

    try:
        await redis_cache.close()
    except Exception as e:
        logger.error(f"Error closing Redis cache: {e}")


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


# --- Chat: SSE streaming + history ---


def _sse(event: dict) -> str:
    """Serialize an event to SSE wire format."""
    return f"data: {json.dumps(event)}\n\n"


@app.get("/chat/{chat_id}/history")
async def get_chat_history(chat_id: str, current_user: str = Depends(get_current_user)):
    """Return committed conversation history plus any saved partial.

    Returns an empty history if the chat is owned by a different user — same
    response shape as a nonexistent chat so we don't leak existence.
    """
    history_messages = await postgres_storage.get_messages(current_user, chat_id)
    history = [postgres_storage._message_to_dict(msg) for msg in history_messages]
    partial = await get_partial_response(current_user, chat_id)
    return {"messages": history, "partial": partial}


@app.post("/chat/{chat_id}/query")
async def stream_chat_query(
    chat_id: str,
    body: QueryRequest,
    request: Request,
    current_user: str = Depends(get_current_user),
):
    """Stream an LLM response over SSE.

    Caller must own ``chat_id``. On first query against a fresh chat_id we
    create it under the caller; subsequent queries verify ownership.
    """
    if not body.message or not body.message.strip():
        raise HTTPException(status_code=400, detail="message is required")
    if len(body.message.encode("utf-8")) > MAX_QUERY_BYTES:
        raise HTTPException(status_code=413, detail="Message too large")

    # If the chat exists, it must be owned by this user. If it doesn't, the
    # agent's append_messages_to_chat will create the conversations row on
    # first save (the INSERT … ON CONFLICT DO NOTHING is a no-op if the
    # caller doesn't own a colliding chat_id).
    exists = await postgres_storage.exists(current_user, chat_id)
    if not exists:
        # Check whether some other user owns this chat_id (UUID collision /
        # squatting attempt) — refuse to create under the wrong owner.
        async with postgres_storage.pool.acquire() as conn:
            other_owner = await conn.fetchval(
                "SELECT user_id FROM conversations WHERE chat_id = $1",
                chat_id,
            )
        if other_owner and other_owner != current_user:
            raise HTTPException(status_code=404, detail="Chat not found")

    # Atomic per-user stream cap via Redis. Survives pod restarts and
    # consistent-hash rebalancing across KEDA scale events (the previous
    # in-memory dict was per-pod and could let a user briefly exceed the
    # cap while the hash ring was settling).
    acquired = await redis_cache.acquire_stream_slot(
        current_user, MAX_STREAMS_PER_USER, STREAM_SLOT_TTL_SECONDS
    )
    if not acquired:
        raise HTTPException(status_code=429, detail="Too many active streams")

    async def event_stream():
        partial_buffer: List[str] = []
        try:
            await clear_partial_response(current_user, chat_id)
            try:
                # PR 6: the agent now emits both ``history`` and ``done`` events
                # itself after persisting the turn, so this handler doesn't need
                # to re-query Postgres for the final history.
                async for event in agent.query(
                    query_text=body.message,
                    chat_id=chat_id,
                    user_id=current_user,
                ):
                    if event.get("type") == "token":
                        partial_buffer.append(event.get("data", "") or "")
                    yield _sse(event)
                    if await request.is_disconnected():
                        raise asyncio.CancelledError()
            except asyncio.CancelledError:
                raise
            except Exception as query_error:
                logger.error(f"Error in agent.query: {query_error}", exc_info=True)
                yield _sse({"type": "error", "content": "An error occurred processing your request"})
        except asyncio.CancelledError:
            partial = "".join(partial_buffer)
            if partial:
                await save_partial_response(current_user, chat_id, partial)
                logger.debug(
                    f"Saved partial response for chat {chat_id} (user={current_user}, "
                    f"{len(partial)} chars, TTL {CACHE_L2_TTL_SECONDS}s)"
                )
            raise
        finally:
            await redis_cache.release_stream_slot(current_user)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.post("/ingest")
async def ingest_files(
    files: Optional[List[UploadFile]] = File(None),
    visibility: str = Form("private"),
    background_tasks: BackgroundTasks = None,
    current_user: str = Depends(get_current_user),
):
    """Ingest documents for vector search and RAG.

    Uploads are tagged with the uploader (``user_id``) and a ``visibility``
    of either ``"public"`` (visible to all users) or ``"private"`` (visible
    only to the uploader, the default).

    Streaming: each file is read in 64 KB chunks straight to per-task disk
    so a malicious large upload can't OOM the backend. Size limits are
    checked mid-stream and the partial file is unlinked on overflow.
    """
    task_id = str(uuid.uuid4())
    uploads_base = os.getenv("UPLOADS_DIR", "uploads")
    permanent_dir = os.path.join(uploads_base, task_id)
    saved_paths: List[str] = []

    try:
        if not files:
            raise HTTPException(status_code=400, detail="No files provided")

        if visibility not in ("public", "private"):
            raise HTTPException(
                status_code=400,
                detail="visibility must be 'public' or 'private'",
            )

        log_request(
            {"file_count": len(files), "visibility": visibility, "user": current_user},
            "/ingest",
        )

        os.makedirs(permanent_dir, exist_ok=True)
        permanent_dir_real = Path(permanent_dir).resolve()

        total_size = 0
        for upload in files:
            ext = os.path.splitext(upload.filename or "")[1].lower()
            if ext not in ALLOWED_UPLOAD_EXTENSIONS:
                raise HTTPException(
                    status_code=400,
                    detail=f"File type '{ext}' not allowed. Allowed: {', '.join(sorted(ALLOWED_UPLOAD_EXTENSIONS))}",
                )

            safe_name = _sanitize_filename(upload.filename)
            dest_path = os.path.join(permanent_dir, safe_name)

            # Defense in depth against a maliciously crafted filename that
            # somehow slipped past _sanitize_filename — verify the resolved
            # path is still inside permanent_dir. Path.is_relative_to() is
            # the idiomatic safe form here; it correctly handles edge cases
            # like ``/uploads/abc`` vs ``/uploads/abcd`` that a naive
            # ``startswith`` would accept.
            real_dest = Path(dest_path).resolve()
            if not real_dest.is_relative_to(permanent_dir_real):
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid filename: {upload.filename!r}",
                )

            file_size = 0
            with open(dest_path, "wb") as out:
                while True:
                    chunk = await upload.read(UPLOAD_READ_CHUNK_BYTES)
                    if not chunk:
                        break
                    file_size += len(chunk)
                    if file_size > MAX_UPLOAD_SIZE_BYTES:
                        out.close()
                        os.unlink(dest_path)
                        raise HTTPException(
                            status_code=413,
                            detail=(
                                f"File '{upload.filename}' exceeds maximum size of "
                                f"{MAX_UPLOAD_SIZE_MB}MB"
                            ),
                        )
                    if total_size + file_size > MAX_TOTAL_UPLOAD_BYTES:
                        out.close()
                        os.unlink(dest_path)
                        raise HTTPException(
                            status_code=413,
                            detail="Total upload size exceeds limit",
                        )
                    out.write(chunk)

            total_size += file_size
            saved_paths.append(dest_path)

            magic_error = _validate_magic_bytes(dest_path, ext)
            if magic_error:
                os.unlink(dest_path)
                saved_paths.pop()
                raise HTTPException(
                    status_code=400,
                    detail=f"File '{upload.filename}' rejected: {magic_error}",
                )

        _record_task(task_id, "queued", current_user)

        background_tasks.add_task(
            process_and_ingest_files_background,
            saved_paths,
            vector_store,
            current_user,
            visibility,
            task_id,
            indexing_tasks,
            postgres_storage,
        )

        response = {
            "message": f"Files queued for processing. Indexing {len(files)} files in the background.",
            "files": [os.path.basename(p) for p in saved_paths],
            "status": "queued",
            "task_id": task_id,
            "visibility": visibility,
        }

        log_response(response, "/ingest")
        return response

    except HTTPException:
        # Clean up any partial uploads on a legitimate 4xx — don't leave
        # disk turds from rejected files behind. Re-raise so the client
        # sees the original status code instead of a remapped 500.
        for p in saved_paths:
            try:
                if os.path.exists(p):
                    os.unlink(p)
            except OSError:
                pass
        try:
            if os.path.isdir(permanent_dir) and not os.listdir(permanent_dir):
                os.rmdir(permanent_dir)
        except OSError:
            pass
        raise
    except Exception as e:
        log_error(e, "/ingest")
        for p in saved_paths:
            try:
                if os.path.exists(p):
                    os.unlink(p)
            except OSError:
                pass
        raise HTTPException(
            status_code=500,
            detail="An internal error occurred",
        )


@app.get("/ingest/status/{task_id}")
async def get_indexing_status(task_id: str, current_user: str = Depends(get_current_user)):
    """Get the status of a file ingestion task.

    Only the task owner can read its status. Other users get 404 (not 403)
    so we don't leak task existence.
    """
    entry = indexing_tasks.get(task_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Task not found")
    status, _, owner = entry
    if owner != current_user:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"status": status}


@app.get("/sources")
async def get_sources(current_user: str = Depends(get_current_user)):
    """Return sources visible to the caller (public + their own).

    Each entry has ``source_name`` and ``ownership`` ("public" or "yours")
    so the frontend can badge sources accordingly.
    """
    try:
        sources = await postgres_storage.get_visible_sources(current_user)
        return {"sources": sources}
    except Exception as e:
        logger.error(f"Unhandled error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="An internal error occurred")


@app.delete("/sources/{source_name}")
async def delete_source(source_name: str, current_user: str = Depends(get_current_user)):
    """Delete a document source that the caller owns.

    Public sources owned by other users (or legacy NULL-owner sources)
    return 404 — they require admin tooling to remove.
    """
    try:
        # Verify ownership before touching Milvus
        deleted_record = await postgres_storage.delete_document_source(
            source_name, current_user
        )
        if not deleted_record:
            raise HTTPException(
                status_code=404,
                detail=f"Source '{source_name}' not found or not owned by you",
            )

        deleted_count = await asyncio.to_thread(
            vector_store.delete_documents_by_source, source_name, current_user
        )
        if deleted_count < 0:
            raise HTTPException(
                status_code=500,
                detail=f"Error deleting embeddings for source: {source_name}",
            )

        # Drop from this user's selected_sources if present
        prefs = await postgres_storage.get_user_preferences(current_user)
        selected = prefs.get("selected_sources") or []
        if source_name in selected:
            selected.remove(source_name)
            await postgres_storage.update_user_selected_sources(current_user, selected)

        return {
            "status": "success",
            "message": f"Deleted source '{source_name}' with {deleted_count} embeddings",
            "source_name": source_name,
            "embeddings_deleted": deleted_count,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Unhandled error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="An internal error occurred")


@app.get("/selected_sources")
async def get_selected_sources(current_user: str = Depends(get_current_user)):
    """Get this user's currently selected document sources."""
    try:
        prefs = await postgres_storage.get_user_preferences(current_user)
        return {"sources": prefs.get("selected_sources") or []}
    except Exception as e:
        logger.error(f"Unhandled error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="An internal error occurred")


@app.post("/selected_sources")
async def update_selected_sources(
    selected_sources: List[str],
    current_user: str = Depends(get_current_user),
):
    """Update this user's selected sources for RAG.

    Only sources visible to the caller (public + their own) may be selected;
    anything else is silently dropped so the user can't pin private sources
    they don't own.
    """
    try:
        visible = set(
            await postgres_storage.get_visible_source_names(current_user)
        )
        filtered = [s for s in selected_sources if s in visible]
        await postgres_storage.update_user_selected_sources(current_user, filtered)
        return {"status": "success", "selected_sources": filtered}
    except Exception as e:
        logger.error(f"Unhandled error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="An internal error occurred")


@app.get("/selected_model")
async def get_selected_model(current_user: str = Depends(get_current_user)):
    """Get the currently selected LLM model (server-wide)."""
    try:
        model = config_manager.get_selected_model()
        return {"model": model}
    except Exception as e:
        logger.error(f"Unhandled error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="An internal error occurred")


@app.post("/selected_model")
async def update_selected_model(request: SelectedModelRequest, current_user: str = Depends(get_current_user)):
    """Update the selected LLM model (server-wide).

    Note: this is currently shared across users. Per-user model selection is
    out of scope for PR 1.
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
    """Get list of the caller's chat conversations."""
    try:
        chat_ids = await postgres_storage.list_conversations(current_user)
        return {"chats": chat_ids}
    except Exception as e:
        logger.error(f"Unhandled error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="An internal error occurred")


@app.get("/chat_id")
async def get_chat_id(current_user: str = Depends(get_current_user)):
    """Get the caller's last-active chat ID, creating one if missing."""
    try:
        prefs = await postgres_storage.get_user_preferences(current_user)
        current_chat_id = prefs.get("current_chat_id")

        if current_chat_id and await postgres_storage.exists(current_user, current_chat_id):
            return {"status": "success", "chat_id": current_chat_id}

        new_chat_id = str(uuid.uuid4())
        await postgres_storage.create_empty_chat(current_user, new_chat_id)
        await postgres_storage.set_chat_metadata(
            current_user, new_chat_id, f"Chat {new_chat_id[:8]}"
        )
        await postgres_storage.update_user_current_chat_id(current_user, new_chat_id)

        return {"status": "success", "chat_id": new_chat_id}
    except Exception as e:
        logger.error(f"Unhandled error: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="An internal error occurred"
        )


@app.post("/chat_id")
async def update_chat_id(request: ChatIdRequest, current_user: str = Depends(get_current_user)):
    """Update the caller's last-active chat ID.

    Only succeeds if the caller owns the chat; otherwise 404.
    """
    try:
        if not await postgres_storage.exists(current_user, request.chat_id):
            raise HTTPException(status_code=404, detail="Chat not found")
        await postgres_storage.update_user_current_chat_id(current_user, request.chat_id)
        return {
            "status": "success",
            "message": f"Current chat ID updated to {request.chat_id}",
            "chat_id": request.chat_id,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Unhandled error: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="An internal error occurred"
        )


@app.get("/chat/{chat_id}/metadata")
async def get_chat_metadata(chat_id: str, current_user: str = Depends(get_current_user)):
    """Get metadata for a chat the caller owns."""
    try:
        metadata = await postgres_storage.get_chat_metadata(current_user, chat_id)
        return metadata
    except Exception as e:
        logger.error(f"Unhandled error: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="An internal error occurred"
        )


@app.post("/chat/rename")
async def rename_chat(request: ChatRenameRequest, current_user: str = Depends(get_current_user)):
    """Rename a chat conversation the caller owns."""
    try:
        if not await postgres_storage.exists(current_user, request.chat_id):
            raise HTTPException(status_code=404, detail="Chat not found")
        await postgres_storage.set_chat_metadata(
            current_user, request.chat_id, request.new_name
        )
        return {
            "status": "success",
            "message": f"Chat {request.chat_id} renamed to {request.new_name}"
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Unhandled error: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="An internal error occurred"
        )


@app.post("/chat/new")
async def create_new_chat(current_user: str = Depends(get_current_user)):
    """Create a new chat conversation for the caller and set it current."""
    try:
        new_chat_id = str(uuid.uuid4())
        await postgres_storage.create_empty_chat(current_user, new_chat_id)
        await postgres_storage.set_chat_metadata(
            current_user, new_chat_id, f"Chat {new_chat_id[:8]}"
        )
        await postgres_storage.update_user_current_chat_id(current_user, new_chat_id)
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
    """Delete a chat the caller owns. 404 on not-owned/not-found."""
    try:
        success = await postgres_storage.delete_conversation(current_user, chat_id)

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
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Unhandled error: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="An internal error occurred"
        )


@app.delete("/chats/clear")
async def clear_all_chats(current_user: str = Depends(get_current_user)):
    """Clear the caller's chats and create a fresh empty one as current.

    Only the caller's chats are affected — never another user's data.
    """
    try:
        chat_ids = await postgres_storage.list_conversations(current_user)
        cleared_count = 0

        for chat_id in chat_ids:
            if await postgres_storage.delete_conversation(current_user, chat_id):
                cleared_count += 1

        new_chat_id = str(uuid.uuid4())
        await postgres_storage.create_empty_chat(current_user, new_chat_id)
        await postgres_storage.set_chat_metadata(
            current_user, new_chat_id, f"Chat {new_chat_id[:8]}"
        )
        await postgres_storage.update_user_current_chat_id(current_user, new_chat_id)

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


# NOTE: The previous DELETE /collections/{collection_name} endpoint was removed
# as part of PR 1. It allowed any authenticated user to drop arbitrary Milvus
# collections, including the shared ``context`` collection that backs RAG for
# every user. Per-user source deletion goes through DELETE /sources/{name}
# which is ownership-scoped via Postgres + Milvus user_id filters.


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
