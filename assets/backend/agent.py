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
"""ChatAgent implementation for LLM-powered conversational AI with direct RAG pipeline."""

import asyncio
import contextlib
import re
import time
from typing import AsyncIterator, List, Dict, Any, TypedDict, Optional, Callable, Awaitable

from langchain_core.messages import HumanMessage, AIMessage, AnyMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
import httpx
from openai import AsyncOpenAI
from opentelemetry import trace

from logger import logger
from prompts import Prompts
from postgres_storage import PostgreSQLConversationStorage


SENTINEL = object()
StreamCallback = Callable[[Dict[str, Any]], Awaitable[None]]

LLM_REQUEST_TIMEOUT = 120.0

# Tokens that an attacker-controlled document could embed to try to break out
# of the <document>…</document> frame the system prompt sets up. We strip /
# neutralize these before injecting any retrieved content. This is defense in
# depth on top of the "treat the following as untrusted" framing in the
# system prompt.
_PROMPT_BREAKOUT_PATTERNS = [
    # Closing-tag escapes from the document fence
    (re.compile(r"</\s*document\s*>", re.IGNORECASE), "&lt;/document&gt;"),
    (re.compile(r"<\s*document\b[^>]*>", re.IGNORECASE), "&lt;document&gt;"),
    # Common chat-template control sequences different models use to switch role
    (re.compile(r"<\|im_start\|>"), "&lt;|im_start|&gt;"),
    (re.compile(r"<\|im_end\|>"), "&lt;|im_end|&gt;"),
    (re.compile(r"<\|system\|>", re.IGNORECASE), "&lt;|system|&gt;"),
    (re.compile(r"<\|user\|>", re.IGNORECASE), "&lt;|user|&gt;"),
    (re.compile(r"<\|assistant\|>", re.IGNORECASE), "&lt;|assistant|&gt;"),
    # <think>...</think> blocks — Nemotron-class models use these as
    # reasoning markers; stripping them from input prevents a poisoned doc
    # from hiding instructions inside a "reasoning" block the model would
    # otherwise filter out of its visible output.
    (re.compile(r"<\s*/?\s*think\s*>", re.IGNORECASE), "&lt;think&gt;"),
]


def _neutralize_doc_content(text: str) -> str:
    """Strip prompt-injection breakout sequences from retrieved doc content.

    Replaces XML-like control tokens with HTML-entity escapes so the model
    sees them as literal text instead of as role/scope changes. The output
    is still readable for the user (the visible content is unchanged), but
    can't be mistaken for an instruction frame by the chat-template parser.
    """
    if not text:
        return text
    for pattern, replacement in _PROMPT_BREAKOUT_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class State(TypedDict, total=False):
    messages: List[AnyMessage]
    chat_id: Optional[str]
    user_id: Optional[str]


class ChatAgent:
    """Conversational agent with direct RAG pipeline.

    Performs document search and LLM generation in a single graph node,
    bypassing the former MCP subprocess architecture.
    """

    def __init__(self, vector_store, config_manager, postgres_storage: PostgreSQLConversationStorage):
        self.vector_store = vector_store
        self.config_manager = config_manager
        self.conversation_store = postgres_storage
        self.current_model = None
        self.model_client: Optional[AsyncOpenAI] = None
        self.system_prompt_template = None

        self.graph = self._build_graph()
        self.stream_callback = None
        self.last_state = None

    @classmethod
    async def create(cls, vector_store, config_manager, postgres_storage: PostgreSQLConversationStorage):
        """Create and initialize a ChatAgent instance."""
        agent = cls(vector_store, config_manager, postgres_storage)
        agent.system_prompt_template = Prompts.get_template("supervisor_agent")
        agent.set_current_model(config_manager.get_selected_model())
        logger.debug("Agent initialized with direct RAG pipeline.")
        return agent

    def set_current_model(self, model_name: str) -> None:
        """Set the current model and create a new AsyncOpenAI client."""
        available_models = self.config_manager.get_available_models()

        if model_name not in available_models:
            raise ValueError(f"Model {model_name} is not available. Available models: {available_models}")

        self.current_model = model_name
        logger.info(f"Switched to model: {model_name}")
        self.model_client = AsyncOpenAI(
            base_url=f"http://{self.current_model}:8000/v1",
            api_key="api_key",
            timeout=httpx.Timeout(LLM_REQUEST_TIMEOUT, connect=10.0),
        )

    async def generate(self, state: State) -> Dict[str, Any]:
        """Search documents and generate AI response in a single pass.

        Replaces the former two-iteration flow (generate→tool_node→generate)
        with inline vector search followed by a single LLM call.
        """
        await self.stream_callback({'type': 'node_start', 'data': 'generate'})

        user_query = self._extract_user_query(state)
        user_id = state.get("user_id")
        if not user_id:
            raise ValueError("agent.generate requires user_id in state")
        logger.debug({
            "message": "GRAPH: generate — inline search + LLM",
            "chat_id": state.get("chat_id"),
            "user_id": user_id,
            "query": user_query[:100],
        })

        tracer = trace.get_tracer("rag-agent.generate")
        parent_span = trace.get_current_span()
        parent_ctx = trace.set_span_in_context(parent_span)

        # --- Document search ---
        with tracer.start_as_current_span("retrieval", context=parent_ctx) as retrieval_span:
            prefs = await self.conversation_store.get_user_preferences(user_id)
            sources = prefs.get("selected_sources") or []
            retrieval_span.set_attribute("retrieval.selected_sources", sources)

            k_target = 5
            retrieved_docs = await self.vector_store.get_documents(
                user_query,
                user_id,
                k=k_target,
                selected_sources=sources or None,
            )

            retrieval_span.set_attribute("retrieval.doc_count", len(retrieved_docs))
            if retrieved_docs:
                returned_sources = list({doc.metadata.get("source", "unknown") for doc in retrieved_docs})
                retrieval_span.set_attribute("retrieval.returned_sources", returned_sources)

        logger.info({
            "message": "Source filter for retrieval",
            "user_id": user_id,
            "selected_sources": sources,
        })

        with tracer.start_as_current_span("format_context", context=parent_ctx) as ctx_span:
            if retrieved_docs:
                returned_sources = list({doc.metadata.get("source", "unknown") for doc in retrieved_docs})
                logger.info({
                    "message": "Retrieved docs source check",
                    "requested_sources": sources,
                    "returned_sources": returned_sources,
                    "doc_count": len(retrieved_docs),
                })
                context_parts = ["Document context (untrusted reference material):"]
                for i, doc in enumerate(retrieved_docs, 1):
                    source = doc.metadata.get("source", "unknown")
                    content = _neutralize_doc_content(doc.page_content.strip())
                    safe_source = _neutralize_doc_content(source)
                    context_parts.append(
                        f'<document index="{i}" source="{safe_source}">\n'
                        f"{content}\n"
                        f"</document>"
                    )
                context_str = "\n\n".join(context_parts)
                ctx_span.set_attribute("context.length", len(context_str))
                ctx_span.set_attribute("context.doc_count", len(retrieved_docs))
                logger.info({
                    "message": "Documents retrieved for RAG",
                    "doc_count": len(retrieved_docs),
                    "context_length": len(context_str),
                })
            else:
                context_str = "No document context available for this query."
                ctx_span.set_attribute("context.length", 0)
                ctx_span.set_attribute("context.doc_count", 0)
                logger.warning({"message": "No documents retrieved", "query": user_query})

        # --- LLM call with context baked into system prompt ---
        system_prompt = self.system_prompt_template.render({"context": context_str})
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_query},
        ]

        with tracer.start_as_current_span("llm_streaming", context=parent_ctx) as llm_span:
            llm_span.set_attribute("llm.model_name", self.current_model)

            t0 = time.monotonic()
            stream = await self.model_client.chat.completions.create(
                model=self.current_model,
                messages=messages,
                temperature=0,
                top_p=1,
                stream=True,
                stream_options={"include_usage": True},
                extra_body={"chat_template_kwargs": {"enable_thinking": False}},
            )

            llm_output_buffer, _, usage = await self._stream_response(stream, self.stream_callback)
            stream_duration = time.monotonic() - t0

            llm_span.set_attribute("llm.stream_duration_ms", round(stream_duration * 1000, 1))
            if usage:
                self._usage_accumulator["prompt_tokens"] += getattr(usage, "prompt_tokens", 0)
                self._usage_accumulator["completion_tokens"] += getattr(usage, "completion_tokens", 0)
                self._usage_accumulator["total_tokens"] += getattr(usage, "total_tokens", 0)
                llm_span.set_attribute("llm.token_count.prompt", self._usage_accumulator["prompt_tokens"])
                llm_span.set_attribute("llm.token_count.completion", self._usage_accumulator["completion_tokens"])
                llm_span.set_attribute("llm.token_count.total", self._usage_accumulator["total_tokens"])

        raw_output = "".join(llm_output_buffer)

        if self._usage_accumulator.get("total_tokens", 0) > 0:
            await self.stream_callback({"type": "usage", "data": dict(self._usage_accumulator)})

        response = AIMessage(content=raw_output)

        logger.debug({
            "message": "GRAPH: generate complete",
            "chat_id": state.get("chat_id"),
            "response_length": len(raw_output),
        })
        await self.stream_callback({'type': 'node_end', 'data': 'generate'})
        return {"messages": state.get("messages", []) + [response]}

    def _build_graph(self) -> StateGraph:
        """Build a single-node graph: START → generate → END."""
        workflow = StateGraph(State)
        workflow.add_node("generate", self.generate)
        workflow.add_edge(START, "generate")
        workflow.add_edge("generate", END)
        return workflow.compile()

    @staticmethod
    def _extract_user_query(state: State) -> str:
        """Return the last HumanMessage content from the state."""
        for msg in reversed(state.get("messages", [])):
            if isinstance(msg, HumanMessage):
                return msg.content
        return ""

    async def _stream_response(self, stream, stream_callback: StreamCallback) -> tuple[List[str], Dict[int, Dict[str, str]], Any]:
        """Process streaming LLM response and extract content and tool calls.

        Args:
            stream: Async stream from LLM
            stream_callback: Callback for streaming events

        Returns:
            Tuple of (content_buffer, tool_calls_buffer, usage)
        """
        llm_output_buffer = []
        tool_calls_buffer = {}
        saw_tool_finish = False
        usage = None
        _in_think_block = False
        _seen_first_content = False
        _pending = ""
        _stream_start = time.monotonic()
        _ttft_recorded = False

        async for chunk in stream:
            if hasattr(chunk, "usage") and chunk.usage is not None:
                usage = chunk.usage

            if saw_tool_finish:
                continue

            for choice in getattr(chunk, "choices", []) or []:
                delta = getattr(choice, "delta", None)
                if not delta:
                    continue

                content = getattr(delta, "content", None)
                if content:
                    # On first content chunk, check if model starts with <think>
                    if not _seen_first_content:
                        _seen_first_content = True
                        stripped = content.lstrip()
                        if stripped.startswith("<think>"):
                            _in_think_block = True
                            content = stripped[len("<think>"):]
                            if not content:
                                continue

                    # --- strip <think>…</think> blocks from streamed output ---
                    _pending += content
                    emit = ""

                    while _pending:
                        if _in_think_block:
                            # Look for closing </think> tag
                            close_idx = _pending.find("</think>")
                            if close_idx != -1:
                                _pending = _pending[close_idx + len("</think>"):]
                                _in_think_block = False
                                continue
                            # Check for partial </think> at the end of buffer
                            if _pending.endswith(("<", "</", "</t", "</th", "</thi", "</thin", "</think", "</think>")):
                                break  # wait for more data
                            _pending = ""
                            break
                        else:
                            # Look for opening <think> tag
                            open_idx = _pending.find("<think>")
                            if open_idx != -1:
                                emit += _pending[:open_idx]
                                _pending = _pending[open_idx + len("<think>"):]
                                _in_think_block = True
                                continue
                            # Check for partial <think> at the end of buffer
                            for i in range(1, min(len("<think>"), len(_pending) + 1)):
                                if _pending.endswith("<think>"[:i]):
                                    emit += _pending[:-i]
                                    _pending = _pending[-i:]
                                    break
                            else:
                                emit += _pending
                                _pending = ""
                            break

                    if emit:
                        if not _ttft_recorded:
                            _ttft_recorded = True
                            ttft_ms = round((time.monotonic() - _stream_start) * 1000, 1)
                            span = trace.get_current_span()
                            if span.is_recording():
                                span.set_attribute("llm.ttft_ms", ttft_ms)
                                span.add_event("first_token", {"ttft_ms": ttft_ms})
                        await stream_callback({"type": "token", "data": emit})
                        llm_output_buffer.append(emit)
                for tc in getattr(delta, "tool_calls", []) or []:
                    idx = getattr(tc, "index", None)
                    if idx is None:
                        idx = 0 if not tool_calls_buffer else max(tool_calls_buffer) + 1
                    entry = tool_calls_buffer.setdefault(idx, {"id": None, "name": None, "arguments": ""})

                    if getattr(tc, "id", None):
                        entry["id"] = tc.id

                    fn = getattr(tc, "function", None)
                    if fn:
                        if getattr(fn, "name", None):
                            entry["name"] = fn.name
                        if getattr(fn, "arguments", None):
                            entry["arguments"] += fn.arguments

                finish_reason = getattr(choice, "finish_reason", None)
                if finish_reason == "tool_calls":
                    saw_tool_finish = True
                    break

        return llm_output_buffer, tool_calls_buffer, usage

    async def query(
        self, query_text: str, chat_id: str, user_id: str
    ) -> AsyncIterator[Dict[str, Any]]:
        """Process user query and stream response tokens.

        Args:
            query_text: User's input text
            chat_id: Unique chat identifier
            user_id: Authenticated user (from JWT sub)

        Yields:
            Streaming events and tokens
        """
        logger.debug({
            "message": "GRAPH: STARTING EXECUTION",
            "chat_id": chat_id,
            "user_id": user_id,
            "query": query_text[:100] + "..." if len(query_text) > 100 else query_text,
            "graph_flow": "START → generate → END"
        })

        try:
            initial_state = {
                "chat_id": chat_id,
                "user_id": user_id,
                "messages": [HumanMessage(content=query_text)],
            }

            model_name = self.config_manager.get_selected_model()
            if self.current_model != model_name:
                self.set_current_model(model_name)

            logger.debug({
                "message": "GRAPH: LAUNCHING EXECUTION",
                "chat_id": chat_id,
                "message_count": len(initial_state["messages"]),
            })

            self.last_state = None
            self._usage_accumulator = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
            token_q: asyncio.Queue[Any] = asyncio.Queue()
            self.stream_callback = lambda event: self._queue_writer(event, token_q)
            runner = asyncio.create_task(self._run_graph(initial_state, chat_id, user_id, token_q))

            try:
                while True:
                    item = await token_q.get()
                    if item is SENTINEL:
                        break
                    yield item
            except Exception as stream_error:
                logger.error({"message": "Error in streaming", "error": str(stream_error)}, exc_info=True)
            finally:
                with contextlib.suppress(asyncio.CancelledError):
                    await runner

                logger.debug({
                    "message": "GRAPH: EXECUTION COMPLETED",
                    "chat_id": chat_id,
                    "has_response": bool(self.last_state.get("messages")) if self.last_state else False
                })

        except Exception as e:
            logger.error({"message": "GRAPH: EXECUTION FAILED", "error": str(e), "chat_id": chat_id}, exc_info=True)
            yield {"type": "error", "data": f"Error performing query: {str(e)}"}


    async def _queue_writer(self, event: Dict[str, Any], token_q: asyncio.Queue) -> None:
        """Write events to the streaming queue.
        
        Args:
            event: Event data to queue
            token_q: Queue for streaming events
        """
        await token_q.put(event)

    async def _run_graph(
        self,
        initial_state: Dict[str, Any],
        chat_id: str,
        user_id: str,
        token_q: asyncio.Queue,
    ) -> None:
        """Run the graph execution in background task.

        After the graph completes, ``done`` + SENTINEL are pushed immediately
        so the SSE stream closes without waiting for persistence.  The
        Postgres/Redis save is kicked off as a fire-and-forget task so the
        UI never stalls on I/O.
        """
        try:
            async for final_state in self.graph.astream(
                initial_state,
                stream_mode="values",
                stream_writer=lambda event: self._queue_writer(event, token_q)
            ):
                self.last_state = final_state
        finally:
            await token_q.put({"type": "done"})
            await token_q.put(SENTINEL)

            if self.last_state and self.last_state.get("messages"):
                asyncio.create_task(
                    self._persist_conversation(chat_id, user_id)
                )

    async def _persist_conversation(self, chat_id: str, user_id: str) -> None:
        """Save the completed turn to Postgres + Redis (fire-and-forget)."""
        try:
            new_messages = [
                msg for msg in self.last_state["messages"]
                if not isinstance(msg, SystemMessage)
            ]
            await self.conversation_store.append_messages_to_chat(
                user_id, chat_id, new_messages
            )
        except Exception as exc:
            logger.warning({
                "message": "Failed to persist conversation",
                "chat_id": chat_id,
                "user_id": user_id,
                "error": str(exc),
            })
