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
- **The first LLM call was wasted.** The agent forced `tool_choice=search_documents` on iteration 0, making the model spend ~10s generating a tool call that was completely deterministic. This was later eliminated entirely by switching to a direct RAG pipeline (inline vector search + single LLM call).
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
- Reduced `--gpu-memory-utilization` from 0.70 to 0.65 (less memory needed)
- Reduced `--max-model-len` from 32768 to 16384 (RAG context is ~4K tokens)

### What we learned

- **~8-12 tok/s was better but still slow** for interactive use. A 500-token answer still took 40-60 seconds.
- **Quality was acceptable.** ~2-3% degradation on benchmarks vs FP8, negligible for RAG-grounded Q&A where answers come from retrieved documents.
- **Dense models hit a ceiling on GB10.** With all 49B parameters active per token, the model reads ~25 GB of weights for every generated token. The only way to go faster is to read fewer bytes — either by quantizing further (quality loss) or by changing the architecture.

---

## Phase 3: Qwen3-30B-A3B MoE FP8

**Model:** `Qwen/Qwen3-30B-A3B-Instruct-2507-FP8`
**Throughput:** ~38 tok/s (llama.cpp benchmark)
**Weight size:** ~30 GB

### Why we switched

The [NVIDIA DGX Spark AI agent scaling blog](https://developer.nvidia.com/blog/scaling-autonomous-ai-agents-and-workloads-with-nvidia-dgx-spark) showed MoE models delivering dramatically better throughput than dense models. Qwen3-30B-A3B has 30B total parameters but only 3B active per token:
- **~16x less data read per token** compared to a 49B dense model
- **~38 tok/s generation** on DGX Spark
- **FP8 quality preserved** — no need for aggressive quantization

We initially tried Qwen3.5-35B-A3B-FP8, but vLLM 26.02 (v0.15.1) doesn't support the `qwen3_5_moe` architecture yet.

### What we learned

- **MoE is the right architecture for bandwidth-limited GPUs.** The GB10 has plenty of memory (128 GB) but limited bandwidth. MoE exploits this by storing many parameters but reading few.
- **vLLM version compatibility matters.** Qwen3.5 models use a new architecture (`qwen3_5_moe`) not yet in vLLM 26.02. Always verify model architecture support before committing to a model.
- **Dedicated model namespace.** Isolating model serving from application workloads keeps GPU resources separate from the deployment lifecycle.
- **Qwen FP8 performance is suboptimal.** Qwen themselves acknowledge FP8 performance in Transformers needs further optimization. Community benchmarks showed Nemotron Nano NVFP4 significantly outperforming Qwen3-30B FP8 on the same hardware.

---

## Phase 4: Nemotron 3 Nano 30B-A3B NVFP4

**Model:** `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-NVFP4`
**Throughput:** ~56 tok/s (vLLM), ~70 tok/s (llama.cpp)
**Weight size:** ~15 GB

### Why we switched

Community benchmark data from llama.cpp discussions on DGX Spark hardware showed Nemotron Nano dramatically outperforming Qwen3-30B-A3B:

| Model | Quant | DGX Spark (vLLM) | DGX Spark (llama.cpp) |
| ----- | ----- | ---------------- | --------------------- |
| Nemotron 3 Nano 30B | NVFP4 | **~56 tok/s** | **~70 tok/s** |
| Qwen3-30B-A3B | FP8 | ~38 tok/s | ~38 tok/s |

Nemotron Nano wins by **47% (vLLM)** to **84% (llama.cpp)** on the same hardware.

### Why Nemotron Nano over Qwen3

1. **NVIDIA's own model + quantization format.** NVFP4 is native to Blackwell — hardware-accelerated with an optimization trajectory that only improves as NIM and TRT-LLM mature.
2. **Half the weight size.** ~15 GB (NVFP4) vs ~30 GB (Qwen FP8) — leaves ~100 GB free for KV cache, concurrent requests, or GPU time-slicing with a second model.
3. **Qwen FP8 acknowledged as suboptimal.** Qwen themselves note FP8 performance needs further optimization. Nemotron's NVFP4 has no such caveat.

### Architectural changes

| Change | Rationale |
| ------ | --------- |
| New `llm` namespace | Dedicated namespace for shared model serving |
| ExternalName service in `rag-agent` ns | Backend uses short name `http://nemotron-nano:8000/v1` — ExternalName resolves to `nemotron-nano.llm.svc.cluster.local` |
| Tool call parser: `hermes` | Nemotron Nano supports the Hermes tool calling format |
| `--gpu-memory-utilization=0.65` | NVFP4 is only ~15 GB — moderate utilization balances KV cache size with system headroom |

---

## Phase 5: Qwen3-Coder-Next 80B-A3B FP8 (current)

**Model:** `Qwen/Qwen3-Coder-Next`
**Architecture:** MoE + hybrid attention (linear + standard). 80B total / 3B active per token.
**Weight size:** ~80 GB (FP8, runtime-quantized from BF16)
**Context:** 131,072 tokens (model supports 256K; we configure 128K to leave KV-cache headroom on a single GPU)

### Why we switched

The rag-agent-chatbot workload shifted from "general chat over documents" to also serving as the coding-agent fallback for the ai-agents platform (LiteLLM routes Claude Code CLI traffic to this model when the Max subscription budget is exhausted). That workload rewards agentic coding quality — SWE-Bench Verified, tool-use reliability, long-context reasoning — and Nemotron Nano, while fast and general, wasn't trained for it.

| Benchmark | Qwen3-Coder-Next | Nemotron 3 Nano 30B |
| --------- | ---------------- | ------------------- |
| SWE-Bench Verified (agent scaffold) | **>70%** | Not reported |
| SWE-Bench Pro | 44.3% (matches models 10-20× larger active) | — |
| LiveCodeBench v6 | (not reported but positioned higher) | 68.3% |
| HumanEval | — | 78.05% |
| Agent/tool training | Purpose-built | Strong but general |
| Active params / total | 3B / 80B | 3B / 30B |
| Context | 256K native | 128K native |

### Why Qwen3-Coder-Next over Nemotron Nano

1. **SWE-Bench Verified >70%** means the model actually completes real GitHub-issue-style coding tasks — the metric that matters for a Claude Code fallback. HumanEval / LiveCodeBench test single-shot snippet generation; they don't predict agent success.
2. **Sonnet 4.5-level on coding** with only 3B active params. Same throughput profile as Nemotron Nano (both 3B active) but substantially better code quality.
3. **Shared deployment.** Both rag-agent-chatbot and the ai-agents LiteLLM route to the same `qwen3-coder-next.llm.svc.cluster.local:8000` endpoint — no duplicate deployments.
4. **256K native context** (configured at 128K) vs. Nemotron's 128K native. Room to grow as Blackwell-native quantization (FP4/MXFP4) matures.

### Architectural changes

| Change | Rationale |
| ------ | --------- |
| Rename services: `nemotron-nano` → `qwen3-coder-next` | Matches the served model; avoids confusion for operators reading cluster state |
| Image bump: `nvcr.io/nvidia/vllm:26.02-py3` → `26.03.post1-py3` | Required for vLLM ≥ 0.15 with the `qwen3_coder` tool parser. The `.post1` variant is confirmed loading on DGX Spark per NVIDIA developer forums |
| Tool call parser: `hermes` → `qwen3_coder` | Qwen3-Coder-Next uses its own structured tool-call format |
| Quantization: NVFP4 (native) → FP8 (runtime) | `Qwen/Qwen3-Coder-Next` ships BF16 (~160 GB); FP8 fits the 128 GB unified-memory budget with room for KV cache |
| `--max-model-len=16384` → `131072` | Long-context is valuable for coding agents reading large files; 131K leaves ~20 GB for KV cache |
| `--gpu-memory-utilization=0.65` → `0.85` | Larger weights (~80 GB) + longer context need more of the 128 GB budget |
| Startup probe: max 62 min → 93 min | FP8 weight download is ~80 GB vs Nemotron's ~15 GB; generous threshold for first boot |

### Trade-offs accepted

- **Throughput drops**. Both models have 3B active params, but Qwen3-Coder-Next's 80B total footprint means more memory bandwidth per forward step — realistic expectation is ~40-50 tok/s on vLLM vs. Nemotron's ~56 tok/s, a ~20% regression in raw speed.
- **Longer first-boot**. 80 GB FP8 weight download vs. 15 GB NVFP4. Migration window on a cold PVC is ~15-30 min before the pod Ready's.
- **No native Blackwell quantization yet**. FP8 is fine for Blackwell but NVFP4 would be a step faster. Revisit when Qwen publishes an NVFP4 variant or the community builds one.

---

## Performance Summary Across Phases

| Phase | Model | Architecture | Active params | Weight size | Throughput (vLLM) | Notes |
| ----- | ----- | ------------ | ------------- | ----------- | ----------------- | ----- |
| 1 | Nemotron-49B FP8 | Dense | 49B | ~50 GB | ~4.5 tok/s | Too slow for chat |
| 2 | Nemotron-49B NVFP4 | Dense | 49B | ~25 GB | ~8-12 tok/s | Still slow |
| 3 | Qwen3-30B-A3B FP8 | MoE | 3B | ~30 GB | ~38 tok/s | Fast but FP8 suboptimal |
| 4 | Nemotron 3 Nano 30B NVFP4 | MoE | 3B | ~15 GB | ~56 tok/s | Fast, general-purpose |
| **5** | **Qwen3-Coder-Next 80B-A3B FP8** | **MoE + hybrid** | **3B** | **~80 GB** | **~40-50 tok/s est.** | **Purpose-built for coding agents** |

---

## Future Considerations

### Native Blackwell quantization (NVFP4 / MXFP4) for Qwen3-Coder-Next

FP8 runtime quantization fits the single-GPU budget but isn't Blackwell-native. A community or Qwen-published NVFP4 / MXFP4 variant would drop the footprint to ~40 GB and free budget for 256K context. Track the Qwen/Qwen3-Coder-Next HuggingFace repo for new quantization variants.

### TensorRT-LLM

NVIDIA's blog shows TRT-LLM delivering better throughput than vLLM for supported models. Qwen3-Coder-Next with hybrid attention is new architecture; TRT-LLM support likely lags vLLM for a release or two. Revisit when NVIDIA ships an optimized engine build.

### GPU Time-Slicing

Runs ~80 GB of weights now (vs ~15 GB under Nemotron), leaving ~30 GB of headroom for KV cache and a tight operating margin. Adding a second model via time-slicing would require dropping `--max-model-len` or compressing further (NVFP4). Re-evaluate once Blackwell-native quantization is available.

### Embedding Model

The embedding model (all-MiniLM-L6-v2, 22M params, 384-dim) runs on CPU storage nodes and is deployed via its own pipeline (`azure-pipelines-embedding.yaml`), independent of the backend. An attempt to upgrade to Qwen3-Embedding-0.6B (600M params, 1024-dim) was reverted because the model OOM-killed on 16GB ARM64 storage nodes even with 8Gi memory limits. Future options:
- **GPU-based embedding**: Tight now that Qwen3-Coder-Next occupies ~80 GB; not a near-term move.
- **Smaller high-quality models**: Models like BAAI/bge-base-en-v1.5 (110M, 768-dim) offer better quality than MiniLM without the memory overhead of 0.6B+ models.

### Embedding Model

The embedding model (all-MiniLM-L6-v2, 22M params, 384-dim) runs on CPU storage nodes and is deployed via its own pipeline (`azure-pipelines-embedding.yaml`), independent of the backend. An attempt to upgrade to Qwen3-Embedding-0.6B (600M params, 1024-dim) was reverted because the model OOM-killed on 16GB ARM64 storage nodes even with 8Gi memory limits. Future options:
- **GPU-based embedding**: With only ~15GB used by the LLM, there's headroom to run a larger embedding model on the GPU
- **Smaller high-quality models**: Models like BAAI/bge-base-en-v1.5 (110M, 768-dim) offer better quality than MiniLM without the memory overhead of 0.6B+ models
