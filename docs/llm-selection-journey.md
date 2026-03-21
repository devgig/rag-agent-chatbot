# LLM Selection Journey on DGX Spark

This document traces the evolution of the locally-hosted LLM serving strategy for DGX Spark, explaining why each decision was made, what was learned, and where the architecture is headed.

## Hardware Constraints

Every decision below is shaped by the DGX Spark's hardware profile:

| Spec | Value |
| ---- | ----- |
| GPU | NVIDIA GB10 (Blackwell, single GPU) |
| Memory | 128 GB unified (CPU + GPU share one pool) |
| CPU | ARM Cortex-X925, 20 cores @ 3.9 GHz (aarch64) |
| CUDA | 13.0 |
| Tensor parallelism | Not available (single GPU) |
| Primary bottleneck | **Memory bandwidth** (not compute) |

The unified memory architecture means every byte used by the model is a byte unavailable to the OS, K3s, and other workloads. And unlike data center GPUs (H100, B200), the GB10 has limited memory bandwidth, making bytes-read-per-token the dominant factor in generation speed.

---

## Phase 1: Nemotron-Super-49B FP8

**Model:** `nvidia/Llama-3_3-Nemotron-Super-49B-v1_5-FP8`
**Throughput:** ~4.5 tok/s
**Weight size:** ~50 GB

### Why it was chosen

- NVIDIA's NAS-pruned derivative of Llama-3.3-70B — near-70B quality at 49B parameters
- Pre-quantized FP8 checkpoint with native Blackwell tensor core support
- Strong instruction-following and tool calling via `llama3_json` parser
- Fit comfortably in 128 GB unified memory at `--gpu-memory-utilization=0.70`

### What we learned

- **4.5 tok/s was painfully slow.** A 500-token RAG answer took ~110 seconds. Users would wait over a minute for every question.
- **`--enforce-eager` was wasting performance.** It disabled CUDA graphs, which batch and replay GPU kernel launches. Removing it could improve throughput 20-40%.
- **The first LLM call was wasted.** The agent forced `tool_choice=search_documents` on iteration 0, making the model spend ~10s generating a tool call that was completely deterministic. This was fixed by constructing the tool call directly in Python (fast-path bypass).
- **Milvus had no vector index.** The collection was using brute-force L2 search. Adding HNSW with COSINE metric fixed relevance scoring and improved search quality.

### Optimizations applied (kept through all phases)

| Optimization | Impact |
| ------------ | ------ |
| Fast-path LLM bypass on iteration 0 | Eliminated ~10s per query |
| HNSW/COSINE vector index | Proper relevance scores, faster search |
| Async Milvus operations (`asyncio.to_thread`) | Unblocked the event loop |
| Reduced retrieved documents (k=8 to k=5) | Less prompt tokens, faster generation |
| Single VectorStore instance | Halved Milvus connections and memory |
| `append_messages()` for history persistence | Avoided redundant DB fetches |
| Indexing task TTL cleanup | Prevented unbounded memory growth |

---

## Phase 2: Nemotron-Super-49B NVFP4

**Model:** `nvidia/Llama-3_3-Nemotron-Super-49B-v1_5-NVFP4`
**Throughput:** ~8-12 tok/s
**Weight size:** ~25 GB

### Why we switched

NVFP4 halves the weight size (50 GB to 25 GB), which halves the bytes read per token. On the bandwidth-limited GB10, this roughly doubled generation throughput.

### Changes made

- Switched from FP8 to NVFP4 checkpoint
- Removed `--enforce-eager` to enable CUDA graphs (additional 20-40% speedup)
- Reduced `--gpu-memory-utilization` from 0.70 to 0.55 (less memory needed)
- Reduced `--max-model-len` from 32768 to 16384 (RAG context is ~4K tokens)

### What we learned

- **~8-12 tok/s was better but still slow** for interactive use. A 500-token answer still took 40-60 seconds.
- **Quality was acceptable.** ~2-3% degradation on benchmarks vs FP8, negligible for RAG-grounded Q&A where answers come from retrieved documents.
- **Dense models hit a ceiling on GB10.** With all 49B parameters active per token, the model reads ~25 GB of weights for every generated token. The only way to go faster is to read fewer bytes — either by quantizing further (quality loss) or by changing the architecture.

---

## Phase 3: Qwen3-30B-A3B MoE FP8 (current)

**Model:** `Qwen/Qwen3-30B-A3B-Instruct-2507-FP8`
**Throughput:** ~35 tok/s
**Weight size:** ~30 GB

### Why we switched

The [NVIDIA DGX Spark AI agent scaling blog](https://developer.nvidia.com/blog/scaling-autonomous-ai-agents-and-workloads-with-nvidia-dgx-spark) benchmarked Qwen3.5-35B-A3B at 35.75 tok/s on DGX Spark — 8x faster than Nemotron-49B FP8. The key insight: **Mixture-of-Experts models only activate a subset of parameters per token.**

Qwen3-30B-A3B has 30B total parameters but only 3B are active per token. This means:
- **~16x less data read per token** compared to a 49B dense model
- **~35 tok/s generation** — a 500-token answer in ~14 seconds instead of 110
- **FP8 quality preserved** — no need for aggressive quantization

We initially tried Qwen3.5-35B-A3B-FP8, but vLLM 26.02 (v0.15.1) doesn't support the `qwen3_5_moe` architecture yet. The Qwen3 predecessor (`qwen3_moe`) is supported.

### Architectural changes

| Change | Rationale |
| ------ | --------- |
| New `llm` namespace | Shared model serving across projects (rag-agent-chatbot, ai-agents) |
| ExternalName service in `rag-agent` ns | Backend uses short name `http://qwen35:8000/v1` — ExternalName resolves to `qwen35.llm.svc.cluster.local` |
| Cross-namespace DNS for ai-agents | Direct reference: `http://qwen35.llm.svc.cluster.local:8000/v1` |
| Tool call parser: `hermes` | Qwen3 uses the Hermes tool calling format (not llama3_json) |
| Removed NIM deployment from ai-agents | The 70B NIM image was broken (wrong arch, model too large). Replaced with shared vLLM instance. |

### What we learned

- **MoE is the right architecture for bandwidth-limited GPUs.** The GB10 has plenty of memory (128 GB) but limited bandwidth. MoE exploits this by storing many parameters but reading few.
- **vLLM version compatibility matters.** Qwen3.5 models use a new architecture (`qwen3_5_moe`) not yet in vLLM 26.02. Always verify model architecture support before committing to a model.
- **Shared serving reduces waste.** Running one model instance for multiple projects avoids GPU memory duplication on a single-GPU system.

---

## Performance Summary Across Phases

| Phase | Model | Architecture | Active params | Throughput | 500-token answer |
| ----- | ----- | ------------ | ------------- | ---------- | ---------------- |
| 1 | Nemotron-49B FP8 | Dense | 49B | ~4.5 tok/s | ~110s |
| 2 | Nemotron-49B NVFP4 | Dense | 49B | ~8-12 tok/s | ~40-60s |
| **3** | **Qwen3-30B-A3B FP8** | **MoE** | **3B** | **~35 tok/s** | **~14s** |

---

## Future Considerations

### Qwen3-Coder-30B-A3B-Instruct-FP8

[`Qwen3-Coder-30B-A3B-Instruct-FP8`](https://huggingface.co/Qwen/Qwen3-Coder-30B-A3B-Instruct-FP8) — same MoE architecture (30B/3B active, ~30 GB FP8, ~35 tok/s) but tuned for code generation, review, and debugging.

**When to consider:** If the ai-agents project shifts more code tasks from Claude (Tier 2/3) to the local GPU (Tier 4) to reduce API costs, the coder variant would outperform the general instruct model on those tasks.

**Tradeoff:** The `llm` namespace instance is shared with rag-agent-chatbot (general Q&A). The coder model may be slightly worse for RAG-grounded document Q&A. Mitigation: GPU time-slicing to run both models concurrently.

### Qwen3.5-35B-A3B-FP8 (when vLLM supports it)

The Qwen3.5 generation improves on Qwen3 with better instruction following and reasoning. Once vLLM adds support for the `qwen3_5_moe` architecture (likely in a future NGC release), this would be a drop-in upgrade with the same throughput profile.

### TensorRT-LLM

NVIDIA's blog shows TensorRT-LLM delivering significantly better throughput than vLLM on DGX Spark (e.g., 120B model at 18 tok/s with TRT-LLM vs much slower with vLLM). A NIM container for Nemotron-Super-49B on DGX Spark exists on NGC but hasn't been validated for our setup. TRT-LLM requires pre-compiled engine files for each model/GPU combination.

### GPU Time-Slicing

Running multiple models on the GB10 via NVIDIA's time-slicing scheduler. This would allow serving both the general instruct model (for RAG Q&A) and the coder model (for ai-agents) simultaneously, at the cost of splitting GPU memory and time between them.
