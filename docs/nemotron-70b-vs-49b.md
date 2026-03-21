# Why Nemotron-49B Instead of Nemotron-70B on DGX Spark

The DGX Spark's unified memory architecture makes the full 70B model infeasible. The NAS-pruned 49B variant delivers near-70B quality while fitting comfortably within the hardware budget.

## DGX Spark Hardware Constraints

| Spec | Value |
|------|-------|
| GPU | NVIDIA GB10 (Blackwell) |
| Architecture | Unified memory (CPU + GPU share one pool) |
| Total system memory | 128 GB (119 GiB usable) |
| CPU | ARM Cortex-X925, 20 cores @ 3.9 GHz (aarch64) |
| CUDA | 13.0 |
| Tensor parallelism | Not available (single GPU) |
| OS | Ubuntu 24.04 LTS |

The critical constraint is **unified memory**. Unlike discrete GPUs with dedicated VRAM, the GB10 shares its entire 128 GB memory pool between the CPU (OS, K3s, daemonsets) and GPU (model weights, KV cache, activations). Every byte used by the model is a byte unavailable to the system.

## Why Nemotron-70B Does Not Fit

### BF16 (unquantized): Impossible

Llama-3.3-70B-Instruct at BF16 precision requires **~140 GB** for weights alone — more than the total physical memory on the system. It cannot load at all.

### FP8 (quantized): Still too tight

Even with FP8 quantization, Nemotron-70B requires **~70 GB** for weights. That leaves the memory budget looking like this:

```
Total unified memory:              128 GB
├── OS + K3s + daemonsets:          ~8 GB
├── Model weights (FP8):           ~70 GB
├── Remaining for KV cache:        ~50 GB
│
└── But vLLM also needs:
    ├── CUDA context + activations: ~4-6 GB
    ├── Prefix cache metadata:      ~1-2 GB
    └── /dev/shm (shared memory):   variable
```

At `--gpu-memory-utilization=0.70` (our current setting), vLLM would only have access to **~84 GiB** — not enough for 70 GB of weights plus a usable KV cache. Raising utilization to 0.90+ to make it fit would starve the OS and K3s of memory, causing OOM kills and node instability.

Even if the model loaded, the remaining KV cache would be so small that the system could only handle a few concurrent requests at short context lengths — defeating the purpose of running a 128K-capable model.

### No multi-GPU escape hatch

On a multi-GPU server, tensor parallelism (`--tensor-parallel-size=2+`) splits the model across GPUs so each one holds only a fraction of the weights. The DGX Spark has a **single GB10 GPU**. There is no way to distribute the model across devices.

## Why Nemotron-49B Is the Right Fit

[Llama-3.3-Nemotron-Super-49B-v1.5](https://huggingface.co/nvidia/Llama-3_3-Nemotron-Super-49B-v1_5) is NVIDIA's NAS-pruned (Neural Architecture Search) derivative of Llama-3.3-70B. It removes redundant layers and attention heads to reduce the parameter count from 70B to 49B while preserving the quality of the original model.

### Memory budget with 49B FP8

The [pre-quantized FP8 checkpoint](https://huggingface.co/nvidia/Llama-3_3-Nemotron-Super-49B-v1_5-FP8) requires **~50 GB** for weights. On the DGX Spark this breaks down as:

```
Total unified memory:                   128 GB
├── OS + K3s + daemonsets:               ~8 GB
├── Available to GPU (119 GiB usable):  ~120 GB
│
│   At --gpu-memory-utilization=0.70:
│   ├── vLLM allocation:               ~84 GiB
│   │   ├── Model weights (FP8):       ~50 GB
│   │   └── KV cache:                  ~34 GB  ← enough for concurrent 32K-token requests
│   └── Reserved (0.30):               ~36 GiB ← headroom for OS, K3s, daemonsets
│
└── Observed (nvidia-smi):               ~84 GiB GPU memory used ✓
```

This is exactly what we observe in production — `nvidia-smi` reports **85,933 MiB (~84 GiB)** of GPU memory in use, matching the 0.70 utilization target and leaving ~36 GiB free for system processes.

### Quality preservation

NVIDIA's NAS pruning removes structural redundancy (entire attention heads and MLP layers), not individual weights. The result is a model that:

- Scores within 1-2% of Llama-3.3-70B-Instruct on standard benchmarks (MMLU, HumanEval, MT-Bench)
- Retains full instruction-following and reasoning capability
- Supports native tool calling with the same Llama-3.3 chat template
- Maintains the full 128K context window (we cap at 32K for KV cache efficiency)

### Operational advantages over a hypothetical squeezed 70B

| | Nemotron-70B (FP8, hypothetical) | Nemotron-49B (FP8, actual) |
|--|--|--|
| Weight size | ~70 GB | ~50 GB |
| KV cache headroom at 0.70 util | ~14 GB (barely usable) | ~34 GB |
| Concurrent requests at 32K ctx | 1-2 | 4-6 |
| System memory headroom | ~8 GB (risk of OOM) | ~36 GB (stable) |
| Cold start download | ~70 GB | ~50 GB |
| Quality vs 70B BF16 | ~97% (FP8 loss on larger model) | ~98% (NAS-pruned, FP8 on smaller model) |

### NVFP4 (current deployment)

The project currently uses [`Nemotron-Super-49B-v1.5-NVFP4`](https://huggingface.co/nvidia/Llama-3_3-Nemotron-Super-49B-v1_5-NVFP4) at ~25 GB weights. NVFP4 was chosen over FP8 to maximize generation throughput on the GB10's limited memory bandwidth:

- **~2x faster generation**: ~8-12 tok/s vs ~4.5 tok/s with FP8, because halving weight size halves memory-bandwidth pressure
- **CUDA graphs enabled**: With less memory pressure, `--enforce-eager` was removed, enabling CUDA graph kernel replay for an additional 20-40% speedup
- **Quality tradeoff**: ~2-3% degradation on benchmarks vs FP8 — acceptable for RAG-grounded Q&A where answers come from retrieved documents
- **More headroom**: At `--gpu-memory-utilization=0.55`, leaves ~54 GiB free for OS/K3s vs ~36 GiB with FP8

The FP8 variant remains available as an alternative when higher quality is needed (see deployment YAML for switching instructions). NVFP4 on 70B would still require ~35 GB with significant quality degradation — another reason the 49B model is the right choice.

## Summary

The DGX Spark's 128 GB unified memory is shared between CPU and GPU. The Nemotron-70B model needs ~70 GB just for weights at FP8, leaving almost no room for KV cache or system processes. The NAS-pruned 49B variant uses ~25 GB at NVFP4 (or ~50 GB at FP8), provides near-identical quality, and leaves enough headroom for concurrent inference, prefix caching, and stable node operation.
