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
import glob
import os
from collections import Counter
from typing import Dict, List, Optional, Callable

import httpx
import requests
from langchain_core.documents import Document
from langchain_milvus import Milvus
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_unstructured import UnstructuredLoader

from logger import logger


EMBEDDING_BATCH_SIZE = 32
RELEVANCE_SCORE_THRESHOLD = float(os.getenv("RELEVANCE_SCORE_THRESHOLD", "0.4"))
EMBEDDING_HTTP_TIMEOUT = 60.0


class CustomEmbeddings:
    """Wraps embedding service (all-MiniLM-L6-v2) to match OpenAI format.

    Supports both sync (``embed_query`` / ``embed_documents``) and async
    (``aembed_query`` / ``aembed_documents``) interfaces. The async path uses
    a shared ``httpx.AsyncClient`` so the embed call in the chat-query hot
    path never blocks the event loop — previously it was sync ``requests``
    wrapped in ``asyncio.to_thread``, which paid a thread per call.

    Batched in EMBEDDING_BATCH_SIZE chunks to reduce HTTP overhead during
    large ingestions.
    """
    def __init__(self, model: str = "all-MiniLM-L6-v2", host: str = "http://embedding.rag-agent.svc.cluster.local:8000"):
        self.model = model
        self.url = f"{host}/v1/embeddings"
        self._session = requests.Session()
        self._async_client: Optional[httpx.AsyncClient] = None

    def _ensure_async_client(self) -> httpx.AsyncClient:
        if self._async_client is None:
            self._async_client = httpx.AsyncClient(timeout=EMBEDDING_HTTP_TIMEOUT)
        return self._async_client

    def __call__(self, texts: list[str]) -> list[list[float]]:
        embeddings: list[list[float]] = []
        for i in range(0, len(texts), EMBEDDING_BATCH_SIZE):
            batch = texts[i:i + EMBEDDING_BATCH_SIZE]
            response = self._session.post(
                self.url,
                json={"input": batch, "model": self.model},
                headers={"Content-Type": "application/json"},
                timeout=EMBEDDING_HTTP_TIMEOUT,
            )
            response.raise_for_status()
            data = response.json()
            sorted_data = sorted(data["data"], key=lambda x: x["index"])
            embeddings.extend(item["embedding"] for item in sorted_data)
        return embeddings

    async def _acall(self, texts: list[str]) -> list[list[float]]:
        client = self._ensure_async_client()
        embeddings: list[list[float]] = []
        for i in range(0, len(texts), EMBEDDING_BATCH_SIZE):
            batch = texts[i:i + EMBEDDING_BATCH_SIZE]
            response = await client.post(
                self.url,
                json={"input": batch, "model": self.model},
                headers={"Content-Type": "application/json"},
            )
            response.raise_for_status()
            data = response.json()
            sorted_data = sorted(data["data"], key=lambda x: x["index"])
            embeddings.extend(item["embedding"] for item in sorted_data)
        return embeddings

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed a list of document texts (sync — used by ingest path)."""
        return self.__call__(texts)

    def embed_query(self, text: str) -> list[float]:
        """Embed a single query text (sync — used as fallback by langchain_milvus)."""
        return self.__call__([text])[0]

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed a list of document texts (async)."""
        return await self._acall(texts)

    async def aembed_query(self, text: str) -> list[float]:
        """Embed a single query text (async — used by chat-query hot path)."""
        return (await self._acall([text]))[0]


def _sanitize_milvus_string(value: str) -> str:
    """Sanitize a string value for use in Milvus filter expressions to prevent injection."""
    return value.replace('\\', '\\\\').replace('"', '\\"')


def _build_visibility_filter(user_id: str) -> str:
    """Build a Milvus filter expression that limits results to chunks visible
    to the given user — i.e. public chunks plus chunks the user uploaded.

    Legacy chunks indexed before user-scoping have no ``visibility`` / ``user_id``
    metadata at all; ``langchain_milvus`` writes those fields as empty string
    when missing from a document's metadata. We treat empty visibility as public,
    matching the Postgres-side legacy semantics.
    """
    safe_user = _sanitize_milvus_string(user_id)
    return (
        f'(visibility == "public" || visibility == "" || user_id == "{safe_user}")'
    )


class VectorStore:
    """Vector store for document embedding and retrieval.

    Document chunks carry per-user metadata so each user only sees public
    chunks and their own. The Milvus ``context`` collection has dynamic fields
    enabled, so adding new metadata keys (``user_id``, ``visibility``) doesn't
    require a schema migration on the Milvus side.
    """

    def __init__(
        self,
        embeddings=None,
        uri: str = "http://milvus.milvus-system.svc.cluster.local:19530",
        on_source_deleted: Optional[Callable[[str], None]] = None
    ):
        self.embeddings = embeddings or CustomEmbeddings(model="all-MiniLM-L6-v2")
        self.uri = uri
        self.on_source_deleted = on_source_deleted
        self._milvus_connected = False
        # Everything Milvus-touching is lazy. The backend pod must start
        # cleanly even when Milvus is unreachable so non-RAG endpoints
        # (chat without selected sources, chat list, auth) stay available.
        self._migration_done = False
        self._store = None  # populated by _get_store() on first vector op
        self.text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=1000,
            chunk_overlap=200
        )
        logger.debug({"message": "VectorStore constructed (lazy Milvus connection)"})

    def _get_store(self):
        """Lazy-create the langchain_milvus wrapper.

        langchain_milvus ≥ 2.6 connects to Milvus eagerly inside its
        ``Milvus()`` constructor (through ``MilvusClient``), so building it at
        VectorStore construction time would block the whole backend pod from
        starting whenever Milvus is unreachable.
        """
        if self._store is not None:
            return self._store
        self._initialize_store()
        return self._store

    def _ensure_migrated(self) -> bool:
        """Lazy guard called from every Milvus-touching method.

        Returns True if the collection is ready to query, False if Milvus is
        unreachable or migration failed (caller should fail gracefully).
        Idempotent and cheap once migration is complete.
        """
        if self._migration_done:
            return True
        try:
            self._migrate_collection_if_needed()
            # Build the langchain wrapper only after migration completes —
            # otherwise the wrapper would bind to the old fixed-schema
            # collection and never use the new dynamic-field one.
            self._get_store()
            self._migration_done = True
            return True
        except Exception as e:
            logger.warning({
                "message": (
                    "Milvus migration deferred — server unreachable or errored. "
                    "Will retry on next vector operation."
                ),
                "error": str(e),
            })
            self._milvus_connected = False
            self._store = None
            return False

    def _migrate_collection_if_needed(self) -> None:
        """Migrate the legacy ``context`` collection to the user-scoped schema.

        Pre-PR-1 collections were created with ``enable_dynamic_field=False``
        and a fixed schema of ``(text, pk, vector, source, file_path, filename)``.
        After PR 1 we need ``user_id`` and ``visibility`` metadata on every
        chunk so the visibility filter can run — the legacy fixed schema makes
        that impossible, so we copy the chunks into a fresh collection with
        dynamic fields enabled and tag them as legacy public.

        Idempotent: if the collection already has dynamic fields (or doesn't
        exist yet), this is a no-op.
        """
        from pymilvus import Collection, utility
        self._ensure_milvus_connection()

        if not utility.has_collection("context"):
            logger.debug("No 'context' collection — skipping migration")
            return

        coll = Collection("context")
        if coll.schema.enable_dynamic_field or any(
            f.name in ("user_id", "visibility") for f in coll.schema.fields
        ):
            logger.debug("Milvus 'context' collection already user-scoped")
            return

        coll.load()
        chunk_count = coll.num_entities
        logger.warning({
            "message": (
                "Migrating Milvus 'context' collection to user-scoped schema. "
                "Legacy chunks will be tagged as public (user_id='', "
                "visibility='public') and remain visible to all users."
            ),
            "chunks_to_migrate": chunk_count,
        })

        # Read all chunks from the legacy fixed-schema collection. We need
        # text + vector to reconstruct them; metadata fields ride along.
        results = coll.query(
            expr="pk >= 0",
            output_fields=["text", "vector", "source", "file_path", "filename"],
            limit=max(chunk_count + 100, 1000),
        )
        logger.info(f"Read {len(results)} chunks from legacy 'context' collection")

        # Drop the legacy collection before creating the new one with the
        # same name. Milvus has no rename, so we have to recycle the name.
        coll.drop()
        logger.debug("Dropped legacy 'context' collection")

        if not results:
            logger.info("No chunks to migrate; new collection will be created lazily")
            return

        # Re-create with dynamic fields via the langchain wrapper. Insert via
        # pymilvus directly to skip re-embedding (we already have vectors).
        from pymilvus import (
            CollectionSchema,
            DataType,
            FieldSchema,
            Collection as PMCollection,
        )

        # Build the new schema mirroring langchain_milvus's defaults (pk, vector,
        # text, plus the original source/file_path/filename string columns) with
        # ``enable_dynamic_field=True`` so ``user_id`` and ``visibility`` ride in
        # the dynamic JSON field. Filter expressions like
        # ``visibility == "public"`` work transparently against dynamic-field
        # values in Milvus 2.3+.
        vector_dim = len(results[0]["vector"])
        new_schema = CollectionSchema(
            fields=[
                FieldSchema(
                    name="pk",
                    dtype=DataType.INT64,
                    is_primary=True,
                    auto_id=True,
                ),
                FieldSchema(name="vector", dtype=DataType.FLOAT_VECTOR, dim=vector_dim),
                FieldSchema(name="text", dtype=DataType.VARCHAR, max_length=65535),
                FieldSchema(name="source", dtype=DataType.VARCHAR, max_length=500),
                FieldSchema(name="file_path", dtype=DataType.VARCHAR, max_length=1000),
                FieldSchema(name="filename", dtype=DataType.VARCHAR, max_length=500),
            ],
            description="rag-agent context with per-user metadata in dynamic field",
            enable_dynamic_field=True,
        )
        new_coll = PMCollection("context", schema=new_schema)
        new_coll.create_index(
            field_name="vector",
            index_params={
                "metric_type": "COSINE",
                "index_type": "HNSW",
                "params": {"M": 16, "efConstruction": 256},
            },
        )

        # Re-insert preserving vectors + adding legacy public metadata
        batch_size = 256
        inserted = 0
        for i in range(0, len(results), batch_size):
            batch = results[i : i + batch_size]
            entities = [
                {
                    "vector": list(r["vector"]),
                    "text": r.get("text", "") or "",
                    "source": r.get("source", "") or "",
                    "file_path": r.get("file_path", "") or "",
                    "filename": r.get("filename", "") or "",
                    "user_id": "",  # legacy: no owner
                    "visibility": "public",
                }
                for r in batch
            ]
            new_coll.insert(entities)
            inserted += len(batch)
        new_coll.flush()
        new_coll.load()
        logger.info({
            "message": "Milvus migration complete",
            "chunks_migrated": inserted,
        })

    def _ensure_milvus_connection(self) -> None:
        """Ensure a persistent Milvus connection exists, reconnecting if needed."""
        from pymilvus import connections
        if not self._milvus_connected:
            connections.connect(uri=self.uri)
            self._milvus_connected = True

    def _initialize_store(self):
        self._store = Milvus(
            embedding_function=self.embeddings,
            collection_name="context",
            connection_args={"uri": self.uri},
            auto_id=True,
            enable_dynamic_field=True,  # allow user_id + visibility without schema migration
            index_params={
                "metric_type": "COSINE",
                "index_type": "HNSW",
                "params": {"M": 16, "efConstruction": 256},
            },
            search_params={
                "metric_type": "COSINE",
                "params": {"ef": 64},
            },
        )
        logger.debug({
            "message": "Milvus vector store initialized",
            "uri": self.uri,
            "collection": "context"
        })

    def _load_documents(self, file_paths: List[str] = None, input_dir: str = None) -> List[Document]:
        try:
            documents = []
            source_name = None

            if input_dir:
                source_name = os.path.basename(os.path.normpath(input_dir))
                logger.debug({
                    "message": "Loading files from directory",
                    "directory": input_dir,
                    "source": source_name
                })
                file_paths = glob.glob(os.path.join(input_dir, "**"), recursive=True)
                file_paths = [f for f in file_paths if os.path.isfile(f)]

            logger.info(f"Processing {len(file_paths)} files: {file_paths}")

            for file_path in file_paths:
                try:
                    if not source_name:
                        source_name = os.path.basename(file_path)
                        logger.info(f"Using filename as source: {source_name}")

                    logger.info(f"Loading file: {file_path}")

                    file_ext = os.path.splitext(file_path)[1].lower()
                    logger.info(f"File extension: {file_ext}")

                    docs = None
                    file_text = None

                    if file_ext == ".pdf":
                        logger.info("Loading PDF with PyPDF")
                        try:
                            from pypdf import PdfReader
                            reader = PdfReader(file_path)
                            extracted_pages = []
                            for page in reader.pages:
                                try:
                                    extracted_pages.append(page.extract_text() or "")
                                except Exception as per_page_err:
                                    logger.info(f"Warning: failed to extract a page: {per_page_err}")
                                    extracted_pages.append("")
                            file_text = "\n\n".join(extracted_pages).strip()
                            logger.info(f"PyPDF extracted {len(extracted_pages)} pages")
                        except Exception as pypdf_error:
                            logger.info(f"PyPDF failed: {pypdf_error}")

                    if not file_text and docs is None:
                        try:
                            loader = UnstructuredLoader(file_path)
                            docs = loader.load()
                            logger.info(f"Successfully loaded {len(docs)} documents from {file_path}")
                        except Exception as unstructured_error:
                            logger.error(f'UnstructuredLoader failed: {unstructured_error}')

                    if not file_text and docs is None:
                        logger.info("Falling back to raw text read of file contents")
                        try:
                            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                                file_text = f.read()
                        except Exception as read_error:
                            logger.info(f"Fallback read failed: {read_error}")
                            file_text = ""

                    if docs is None:
                        if file_text and file_text.strip():
                            docs = [Document(
                                page_content=file_text,
                                metadata={
                                    "source": source_name,
                                    "file_path": file_path,
                                    "filename": os.path.basename(file_path),
                                }
                            )]
                        else:
                            logger.info("Creating a simple document as fallback (no text extracted)")
                            docs = [Document(
                                page_content=f"Document: {os.path.basename(file_path)}",
                                metadata={
                                    "source": source_name,
                                    "file_path": file_path,
                                    "filename": os.path.basename(file_path),
                                }
                            )]

                    for doc in docs:
                        if not doc.metadata:
                            doc.metadata = {}
                        cleaned_metadata = {}
                        cleaned_metadata["source"] = source_name
                        cleaned_metadata["file_path"] = file_path
                        cleaned_metadata["filename"] = os.path.basename(file_path)
                        doc.metadata = cleaned_metadata
                    documents.extend(docs)
                    logger.debug({
                        "message": "Loaded documents from file",
                        "file_path": file_path,
                        "document_count": len(docs)
                    })
                except Exception as e:
                    logger.error({
                        "message": "Error loading file",
                        "file_path": file_path,
                        "error": str(e)
                    }, exc_info=True)
                    continue

            logger.info(f"Total documents loaded: {len(documents)}")
            return documents

        except Exception as e:
            logger.error({
                "message": "Error loading documents",
                "error": str(e)
            }, exc_info=True)
            raise

    def index_documents(
        self,
        documents: List[Document],
        user_id: str,
        visibility: str = "private",
    ) -> Dict[str, int]:
        """Index documents with per-user ownership.

        Every chunk carries ``user_id`` (uploader) and ``visibility``
        ("public" or "private"). Retrieval filters by these fields so
        private chunks only surface for their owner.

        Returns a ``{filename: chunk_count}`` map so callers can record the
        real post-split chunk count per source. The previous pre-split count
        was always 1 for single-Document loaders (e.g. PyPDF), making
        ``/sources`` chunk numbers meaningless.
        """
        if visibility not in ("public", "private"):
            raise ValueError(f"Invalid visibility: {visibility!r}")
        if not self._ensure_migrated():
            raise RuntimeError(
                "Milvus is unavailable or migration pending — cannot index documents"
            )
        try:
            logger.debug({
                "message": "Starting document indexing",
                "document_count": len(documents),
                "user_id": user_id,
                "visibility": visibility,
            })

            splits = self.text_splitter.split_documents(documents)
            for chunk in splits:
                if chunk.metadata is None:
                    chunk.metadata = {}
                chunk.metadata["user_id"] = user_id
                chunk.metadata["visibility"] = visibility

            chunks_per_file = Counter(
                chunk.metadata.get("filename", "") for chunk in splits
            )

            logger.debug({
                "message": "Split documents into chunks",
                "chunk_count": len(splits)
            })

            self._store.add_documents(splits)
            self.flush_store()

            logger.debug({"message": "Document indexing completed"})
            return dict(chunks_per_file)
        except Exception as e:
            logger.error({
                "message": "Error during document indexing",
                "error": str(e)
            }, exc_info=True)
            raise

    def flush_store(self):
        """Flush the Milvus collection to persist all added documents to disk."""
        try:
            from pymilvus import utility
            self._ensure_milvus_connection()
            utility.flush_all()
            logger.debug({"message": "Milvus store flushed (persisted to disk)"})
        except Exception as e:
            self._milvus_connected = False
            logger.error({"message": "Error flushing Milvus store", "error": str(e)}, exc_info=True)

    async def get_documents(
        self,
        query: str,
        user_id: str,
        k: int = 5,
    ) -> List[Document]:
        """Retrieve documents visible to this user, filtered by similarity.

        Visibility filter (``visibility == "public" OR user_id == <user>``) is
        applied in Milvus. **Source-set partitioning happens in the caller**
        (see ``agent.generate``) — keeping it out of Milvus lets us answer the
        common "look in selected sources, fall back to corpus if empty" UX
        pattern in a single Milvus query instead of two.

        Chunks below ``RELEVANCE_SCORE_THRESHOLD`` are dropped before return.
        Async-native via ``asimilarity_search_with_relevance_scores`` so the
        chat-query path doesn't burn a thread on every turn.
        """
        if not self._ensure_migrated():
            logger.warning({
                "message": "Milvus unavailable — returning empty retrieval result",
                "user_id": user_id,
            })
            return []
        try:
            filter_expr = _build_visibility_filter(user_id)

            logger.debug({
                "message": "Retrieving with filter",
                "filter": filter_expr,
                "k": k,
                "user_id": user_id,
            })

            results_with_scores = await self._store.asimilarity_search_with_relevance_scores(
                query, k=k, expr=filter_expr
            )

            for doc, score in results_with_scores:
                logger.debug({
                    "message": "Candidate document",
                    "source": doc.metadata.get("source", "unknown"),
                    "relevance_score": round(score, 4),
                    "preview": doc.page_content[:80]
                })

            above_threshold = [
                doc for doc, score in results_with_scores
                if score >= RELEVANCE_SCORE_THRESHOLD
            ]

            logger.info({
                "message": "Document retrieval complete",
                "query": query[:80],
                "total_candidates": len(results_with_scores),
                "above_threshold": len(above_threshold),
                "threshold": RELEVANCE_SCORE_THRESHOLD,
                "user_id": user_id,
            })

            return above_threshold
        except Exception as e:
            logger.error({
                "message": "Error retrieving documents",
                "error": str(e)
            }, exc_info=True)
            return []

    def delete_documents_by_source(
        self, source_name: str, user_id: str
    ) -> int:
        """Delete the caller's chunks for a given source.

        Only chunks where ``user_id == <caller>`` are deleted. Legacy
        chunks (``user_id`` empty / NULL, ``visibility = "public"``) and
        chunks owned by other users are left in place — returns the count
        of chunks the caller actually owned and deleted.
        """
        if not self._ensure_migrated():
            return -1
        try:
            from pymilvus import Collection, utility
            self._ensure_milvus_connection()

            collection_name = "context"
            if not utility.has_collection(collection_name):
                logger.warning({
                    "message": "Collection not found",
                    "collection_name": collection_name
                })
                return 0

            collection = Collection(name=collection_name)
            collection.load()

            safe_source = _sanitize_milvus_string(source_name)
            safe_user = _sanitize_milvus_string(user_id)
            delete_expr = (
                f'source == "{safe_source}" && user_id == "{safe_user}"'
            )

            results = collection.query(expr=delete_expr, output_fields=["pk"])
            count = len(results)

            if count > 0:
                collection.delete(delete_expr)
                collection.flush()
                logger.debug({
                    "message": "Deleted documents by source",
                    "source_name": source_name,
                    "user_id": user_id,
                    "deleted_count": count
                })

            return count

        except Exception as e:
            logger.error({
                "message": "Error deleting documents by source",
                "source_name": source_name,
                "user_id": user_id,
                "error": str(e)
            }, exc_info=True)
            return -1

    def get_sources_visible_to(self, user_id: str) -> List[str]:
        """List unique source names in Milvus visible to this user."""
        if not self._ensure_migrated():
            return []
        try:
            from pymilvus import Collection, utility
            self._ensure_milvus_connection()

            collection_name = "context"
            if not utility.has_collection(collection_name):
                return []

            collection = Collection(name=collection_name)
            collection.load()

            expr = _build_visibility_filter(user_id)
            results = collection.query(
                expr=expr,
                output_fields=["source"],
                limit=10000,
            )

            sources = list({r.get("source", "") for r in results if r.get("source")})

            logger.debug({
                "message": "Retrieved sources from Milvus",
                "source_count": len(sources),
                "user_id": user_id,
            })

            return sources

        except Exception as e:
            logger.error({
                "message": "Error getting sources from Milvus",
                "error": str(e)
            }, exc_info=True)
            return []


def create_vector_store_with_config(
    config_manager,
    uri: str = "http://milvus.milvus-system.svc.cluster.local:19530",
) -> VectorStore:
    """Factory function to create a VectorStore.

    The ``config_manager`` argument is retained for API stability but is no
    longer used for source-list mutation — the source registry is owned by
    PostgreSQL with per-user scoping after PR 1.
    """
    return VectorStore(uri=uri)
