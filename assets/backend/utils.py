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
"""Utility functions for file processing and message conversion."""

import asyncio
import json
import os
from typing import Callable, List, Dict, Any

from langchain_core.messages import HumanMessage, AIMessage, ToolMessage, ToolCall, SystemMessage

from logger import logger
from vector_store import VectorStore


async def process_and_ingest_files_background(
    file_paths: List[str],
    vector_store: VectorStore,
    user_id: str,
    visibility: str,
    task_id: str,
    update_status: Callable[[str, str], None],
    postgres_storage=None,
) -> None:
    """Index already-saved files into the vector store.

    The /ingest endpoint streams uploads to disk under the per-task directory
    and validates magic bytes before queuing this task. By the time we run,
    every file in ``file_paths`` is already on disk at its final location,
    so this function just loads + chunks + indexes them.

    Args:
        file_paths: Absolute paths to already-saved uploaded files
        vector_store: VectorStore instance for document indexing
        user_id: Authenticated uploader (JWT sub)
        visibility: 'public' or 'private' — controls who can retrieve the chunks
        task_id: Unique identifier for this processing task
        update_status: Callback (task_id, status) that preserves the task's
            owner field. The full entry shape lives in main.py; we go through
            this helper so the owner doesn't get clobbered.
        postgres_storage: PostgreSQL storage for persisting source metadata
    """
    if visibility not in ("public", "private"):
        raise ValueError(f"Invalid visibility: {visibility!r}")

    try:
        logger.debug({
            "message": "Starting background file indexing",
            "task_id": task_id,
            "file_count": len(file_paths),
            "user_id": user_id,
            "visibility": visibility,
        })

        update_status(task_id, "loading_documents")
        logger.debug({"message": "Loading documents", "task_id": task_id})

        try:
            documents = await asyncio.to_thread(
                vector_store._load_documents, file_paths
            )

            logger.debug({
                "message": "Documents loaded, starting indexing",
                "task_id": task_id,
                "document_count": len(documents),
            })

            update_status(task_id, "indexing_documents")
            await asyncio.to_thread(
                vector_store.index_documents, documents, user_id, visibility
            )

            if file_paths and postgres_storage:
                for file_path in file_paths:
                    file_name = os.path.basename(file_path)
                    chunk_count = len(
                        [d for d in documents if d.metadata.get("filename") == file_name]
                    )
                    await postgres_storage.add_document_source(
                        source_name=file_name,
                        user_id=user_id,
                        visibility=visibility,
                        file_path=file_path,
                        task_id=task_id,
                        chunk_count=chunk_count,
                    )
                logger.debug({
                    "message": "Saved sources to PostgreSQL",
                    "task_id": task_id,
                    "sources": [os.path.basename(p) for p in file_paths],
                    "user_id": user_id,
                    "visibility": visibility,
                })

            update_status(task_id, "completed")
            logger.debug({
                "message": "Background indexing completed successfully",
                "task_id": task_id,
            })
        except Exception as e:
            update_status(task_id, f"failed_during_indexing: {str(e)}")
            logger.error({
                "message": "Error during document loading or indexing",
                "task_id": task_id,
                "error": str(e),
            }, exc_info=True)

    except Exception as e:
        update_status(task_id, f"failed: {str(e)}")
        logger.error({
            "message": "Error in background indexing task",
            "task_id": task_id,
            "error": str(e),
        }, exc_info=True)


def convert_langgraph_messages_to_openai(messages: List) -> List[Dict[str, Any]]:
    """Convert LangGraph message objects to OpenAI API format.

    Args:
        messages: List of LangGraph message objects

    Returns:
        List of dictionaries in OpenAI API format
    """
    openai_messages = []

    for msg in messages:
        if isinstance(msg, SystemMessage):
            openai_messages.append({
                "role": "system",
                "content": msg.content
            })
        elif isinstance(msg, HumanMessage):
            openai_messages.append({
                "role": "user",
                "content": msg.content
            })
        elif isinstance(msg, AIMessage):
            openai_msg = {
                "role": "assistant",
                "content": msg.content or ""
            }
            if hasattr(msg, 'tool_calls') and msg.tool_calls:
                openai_msg["tool_calls"] = []
                for tc in msg.tool_calls:
                    openai_msg["tool_calls"].append({
                        "id": tc["id"],
                        "type": "function",
                        "function": {
                            "name": tc["name"],
                            "arguments": json.dumps(tc["args"])
                        }
                    })
            openai_messages.append(openai_msg)
        elif isinstance(msg, ToolMessage):
            openai_messages.append({
                "role": "tool",
                "content": msg.content,
                "tool_call_id": msg.tool_call_id
            })

    return openai_messages
