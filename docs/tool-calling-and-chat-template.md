# Tool Calling and Chat Template

How tool calling works in this project and how the `search_documents` tool is integrated end-to-end.

## Tool Calling with Nemotron-Super-49B

Llama-3.3-Nemotron-Super-49B natively supports tool calling through its built-in chat template. vLLM provides a dedicated `llama_nemotron_json` parser that handles the model's native JSON-based tool call format.

### vLLM Configuration

Two deployment flags enable tool calling:

```yaml
args:
  - "--tool-call-parser=llama_nemotron_json"          # Parse Nemotron's native tool call output
  - "--enable-auto-tool-choice"                        # Let model decide when to call tools
```

The `llama_nemotron_json` parser tells vLLM to look for tool call patterns in the model's output and convert them into OpenAI-compatible tool call objects in the streaming response. No custom chat template is needed — vLLM uses the model's built-in tokenizer chat template automatically.

## Tool Calling Pipeline

The project has exactly one tool: `search_documents`. Here is the full request flow:

```
User Message
  │
  ▼
Backend (agent.py) ── formats messages with system prompt ──▶ vLLM
  │                                                              │
  │  vLLM applies chat template:                                 │
  │  1. Injects tool definitions into system message              │
  │  2. Formats conversation with model's native template         │
  │  3. Model generates JSON tool call output                     │
  │  4. llama_nemotron_json parser converts to OpenAI tool_calls  │
  │                                                              │
  ◀──────────── streaming response with tool_calls ──────────────┘
  │
  ▼
Backend executes tool_node
  │
  ▼
MCP Client (client.py) ── stdio ──▶ RAG MCP Server (rag.py)
                                       │
                                       ├── Read selected sources from config
                                       ├── Query Milvus vector store (top-k=8)
                                       ├── Filter by relevance score threshold
                                       └── Return formatted context with source attribution
  │
  ◀──────────── tool result (document chunks) ──────────────────┘
  │
  ▼
Backend (agent.py) ── sends tool result back to vLLM ──▶ vLLM
  │                                                          │
  │  Model generates final answer grounded in retrieved docs  │
  │                                                          │
  ◀──────────── streaming response (text tokens) ────────────┘
  │
  ▼
WebSocket ──▶ Frontend (token-by-token rendering)
```

## Component Details

### 1. Agent Tool Initialization (Backend)

**File:** `assets/backend/agent.py` — `init_tools()`

On startup, the backend:
1. Connects to the RAG MCP server via stdio
2. Retrieves available tools (retries up to 10 times with exponential backoff)
3. Converts tools to OpenAI function-calling format using `convert_to_openai_tool()`
4. Stores tools in `openai_tools` (for the API) and `tools_by_name` (for execution)

### 2. Forced Tool Call on First Iteration

**File:** `assets/backend/agent.py` — `generate()`

The agent forces `search_documents` on the first iteration of every query:

```python
if iterations == 0 and self.tools_by_name.get("search_documents"):
    tool_choice = {"type": "function", "function": {"name": "search_documents"}}
else:
    tool_choice = "auto"
```

This ensures the model always retrieves document context before answering, preventing it from using its own knowledge. On subsequent iterations (if any), `tool_choice` is set to `"auto"` so the model can choose to answer directly with the retrieved context.

### 3. MCP Server Configuration

**File:** `assets/backend/client.py`

The only registered MCP server:

```python
self.server_configs = {
    "rag-server": {
        "command": python_exe,
        "args": ["tools/mcp_servers/rag.py"],
        "transport": "stdio",
        "env": mcp_env,
    }
}
```

### 4. search_documents Tool

**File:** `assets/backend/tools/mcp_servers/rag.py`

The tool:
1. Reads selected sources from `config.json`
2. Queries the Milvus vector store with the user's query
3. Falls back to searching all documents if source-filtered results are empty
4. Returns formatted chunks with source attribution: `[Document 1 - filename.pdf]\n<content>`

### 5. System Prompt

**File:** `assets/backend/prompts.py`

The system prompt enforces document-grounded behavior:
- **Must** call `search_documents` for every question
- **Must** answer only from retrieved context
- **Must not** use the model's own knowledge
- **Must** say "I couldn't find information about that" when results are irrelevant

## LangGraph State Machine

The agent runs a loop with a maximum of 3 iterations:

```
START → generate → should_continue?
                      │
                 YES (has tool calls)  →  tool_node  →  generate  → ...
                      │
                 NO (text response or max iterations)  →  END
```

Typical flow for a RAG query:
1. **generate** (iteration 0): Model is forced to call `search_documents` → emits tool call
2. **tool_node**: Executes `search_documents` → returns document chunks
3. **generate** (iteration 1): Model receives chunks → generates grounded answer → no tool calls → END

## Why This Architecture

| Decision | Reason |
|----------|--------|
| Native tool calling over custom chat templates | Nemotron-Super-49B supports tool calling natively with the `llama_nemotron_json` parser — no custom template maintenance needed |
| `llama_nemotron_json` parser | NVIDIA's dedicated parser for Nemotron's native JSON tool call format, providing excellent BFCL benchmark scores (71.75% on BFCL v3) |
| MCP over direct function calls | Clean process isolation, standard protocol, easy to add tools without modifying agent code |
| Forced first tool call | Prevents the model from confidently answering from its training data instead of searching documents |
| Single tool (search_documents) | The project is a focused RAG chatbot — one tool keeps the pipeline simple and reliable |
