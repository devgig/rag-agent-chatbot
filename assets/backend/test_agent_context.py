"""Integration tests for ChatAgent multi-turn context."""
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

# Mock heavy dependencies that aren't needed for unit-testing agent logic
from unittest.mock import MagicMock
sys.modules.setdefault("asyncpg", MagicMock())
sys.modules.setdefault("redis", MagicMock())
sys.modules.setdefault("redis.asyncio", MagicMock())
sys.modules.setdefault("orjson", MagicMock())
sys.modules.setdefault("prometheus_fastapi_instrumentator", MagicMock())

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from dataclasses import dataclass

import pytest

from conversation_context import ConversationBuffer


@dataclass
class FakeDoc:
    page_content: str
    metadata: dict


class FakeChoice:
    def __init__(self, content=None, finish_reason=None):
        self.delta = MagicMock()
        self.delta.content = content
        self.delta.tool_calls = []
        self.finish_reason = finish_reason


class FakeChunk:
    def __init__(self, content=None, finish_reason=None, usage=None):
        self.choices = [FakeChoice(content, finish_reason)] if content or finish_reason else []
        self.usage = usage


class FakeUsage:
    prompt_tokens = 100
    completion_tokens = 50
    total_tokens = 150


class FakeStreamContext:
    def __init__(self, chunks):
        self._chunks = chunks

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        for c in self._chunks:
            yield c


@pytest.fixture
def mock_deps():
    vector_store = MagicMock()
    vector_store.embeddings = MagicMock()
    vector_store.get_documents = AsyncMock(return_value=[
        FakeDoc(page_content="Relevant document content.", metadata={"source": "test.pdf"})
    ])

    config_manager = MagicMock()
    config_manager.get_available_models.return_value = ["test-model"]
    config_manager.get_selected_model.return_value = "test-model"

    postgres_storage = MagicMock()
    postgres_storage.get_user_preferences = AsyncMock(return_value={"selected_sources": []})
    postgres_storage.get_messages = AsyncMock(return_value=[])
    postgres_storage.append_messages_to_chat = AsyncMock(return_value=[])

    return vector_store, config_manager, postgres_storage


def _make_agent(mock_deps):
    from agent import ChatAgent
    vector_store, config_manager, postgres_storage = mock_deps

    with patch.object(ChatAgent, 'set_current_model'):
        agent = ChatAgent(vector_store, config_manager, postgres_storage)
        agent.current_model = "test-model"
        agent.system_prompt_template = MagicMock()
        agent.system_prompt_template.render = MagicMock(return_value="System prompt")

        chunks = [
            FakeChunk(content="Test response"),
            FakeChunk(finish_reason="stop", usage=FakeUsage()),
        ]
        model_client = AsyncMock()
        model_client.chat.completions.create = AsyncMock(return_value=FakeStreamContext(chunks))
        agent.model_client = model_client
        agent.stream_callback = AsyncMock()
        agent._usage_accumulator = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        return agent, model_client


@pytest.mark.asyncio
async def test_generate_includes_history(mock_deps):
    """When history exists, it should appear in the LLM messages."""
    agent, model_client = _make_agent(mock_deps)

    agent.context_buffer.append("chat1", "What is Redpanda?", "Redpanda is a streaming platform.")

    from langchain_core.messages import HumanMessage
    state = {
        "chat_id": "chat1",
        "user_id": "user1",
        "messages": [HumanMessage(content="Tell me more about Redpanda")],
    }

    await agent.generate(state)

    call_args = model_client.chat.completions.create.call_args
    messages = call_args.kwargs["messages"]
    roles = [m["role"] for m in messages]
    assert roles.count("user") == 2
    assert "assistant" in roles


@pytest.mark.asyncio
async def test_generate_works_without_history(mock_deps):
    """First message in a chat should work with no history."""
    agent, model_client = _make_agent(mock_deps)

    from langchain_core.messages import HumanMessage
    state = {
        "chat_id": "chat1",
        "user_id": "user1",
        "messages": [HumanMessage(content="Hello")],
    }

    await agent.generate(state)

    call_args = model_client.chat.completions.create.call_args
    messages = call_args.kwargs["messages"]
    roles = [m["role"] for m in messages]
    assert roles == ["system", "user"]


@pytest.mark.asyncio
async def test_generate_loads_from_postgres_on_cold_start(mock_deps):
    """On buffer miss, history should be loaded from Postgres."""
    vector_store, config_manager, postgres_storage = mock_deps

    from langchain_core.messages import HumanMessage, AIMessage
    postgres_storage.get_messages = AsyncMock(return_value=[
        HumanMessage(content="Prior question"),
        AIMessage(content="Prior answer"),
    ])

    agent, model_client = _make_agent(mock_deps)

    state = {
        "chat_id": "chat1",
        "user_id": "user1",
        "messages": [HumanMessage(content="Follow up question")],
    }

    await agent.generate(state)

    postgres_storage.get_messages.assert_called_once()
    call_args = model_client.chat.completions.create.call_args
    messages = call_args.kwargs["messages"]
    roles = [m["role"] for m in messages]
    assert "assistant" in roles


@pytest.mark.asyncio
async def test_buffer_updated_after_generate(mock_deps):
    """After generate completes, the buffer should contain the new turn."""
    agent, _ = _make_agent(mock_deps)

    from langchain_core.messages import HumanMessage
    state = {
        "chat_id": "chat1",
        "user_id": "user1",
        "messages": [HumanMessage(content="New question")],
    }

    await agent.generate(state)

    history = agent.context_buffer.get("chat1")
    assert history is not None
    assert len(history) == 2
    assert history[0] == {"role": "user", "content": "New question"}
    assert history[1] == {"role": "assistant", "content": "Test response"}


@pytest.mark.asyncio
async def test_no_shared_state_between_chats(mock_deps):
    """Different chats should have independent buffers."""
    agent, _ = _make_agent(mock_deps)

    agent.context_buffer.append("chatA", "Question A", "Answer A")
    agent.context_buffer.append("chatB", "Question B", "Answer B")

    histA = agent.context_buffer.get("chatA")
    histB = agent.context_buffer.get("chatB")

    assert histA[0]["content"] == "Question A"
    assert histB[0]["content"] == "Question B"
    assert histA != histB


@pytest.mark.asyncio
async def test_no_extra_embedding_call(mock_deps):
    """generate() should NOT make its own embedding call — only Milvus does that internally."""
    vector_store, _, _ = mock_deps
    vector_store.embeddings.aembed_query = AsyncMock()

    agent, _ = _make_agent(mock_deps)

    from langchain_core.messages import HumanMessage
    state = {
        "chat_id": "chat1",
        "user_id": "user1",
        "messages": [HumanMessage(content="Hello")],
    }

    await agent.generate(state)

    vector_store.embeddings.aembed_query.assert_not_called()
