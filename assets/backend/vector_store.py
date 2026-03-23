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
from typing import List, Tuple
import os
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_milvus import Milvus
from langchain_core.documents import Document
from typing_extensions import List
from langchain_openai import OpenAIEmbeddings
from langchain_unstructured import UnstructuredLoader
from dotenv import load_dotenv
from logger import logger
from typing import Optional, Callable
import requests


EMBEDDING_BATCH_SIZE = 32
RELEVANCE_SCORE_THRESHOLD = float(os.getenv("RELEVANCE_SCORE_THRESHOLD", "0.4"))


class CustomEmbeddings:
    """Wraps embedding service (all-MiniLM-L6-v2) to match OpenAI format.

    Supports batched requests to reduce HTTP round-trips during document indexing.
    Service name 'qwen3-embedding' is legacy; the model served is all-MiniLM-L6-v2.
    """
    def __init__(self, model: str = "all-MiniLM-L6-v2", host: str = "http://qwen3-embedding.rag-agent.svc.cluster.local:8000"):
        self.model = model
        self.url = f"{host}/v1/embeddings"
        self._session = requests.Session()

    def __call__(self, texts: list[str]) -> list[list[float]]:
        embeddings: list[list[float]] = []
        # Batch requests to reduce HTTP overhead during large ingestions
        for i in range(0, len(texts), EMBEDDING_BATCH_SIZE):
            batch = texts[i:i + EMBEDDING_BATCH_SIZE]
            response = self._session.post(
                self.url,
                json={"input": batch, "model": self.model},
                headers={"Content-Type": "application/json"},
                timeout=60,
            )
            response.raise_for_status()
            data = response.json()
            # Sort by index to maintain ordering
            sorted_data = sorted(data["data"], key=lambda x: x["index"])
            embeddings.extend(item["embedding"] for item in sorted_data)
        return embeddings

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed a list of document texts. Required by Milvus library."""
        return self.__call__(texts)

    def embed_query(self, text: str) -> list[float]:
        """Embed a single query text. Required by Milvus library."""
        return self.__call__([text])[0]


def _sanitize_milvus_string(value: str) -> str:
    """Sanitize a string value for use in Milvus filter expressions to prevent injection."""
    return value.replace('\\', '\\\\').replace('"', '\\"')


class VectorStore:
    """Vector store for document embedding and retrieval.

    Decoupled from ConfigManager - uses optional callbacks for source management.
    """

    def __init__(
        self,
        embeddings=None,
        uri: str = "http://milvus.milvus-system.svc.cluster.local:19530",
        on_source_deleted: Optional[Callable[[str], None]] = None
    ):
        try:
            self.embeddings = embeddings or CustomEmbeddings(model="all-MiniLM-L6-v2")
            self.uri = uri
            self.on_source_deleted = on_source_deleted
            self._milvus_connected = False
            self._initialize_store()

            self.text_splitter = RecursiveCharacterTextSplitter(
                chunk_size=1000,
                chunk_overlap=200
            )

            logger.debug({"message": "VectorStore initialized successfully"})
        except Exception as e:
            logger.error({"message": "Error initializing VectorStore", "error": str(e)}, exc_info=True)
            raise
    
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

    def _load_documents(self, file_paths: List[str] = None, input_dir: str = None) -> List[str]:
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

                    # For PDFs, use PyPDF first — it's faster and more
                    # reliable than UnstructuredLoader on ARM64.
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

                    # For non-PDFs (or if PyPDF failed), try UnstructuredLoader
                    if not file_text and docs is None:
                        try:
                            loader = UnstructuredLoader(file_path)
                            docs = loader.load()
                            logger.info(f"Successfully loaded {len(docs)} documents from {file_path}")
                        except Exception as unstructured_error:
                            logger.error(f'UnstructuredLoader failed: {unstructured_error}')

                    # Final fallback: raw text read
                    if not file_text and docs is None:
                        logger.info("Falling back to raw text read of file contents")
                        try:
                            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                                file_text = f.read()
                        except Exception as read_error:
                            logger.info(f"Fallback read failed: {read_error}")
                            file_text = ""

                    # Convert extracted text to a Document if we don't have docs yet
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

                        # Only include metadata fields that are in the Milvus schema
                        # The 'context' collection has dynamic fields disabled and only accepts:
                        # source, file_path, filename
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

    def index_documents(self, documents: List[Document]) -> List[Document]:
        try:
            logger.debug({
                "message": "Starting document indexing",
                "document_count": len(documents)
            })
            
            splits = self.text_splitter.split_documents(documents)
            logger.debug({
                "message": "Split documents into chunks",
                "chunk_count": len(splits)
            })
            
            self._store.add_documents(splits)
            self.flush_store()
            
            logger.debug({
                "message": "Document indexing completed"
            })            
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


    def get_documents(self, query: str, k: int = 5, sources: List[str] = None) -> List[Document]:
        """
        Get relevant documents filtered by similarity score threshold.

        Uses similarity_search_with_relevance_scores to get normalized [0, 1]
        scores (1 = most relevant) and drops chunks below RELEVANCE_SCORE_THRESHOLD.
        """
        try:
            kwargs = {}

            if sources:
                if len(sources) == 1:
                    filter_expr = f'source == "{_sanitize_milvus_string(sources[0])}"'
                else:
                    source_conditions = [f'source == "{_sanitize_milvus_string(s)}"' for s in sources]
                    filter_expr = " || ".join(source_conditions)

                kwargs["expr"] = filter_expr
                logger.debug({
                    "message": "Retrieving with filter",
                    "filter": filter_expr
                })

            results_with_scores = self._store.similarity_search_with_relevance_scores(
                query, k=k, **kwargs
            )

            for doc, score in results_with_scores:
                logger.debug({
                    "message": "Candidate document",
                    "source": doc.metadata.get("source", "unknown"),
                    "relevance_score": round(score, 4),
                    "preview": doc.page_content[:80]
                })

            filtered = [
                (doc, score)
                for doc, score in results_with_scores
                if score >= RELEVANCE_SCORE_THRESHOLD
            ]

            logger.info({
                "message": "Document retrieval complete",
                "query": query[:80],
                "total_candidates": len(results_with_scores),
                "above_threshold": len(filtered),
                "threshold": RELEVANCE_SCORE_THRESHOLD
            })

            return [doc for doc, score in filtered]
        except Exception as e:
            logger.error({
                "message": "Error retrieving documents",
                "error": str(e)
            }, exc_info=True)
            return []

    def delete_collection(self, collection_name: str) -> bool:
        """Delete a collection from Milvus."""
        try:
            from pymilvus import Collection, utility
            self._ensure_milvus_connection()

            if utility.has_collection(collection_name):
                collection = Collection(name=collection_name)

                collection.drop()

                if self.on_source_deleted:
                    self.on_source_deleted(collection_name)

                logger.debug({
                    "message": "Collection deleted successfully",
                    "collection_name": collection_name
                })
                return True
            else:
                logger.warning({
                    "message": "Collection not found",
                    "collection_name": collection_name
                })
                return False
        except Exception as e:
            logger.error({
                "message": "Error deleting collection",
                "collection_name": collection_name,
                "error": str(e)
            }, exc_info=True)
            return False

    def delete_documents_by_source(self, source_name: str) -> int:
        """Delete all documents with a specific source from Milvus."""
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

            delete_expr = f'source == "{_sanitize_milvus_string(source_name)}"'

            # First count how many will be deleted
            results = collection.query(
                expr=delete_expr,
                output_fields=["pk"]
            )
            count = len(results)

            if count > 0:
                collection.delete(delete_expr)
                collection.flush()

                logger.debug({
                    "message": "Deleted documents by source",
                    "source_name": source_name,
                    "deleted_count": count
                })

            return count

        except Exception as e:
            logger.error({
                "message": "Error deleting documents by source",
                "source_name": source_name,
                "error": str(e)
            }, exc_info=True)
            return -1

    def get_sources_from_milvus(self) -> List[str]:
        """Get list of unique sources from Milvus collection."""
        try:
            from pymilvus import Collection, utility
            self._ensure_milvus_connection()

            collection_name = "context"
            if not utility.has_collection(collection_name):
                return []

            collection = Collection(name=collection_name)
            collection.load()

            # Query all unique sources
            results = collection.query(
                expr="pk >= 0",  # Match all
                output_fields=["source"],
                limit=10000
            )

            sources = list(set(r.get("source", "") for r in results if r.get("source")))

            logger.debug({
                "message": "Retrieved sources from Milvus",
                "source_count": len(sources)
            })

            return sources

        except Exception as e:
            logger.error({
                "message": "Error getting sources from Milvus",
                "error": str(e)
            }, exc_info=True)
            return []


def create_vector_store_with_config(config_manager, uri: str = "http://milvus.milvus-system.svc.cluster.local:19530") -> VectorStore:
    """Factory function to create a VectorStore with ConfigManager integration.
    
    Args:
        config_manager: ConfigManager instance for source management
        uri: Milvus connection URI
        
    Returns:
        VectorStore instance with source deletion callback
    """
    def handle_source_deleted(source_name: str):
        """Handle source deletion by updating config."""
        config = config_manager.read_config()
        if hasattr(config, 'sources') and source_name in config.sources:
            config.sources.remove(source_name)
            config_manager.write_config(config)
    
    return VectorStore(
        uri=uri,
        on_source_deleted=handle_source_deleted
    )