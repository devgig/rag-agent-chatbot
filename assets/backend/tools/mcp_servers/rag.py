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

"""RAG MCP Server for Document Search and Question Answering.

This module implements an MCP server that provides document search capabilities using
a simple retrieval-augmented generation (RAG) pipeline. The server exposes a 
search_documents tool that retrieves relevant document chunks and generates answers.

The simplified RAG workflow consists of:
    - Document retrieval from a vector store
    - Answer generation using retrieved context
"""
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Annotated, Dict, List, Optional, Sequence, TypedDict

from langchain_core.documents import Document
from langchain_core.messages import AnyMessage, HumanMessage, AIMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph, add_messages
from mcp.server.fastmcp import FastMCP
from openai import AsyncOpenAI
from pypdf import PdfReader

project_root = Path(__file__).parent.parent.parent
sys.path.append(str(project_root))

from config import ConfigManager
from vector_store import VectorStore, create_vector_store_with_config


logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(name)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    level=logging.INFO
)
logger = logging.getLogger(__name__)


class RAGState(TypedDict, total=False):
    """Type definition for the simplified RAG agent state.
    
    Attributes:
        question: The user's question to be answered.
        messages: Conversation history with automatic message aggregation.
        context: Retrieved documents from the local vector store.
        sources: Optional list of source filters for retrieval.
    """
    question: str
    messages: Annotated[Sequence[AnyMessage], add_messages]
    context: Optional[List[Document]]
    sources: Optional[List[str]]


class RAGAgent:
    """Simplified RAG Agent for fast document search and answer generation.
    
    This agent manages a simple two-step pipeline:
    1. Retrieve documents from the local vector store.
    2. Generate an answer using the retrieved context.
    """
    
    def __init__(self):
        """Initialize the RAG agent with model client, configuration, and graph."""
        config_path = self._get_config_path()
        self.config_manager = ConfigManager(config_path)
        milvus_address = os.getenv("MILVUS_ADDRESS", "tcp://milvus.milvus-system.svc.cluster.local:19530")
        self.vector_store = create_vector_store_with_config(self.config_manager, uri=milvus_address)
        self.model_name = self.config_manager.get_selected_model()
        self.model_client = AsyncOpenAI(
            base_url=f"http://{self.model_name}:8000/v1",
            api_key="api_key"
        )

        self.generation_prompt = self._get_generation_prompt()
        

        self.graph = self._build_graph()

    def _get_config_path(self):
        """Get the configuration file path and validate its existence."""
        config_path = os.getenv("CONFIG_PATH", "./config.json")
        if not os.path.exists(config_path):
            logger.error(f"ERROR: config.json not found at {config_path}")
        return config_path

    def _get_generation_prompt(self) -> str:
        """Get the system prompt template for the generation node."""
        return """You are an assistant for question-answering tasks. 
        Use the following pieces of retrieved context to answer the question.
        If no context is provided, answer the question using your own knowledge, but state that you could not find relevant information in the provided documents.
        Don't make up any information that is not provided in the context. Keep the answer concise.
        
        Context: 
        {context}
        """

    def retrieve(self, state: RAGState) -> Dict:
        """Retrieve relevant documents from the vector store."""
        logger.info({"message": "Starting document retrieval"})
        sources = state.get("sources", [])
        
        if sources:
            logger.info({"message": "Attempting retrieval with source filters", "sources": sources})
            retrieved_docs = self.vector_store.get_documents(state["question"], sources=sources)
        else:
            logger.info({"message": "No sources specified, searching all documents"})
            retrieved_docs = self.vector_store.get_documents(state["question"])
        
        if not retrieved_docs and sources:
            logger.info({"message": "No documents found with source filtering, trying without filters"})
            retrieved_docs = self.vector_store.get_documents(state["question"])
        
        if retrieved_docs:
            sources_found = set(doc.metadata.get("source", "unknown") for doc in retrieved_docs)
            logger.info({"message": "Document sources found", "sources": list(sources_found), "doc_count": len(retrieved_docs)})
        else:
            logger.warning({"message": "No documents retrieved", "query": state["question"], "attempted_sources": sources})
            
        return {"context": retrieved_docs}


    async def generate(self, state: RAGState) -> Dict:
        """Generate an answer using retrieved context."""
        logger.info({
            "message": "Generating answer", 
            "question": state['question']
        })
        
        context = state.get("context", [])
        
        if not context: 
            logger.warning({"message": "No context available for generation", "question": state['question']})
            docs_content = "No relevant documents were found."
        else:
            logger.info({"message": "Generating with context", "context_count": len(context)})
            docs_content = self._hydrate_context(context)

        system_prompt = self.generation_prompt.format(context=docs_content)
        user_message = f"Question: {state['question']}"

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message}
        ]

        try:
            response = await self.model_client.chat.completions.create(
                    model=self.model_name,
                    messages=messages,
            )
            response_content = response.choices[0].message.content
            
            logger.info({
                "message": "Generation completed",
                "response_length": len(response_content),
                "response_preview": response_content[:100] + "..."
            })
            
            return {
                "messages": [HumanMessage(content=state["question"]), AIMessage(content=response_content)]
            }
        except Exception as e:
            logger.error({"message": "Error during generation", "error": str(e)})
            fallback_response = f"I apologize, but I encountered an error while processing your query about: {state['question']}"
            return {
                "messages": [HumanMessage(content=state["question"]), AIMessage(content=fallback_response)]
            }

    def _hydrate_context(self, context: List[Document]) -> str:
        """Extract text content from document objects."""
        return "\n\n".join([doc.page_content for doc in context if doc.page_content])

    def _build_graph(self):
        """Build and compile the simplified RAG workflow graph."""
        workflow = StateGraph(RAGState)

        workflow.add_node("retrieve", self.retrieve)
        workflow.add_node("generate", self.generate)
        
        workflow.set_entry_point("retrieve")
        workflow.add_edge("retrieve", "generate")
        workflow.add_edge("generate", END)
        
        return workflow.compile()


mcp = FastMCP("RAG")
rag_agent = RAGAgent()
milvus_address = os.getenv("MILVUS_ADDRESS", "tcp://milvus.milvus-system.svc.cluster.local:19530")
vector_store = create_vector_store_with_config(rag_agent.config_manager, uri=milvus_address)


@mcp.tool()
async def search_documents(query: str) -> str:
    """Search documents uploaded by the user and return relevant context.

    Retrieves relevant document chunks from the vector store based on the query.
    The main agent should use this context to formulate the final answer.

    Args:
        query: The question or query to search for.

    Returns:
        Retrieved document context relevant to the query.
    """
    config_obj = rag_agent.config_manager.read_config()
    sources = config_obj.selected_sources or []

    logger.info({"message": "Starting document search", "query": query, "sources": sources})

    # Retrieve documents directly without LLM generation
    if sources:
        retrieved_docs = vector_store.get_documents(query, sources=sources)
    else:
        retrieved_docs = vector_store.get_documents(query)

    # Fallback to all documents if source filtering returns nothing
    if not retrieved_docs and sources:
        logger.info({"message": "No documents found with source filtering, trying without filters"})
        retrieved_docs = vector_store.get_documents(query)

    if not retrieved_docs:
        logger.warning({"message": "No documents retrieved", "query": query})
        return f"No relevant documents found for query: '{query}'. Please ensure documents have been uploaded."

    # Format retrieved context for the main agent
    sources_found = set(doc.metadata.get("source", "unknown") for doc in retrieved_docs)
    logger.info({"message": "Documents retrieved", "sources": list(sources_found), "doc_count": len(retrieved_docs)})

    # Build context string with source attribution
    context_parts = []
    for i, doc in enumerate(retrieved_docs, 1):
        source = doc.metadata.get("source", "unknown")
        content = doc.page_content.strip()
        context_parts.append(f"[Document {i} - {source}]\n{content}")

    context = "\n\n".join(context_parts)

    logger.info({"message": "Search completed", "context_length": len(context), "doc_count": len(retrieved_docs)})
    return context


if __name__ == "__main__":
    print(f"Starting {mcp.name} MCP server...")
    mcp.run(transport="stdio")
