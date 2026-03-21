# RAG Grounding Constraints: Forcing Document-Only Responses

This document describes every mechanism implemented to ensure the LLM **only** answers questions using content retrieved from uploaded document embeddings, and never falls back to its general training knowledge.

---

## Overview

The system uses a defense-in-depth strategy across nine layers: prompt instructions, forced tool calling, relevance filtering, MCP tool boundaries, generation prompt constraints, iteration limits, state machine architecture, architectural isolation, and deterministic sampling. Each layer independently prevents the model from answering outside the scope of uploaded documents.

---

## 1. System Prompt Constraints

**File:** `assets/backend/prompts.py` (lines 21-46)

The system prompt is the first line of defense. It establishes the model's identity as a **document-grounded assistant** with explicit prohibitions:

```
You are a document-grounded assistant. You answer questions ONLY using
uploaded documents. You have NO general knowledge.
```

### Critical Rules (injected into every request)

| Rule | Constraint |
|------|-----------|
| 1 | For EVERY user question, you MUST call `search_documents` first. No exceptions. |
| 2 | Your answers must come ONLY from `search_documents` results. You have no other knowledge. |
| 3 | If no relevant results, respond ONLY with: "I couldn't find information about that in your uploaded documents..." |
| 4 | NEVER answer from your own knowledge, even if you know the answer. You are not a general-purpose assistant. |
| 5 | NEVER perform calculations, provide facts, or give advice that is not directly from the documents. |

### Output Protocol

The prompt also includes instructions to handle edge cases where retrieved results are tangentially related:

- When results **directly answer** the question: MUST use them.
- When results **do NOT** contain relevant information: MUST respond with the "couldn't find" message.
- When results discuss a **different topic** but share surface-level keywords: treat as irrelevant.
- **Do NOT fill in gaps** with the model's own knowledge.

---

## 2. Forced Tool Calling on First Iteration (Fast Path)

**File:** `assets/backend/agent.py` (lines 273-298)

Even with strong prompt instructions, the model could theoretically skip calling `search_documents` and answer directly. This is prevented at the code level by bypassing the LLM entirely on the first iteration:

```python
if iterations == 0 and self.tools_by_name.get("search_documents"):
    user_query = self._extract_user_query(state)
    tool_call_id = f"call_fast_{uuid.uuid4().hex[:8]}"
    response = AIMessage(
        content="",
        tool_calls=[ToolCall(
            name="search_documents",
            args={"query": user_query},
            id=tool_call_id,
        )],
    )
    return {"messages": state.get("messages", []) + [response]}
```

On the **first iteration** (iteration 0), the agent constructs the `search_documents` tool call directly in Python without invoking the LLM at all. The model has no opportunity to skip retrieval — the tool call is hardcoded. On subsequent iterations, the LLM runs with `tool_choice="auto"` so it can produce its final text response.

This is the strongest technical guarantee — the LLM is never even consulted about whether to retrieve documents. As a side benefit, this eliminates a ~10s LLM round-trip that was previously wasted generating a predetermined tool call.

---

## 3. Relevance Score Threshold

**File:** `assets/backend/vector_store.py` (lines 33, 306-332)

Retrieved document chunks are filtered by a configurable similarity threshold before reaching the LLM:

```python
RELEVANCE_SCORE_THRESHOLD = float(os.getenv("RELEVANCE_SCORE_THRESHOLD", "0.4"))
```

The `get_documents()` method uses `similarity_search_with_relevance_scores` which returns normalized scores on a [0, 1] scale (1 = most relevant). Chunks below the threshold are dropped:

```python
filtered = [
    (doc, score)
    for doc, score in results_with_scores
    if score >= RELEVANCE_SCORE_THRESHOLD
]
```

This prevents the model from receiving tangentially related content that it might use to construct a plausible-sounding but unsupported answer. The threshold is tunable via the `RELEVANCE_SCORE_THRESHOLD` environment variable.

---

## 4. MCP Tool: search_documents

**File:** `assets/backend/tools/mcp_servers/rag.py` (lines 214-261)

The `search_documents` tool is the **sole pathway** for the model to access document content. It is implemented as an MCP (Model Context Protocol) server tool, which means:

- The model cannot access the vector store directly — it must go through this tool.
- The tool enforces source filtering based on user configuration.
- Results include explicit source attribution: `[Document i - {source}]`.
- When no documents are found, the tool returns a clear message rather than empty content:

```
No relevant documents found for query: '{query}'.
Please ensure documents have been uploaded.
```

### Source Filtering

The tool respects user-selected document sources from configuration:

```python
config_obj = rag_agent.config_manager.read_config()
sources = config_obj.selected_sources or []

if sources:
    retrieved_docs = vector_store.get_documents(query, sources=sources)
else:
    retrieved_docs = vector_store.get_documents(query)
```

If source-filtered retrieval returns nothing, it falls back to searching all documents before concluding no results exist.

---

## 5. RAG Agent Generation Prompt (Secondary Path)

**File:** `assets/backend/tools/mcp_servers/rag.py` (lines 107-116)

The `RAGAgent` class (used internally by the MCP server) has its own generation prompt that reinforces the same constraints:

```
You are an assistant for question-answering tasks.
Use ONLY the following pieces of retrieved context to answer the question.
If no relevant context is provided, state that no relevant information
was found in the uploaded documents. Do NOT answer from your own knowledge.
Don't make up any information that is not provided in the context.
```

This ensures that even in the internal RAG pipeline (retrieve → generate), the generation step is constrained to only use retrieved context.

---

## 6. Maximum Iteration Limit

**File:** `assets/backend/agent.py` (lines 43, 192-205)

The agent state machine is capped at 3 iterations:

```python
MAX_ITERATIONS = 3
```

The `should_continue()` method enforces this:

```python
if iterations >= self.max_iterations:
    return "end"
```

This prevents the model from entering an extended tool-calling loop where it might progressively refine queries to find tangentially related content and synthesize an answer from its own knowledge combined with loosely related chunks.

---

## 7. LangGraph State Machine Architecture

**File:** `assets/backend/agent.py` (lines 357-378)

The conversation flow is controlled by a LangGraph state machine with a fixed topology:

```
START → generate → should_continue → [action → generate] → END
```

- **generate**: Calls the LLM (with forced tool choice on first pass).
- **should_continue**: Checks for tool calls and iteration limits.
- **action (tool_node)**: Executes tool calls and returns results.

There is no bypass path. The model cannot skip the state machine or inject additional nodes. Tool results are added as `ToolMessage` objects in the message history, keeping them distinct from model-generated content.

---

## 8. Architectural Isolation

Several architectural decisions prevent knowledge leakage:

| Component | Isolation Mechanism |
|-----------|---------------------|
| **Embedding model** | Qwen3-Embedding-4B running locally — no external API calls that could inject knowledge |
| **Vector database** | Self-hosted Milvus — no shared/public collections |
| **LLM inference** | Self-hosted via vLLM — no external knowledge augmentation |
| **MCP transport** | stdio-based — tools communicate only through structured messages |
| **No web access** | No search tools, no URL fetching — the model cannot access external information |

---

## 9. Temperature and Sampling

**File:** `assets/backend/agent.py` (line 310)

The LLM is called with `temperature=0` and `top_p=1`:

```python
stream = await self.model_client.chat.completions.create(
    model=self.current_model,
    messages=messages,
    temperature=0,
    top_p=1,
    ...
)
```

While not a grounding constraint per se, deterministic sampling reduces the model's tendency to hallucinate or creatively extrapolate beyond provided context.

---

## Summary of Constraint Layers

| Layer | Mechanism | Prevents |
|-------|-----------|----------|
| **Prompt** | System prompt with explicit rules | Model choosing to use general knowledge |
| **Forced tool call** | Fast-path: tool call constructed in Python, LLM bypassed on iteration 0 | Model skipping retrieval entirely |
| **Relevance threshold** | Score filtering at 0.4 cutoff | Low-quality/tangential chunks reaching the model |
| **MCP tool boundary** | `search_documents` as sole data pathway | Direct vector store access or knowledge injection |
| **Generation prompt** | Secondary prompt in RAG pipeline | Internal generation using general knowledge |
| **Iteration cap** | Max 3 iterations | Loop-based constraint escape |
| **State machine** | Fixed LangGraph topology | Bypassing the retrieve-then-answer flow |
| **Architecture** | Local models, self-hosted DB, no web access | External knowledge sources |
| **Sampling** | temperature=0, top_p=1 | Creative hallucination beyond context |
