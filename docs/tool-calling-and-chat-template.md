# Tool Calling and Chat Template

How tool calling works in this project, why a custom chat template is required, and how the `search_documents` tool is integrated end-to-end.

## The Problem

Qwen2.5-VL-7B-Instruct-AWQ is a vision-language model. Its default chat template supports images and video but **does not support tool calling**. Without tool calling, the LLM cannot invoke `search_documents` to retrieve document context, which breaks the entire RAG pipeline.

## The Solution: Custom Chat Template

A custom Jinja2 chat template (`qwen-chat-template.yaml`) merges Qwen's vision support with [Hermes-style tool calling](https://huggingface.co/NousResearch/Hermes-2-Pro-Llama-3-8B). This template is mounted into the vLLM pod as a ConfigMap and referenced via the `--chat-template` flag.

### How It Works

The template controls how vLLM formats the conversation before sending it to the model. It handles three concerns:

**1. System prompt with tool definitions**

When tools are provided, the template injects them into the system message:

```
<|im_start|>system
You are a document-grounded assistant...

# Tools

You may call one or more functions to assist with the user query.

You are provided with function signatures within <tools></tools> XML tags:
<tools>
{"name": "search_documents", "parameters": {"query": {"type": "string"}}}
</tools>

For each function call, return a json object with function name and arguments
within <tool_call></tool_call> XML tags:
<tool_call>
{"name": "search_documents", "arguments": {"query": "..."}}
</tool_call>
<|im_end|>
```

**2. Vision tokens**

Images and videos in messages are replaced with Qwen's special tokens (`<|vision_start|><|image_pad|><|vision_end|>`), preserving multimodal capabilities.

**3. Tool call/response formatting**

- Assistant tool calls are wrapped in `<tool_call>` XML tags
- Tool results are wrapped in `<tool_response>` tags and presented as user messages (Hermes convention)

### vLLM Configuration

Two deployment flags enable tool calling:

```yaml
args:
  - "--chat-template=/chat-template/chat_template.jinja"   # Custom template
  - "--tool-call-parser=hermes"                             # Parse <tool_call> XML
  - "--enable-auto-tool-choice"                             # Let model decide when to call tools
```

The `hermes` parser tells vLLM to look for `<tool_call>` XML in the model's output and convert it into OpenAI-compatible tool call objects in the streaming response.

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
  │  2. Formats conversation with <|im_start|>/<|im_end|> tokens │
  │  3. Model generates <tool_call> XML                           │
  │  4. Hermes parser converts XML to OpenAI tool_call objects    │
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

### 1. Chat Template (Kubernetes ConfigMap)

**File:** `kustomize/models/base/qwen-chat-template.yaml`

A ConfigMap containing the Jinja2 template. Mounted into the vLLM pod at `/chat-template/chat_template.jinja`.

Source: [edwardzjl/chat-templates](https://github.com/edwardzjl/chat-templates/blob/main/qwen2_5/chat_template.jinja)

### 2. Agent Tool Initialization (Backend)

**File:** `assets/backend/agent.py` — `init_tools()`

On startup, the backend:
1. Connects to the RAG MCP server via stdio
2. Retrieves available tools (retries up to 10 times with exponential backoff)
3. Converts tools to OpenAI function-calling format using `convert_to_openai_tool()`
4. Stores tools in `openai_tools` (for the API) and `tools_by_name` (for execution)

### 3. Forced Tool Call on First Iteration

**File:** `assets/backend/agent.py` — `generate()`

The agent forces `search_documents` on the first iteration of every query:

```python
if iterations == 0 and self.tools_by_name.get("search_documents"):
    tool_choice = {"type": "function", "function": {"name": "search_documents"}}
else:
    tool_choice = "auto"
```

This ensures the model always retrieves document context before answering, preventing it from using its own knowledge. On subsequent iterations (if any), `tool_choice` is set to `"auto"` so the model can choose to answer directly with the retrieved context.

### 4. MCP Server Configuration

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

### 5. search_documents Tool

**File:** `assets/backend/tools/mcp_servers/rag.py`

The tool:
1. Reads selected sources from `config.json`
2. Queries the Milvus vector store with the user's query
3. Falls back to searching all documents if source-filtered results are empty
4. Returns formatted chunks with source attribution: `[Document 1 - filename.pdf]\n<content>`

### 6. System Prompt

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
| Custom chat template over fine-tuning | Zero training cost, works with any Qwen2.5-VL checkpoint, easy to update |
| Hermes-style XML over native function calling | Qwen2.5-VL wasn't trained with native tool calling; Hermes XML is a well-tested convention that works with instruction-tuned models |
| MCP over direct function calls | Clean process isolation, standard protocol, easy to add tools without modifying agent code |
| Forced first tool call | Prevents the model from confidently answering from its training data instead of searching documents |
| Single tool (search_documents) | The project is a focused RAG chatbot — one tool keeps the pipeline simple and reliable |
