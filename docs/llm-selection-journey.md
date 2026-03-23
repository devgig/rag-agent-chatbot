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
- Reduced `--gpu-memory-utilization` from 0.70 to 0.55 (less memory needed)
- Reduced `--max-model-len` from 32768 to 16384 (RAG context is ~4K tokens)

### What we learned

- **~8-12 tok/s was better but still slow** for interactive use. A 500-token answer still took 40-60 seconds.
- **Quality was acceptable.** ~2-3% degradation on benchmarks vs FP8, negligible for RAG-grounded Q&A where answers come from retrieved documents.
- **Dense models hit a ceiling on GB10.** With all 49B parameters active per token, the model reads ~25 GB of weights for every generated token. The only way to go faster is to read fewer bytes — either by quantizing further (quality loss) or by changing the architecture.

---

## Phase 3: Qwen3-30B-A3B MoE FP8 (current)

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

## Phase 4: Nemotron 3 Nano 30B-A3B NVFP4 (current)

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
| `--gpu-memory-utilization=0.55` | NVFP4 is only ~15 GB — lower utilization leaves more system headroom |

---

## Performance Summary Across Phases

| Phase | Model | Architecture | Active params | Weight size | Throughput (vLLM) | 500-token answer |
| ----- | ----- | ------------ | ------------- | ----------- | ----------------- | ---------------- |
| 1 | Nemotron-49B FP8 | Dense | 49B | ~50 GB | ~4.5 tok/s | ~110s |
| 2 | Nemotron-49B NVFP4 | Dense | 49B | ~25 GB | ~8-12 tok/s | ~40-60s |
| 3 | Qwen3-30B-A3B FP8 | MoE | 3B | ~30 GB | ~38 tok/s | ~13s |
| **4** | **Nemotron 3 Nano 30B NVFP4** | **MoE** | **3B** | **~15 GB** | **~56 tok/s** | **~9s** |

---

## Future Considerations

### TensorRT-LLM / NIM

NVIDIA's blog shows TRT-LLM delivering better throughput than vLLM. A NIM container for Nemotron models on DGX Spark exists on NGC. Since we're now on an NVIDIA model with NVIDIA quantization, the path to NIM/TRT-LLM is straightforward and would likely push throughput even higher.

### GPU Time-Slicing

At only ~15 GB for weights, there's ~100 GB of headroom in unified memory. GPU time-slicing could run a second specialized model (e.g., a coder variant) alongside the general model for different task types.

### Qwen3.5-35B-A3B-FP8 (when vLLM supports it)

If Qwen's FP8 performance improves and vLLM adds `qwen3_5_moe` support, this could be revisited as an alternative. But the NVFP4 + NVIDIA model advantage is significant enough that Qwen would need to close a large gap.

### Embedding Model

The embedding model (all-MiniLM-L6-v2, 22M params, 384-dim) runs on CPU storage nodes and is deployed via its own pipeline (`azure-pipelines-embedding.yaml`), independent of the backend. An attempt to upgrade to Qwen3-Embedding-0.6B (600M params, 1024-dim) was reverted because the model OOM-killed on 16GB ARM64 storage nodes even with 8Gi memory limits. Future options:
- **GPU-based embedding**: With only ~15GB used by the LLM, there's headroom to run a larger embedding model on the GPU
- **Smaller high-quality models**: Models like BAAI/bge-base-en-v1.5 (110M, 768-dim) offer better quality than MiniLM without the memory overhead of 0.6B+ models
