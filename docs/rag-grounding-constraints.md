# RAG Grounding Constraints: Forcing Document-Only Responses

This document describes every mechanism implemented to ensure the LLM **only** answers questions using content retrieved from uploaded document embeddings, and never falls back to its general training knowledge.

---

## Overview

The system uses a defense-in-depth strategy across five layers: mandatory retrieval, prompt constraints, relevance filtering, architectural isolation, and deterministic sampling. Each layer independently prevents the model from answering outside the scope of uploaded documents.

---

## 1. Mandatory Retrieval (Code-Level Guarantee)

**File:** `assets/backend/agent.py` — `generate()`

The `generate()` node **always** performs a vector search before calling the LLM. There is no code path where the LLM is invoked without first querying the vector store — retrieval is hardcoded, not a model decision:

```python
# Document search runs unconditionally before any LLM call
config_obj = self.config_manager.read_config()
sources = config_obj.selected_sources or []

if sources:
    retrieved_docs = await asyncio.to_thread(
        self.vector_store.get_documents, user_query, 5, sources
    )
else:
    retrieved_docs = await asyncio.to_thread(
        self.vector_store.get_documents, user_query
    )
```

The retrieved context is baked directly into the system prompt before the LLM sees anything. The model has no mechanism to skip retrieval — it is never consulted about whether to search.

If source-filtered retrieval returns nothing, the system falls back to searching all documents before concluding no results exist.

---

## 2. System Prompt Constraints

**File:** `assets/backend/prompts.py`

The system prompt establishes the model's identity as a **document-grounded assistant** and includes the retrieved context directly:

```
You are a document-grounded assistant. Answer ONLY using the provided document context.
If no relevant context is provided, say "I couldn't find information about that in your uploaded documents."
NEVER answer from your own knowledge. Be concise and to the point.

Context:
{{ context }}
```

The context is rendered into the prompt by `generate()` before the LLM call. The model sees only the retrieved documents — there are no tool-calling instructions or escape hatches.

---

## 3. Relevance Score Threshold

**File:** `assets/backend/vector_store.py`

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

## 4. Architectural Isolation

Several architectural decisions prevent knowledge leakage:

| Component | Isolation Mechanism |
|-----------|---------------------|
| **Embedding model** | all-MiniLM-L6-v2 running locally — no external API calls that could inject knowledge |
| **Vector database** | Self-hosted Milvus — no shared/public collections |
| **LLM inference** | Self-hosted via vLLM — no external knowledge augmentation |
| **Single-pass pipeline** | `START → generate → END` — no iterative loops that could refine queries to escape grounding |
| **No web access** | No search tools, no URL fetching — the model cannot access external information |

---

## 5. Temperature and Sampling

**File:** `assets/backend/agent.py` — `generate()`

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
| **Mandatory retrieval** | Vector search hardcoded in `generate()` before every LLM call | Model skipping retrieval entirely |
| **Prompt** | System prompt with context and explicit rules | Model choosing to use general knowledge |
| **Relevance threshold** | Score filtering at 0.4 cutoff | Low-quality/tangential chunks reaching the model |
| **Architecture** | Local models, self-hosted DB, single-pass pipeline, no web access | External knowledge sources or iterative escape |
| **Sampling** | temperature=0, top_p=1 | Creative hallucination beyond context |
