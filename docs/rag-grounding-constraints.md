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
prefs = await self.conversation_store.get_user_preferences(user_id)
sources = prefs.get("selected_sources") or []

retrieved_docs = await self.vector_store.get_documents(
    user_query,
    user_id,
    k=5,
    selected_sources=sources or None,
)
```

The retrieved context is baked directly into the system prompt before the LLM sees anything. The model has no mechanism to skip retrieval — it is never consulted about whether to search.

When a source is selected, Milvus filters to that source only. If no relevant chunks exist in the selected source, the LLM receives empty context and tells the user it has no information — there is no silent fallback to other documents.

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
| **LLM inference** | Self-hosted via KServe (vLLM runtime) — no external knowledge augmentation |
| **Single-pass pipeline** | `START → generate → END` — no iterative loops that could refine queries to escape grounding |
| **No web access** | No search tools, no URL fetching — the model cannot access external information |
| **Zero conversation history** | Each turn is sent to the LLM as `(system + current user message)` only — see §5 |

---

## 5. Stateless Turns (No Conversation History)

**File:** `assets/backend/agent.py` — `generate()` (line ~140)

The LLM call uses **only** the current turn's user message plus the system prompt
(which embeds the retrieved documents). Prior `HumanMessage` / `AIMessage` pairs
are saved to PostgreSQL for the UI to display, but never replayed to the model.

```python
messages = [
    {"role": "system", "content": system_prompt},  # includes retrieved <document> blocks
    {"role": "user", "content": user_query},
]
```

### Why no history

The obvious feature ask is "let the model see the last N turns so users can ask
follow-ups like 'expand on that'." We deliberately don't do it because of a
failure mode that's specific to user-selected RAG:

> **Scenario.** User selects Doc A, asks question about Doc A → correct answer.
> User then *deselects* Doc A, selects Doc B, asks a similar question. The model
> has Doc A's facts in conversation history. It bleeds those facts into the
> Doc B answer even though Doc A is no longer in the retrieved context for this
> turn. Result: confidently-stated factual claims that contradict the currently-
> selected sources.

In a non-RAG chatbot, conversation history is purely additive context. In a
RAG chatbot where the *active source set* can change between turns, history
becomes a vector for cross-source contamination. The user has no idea the
model is still influenced by a doc they've already moved on from.

### Trade-offs accepted

- **No "expand on that" / "elaborate" UX.** Follow-ups need to restate context.
  The system prompt explicitly tells the model to ask the user to rephrase when
  a question is ambiguous in isolation, instead of guessing from prior turns.
- **Slightly more typing for users.** Acceptable cost for the correctness
  guarantee.
- **The UI surfaces this** in the welcome message so users aren't surprised.

### What if multi-turn becomes a hard requirement later

Two options that preserve grounding:

1. **Per-source history.** Maintain a separate history thread per active source
   set; switch threads when the user changes selected_sources. Adds state
   complexity but eliminates the cross-source bleed.
2. **Summarized history.** Maintain a rolling summary of prior turns scoped to
   "what the user has been asking about", not "what the model said." The
   summary feeds back into the system prompt as topical hints, not as
   conclusions to defer to. Lower precision but bounded contamination.

Neither is in scope today.

---

## 6. Temperature and Sampling

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
| **Source isolation** | Milvus `expr` filter + Python-side enforcement | Documents from non-selected sources leaking into context |
| **Architecture** | Local models, self-hosted DB, single-pass pipeline, no web access | External knowledge sources or iterative escape |
| **Sampling** | temperature=0, top_p=1 | Creative hallucination beyond context |
