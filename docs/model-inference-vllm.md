# Model Inference - vLLM on DGX Spark

Kubernetes manifests for GPU-accelerated LLM inference using vLLM on the NVIDIA DGX Spark (GB10 Blackwell GPU, 128GB unified memory).

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│ Backend (rag-agent-backend)                             │
│ http://nemotron-super-49b:8000/v1                       │
└────────────────┬────────────────────────────────────────┘
                 │
                 ▼
┌─────────────────────────────────────────────────────────┐
│ Service: nemotron-super-49b (ClusterIP:8000)            │
└────────────────┬────────────────────────────────────────┘
                 │
                 ▼
┌─────────────────────────────────────────────────────────┐
│ Deployment: nemotron-super-49b                          │
│ - Image: nvcr.io/nvidia/vllm:26.02-py3                 │
│ - Model: nvidia/Llama-3_3-Nemotron-Super-49B-v1_5-NVFP4│
│ - Quantization: NVFP4                                   │
│ - GPU: 1x NVIDIA GB10 (spark-7eb5)                     │
│ - Context: 16,384 tokens                                │
│ - PVC: model-cache-pvc (100Gi, Longhorn)                │
└─────────────────────────────────────────────────────────┘
```

## Current Configuration

- **Model**: [`nvidia/Llama-3_3-Nemotron-Super-49B-v1_5-NVFP4`](https://huggingface.co/nvidia/Llama-3_3-Nemotron-Super-49B-v1_5-NVFP4) (49B params, NVFP4 quantized)
- **Architecture**: Dense transformer, NAS-pruned from Llama-3.3-70B
- **Quantization**: NVFP4 (~25GB weights — half the memory bandwidth of FP8)
- **Context Length**: 16,384 tokens (model supports up to 128K)
- **GPU**: 1x NVIDIA GB10 Blackwell (128GB unified memory)
- **Serving**: OpenAI-compatible API (`/v1/chat/completions`, `/v1/models`)
- **Features**: Native tool calling (`llama3_json` parser), prefix caching, CUDA graphs enabled

## NVFP4 Quantization

The deployment uses NVFP4 quantization to maximize generation throughput on the GB10's limited memory bandwidth:

| Metric | BF16 (unquantized) | FP8 (pre-quantized) | NVFP4 (current) |
|--------|--------------------|--------------------|-----------------|
| Model weight size | ~98 GB | ~50 GB | **~25 GB** |
| Fits in 128GB unified memory | Tight | Comfortable | **Very comfortable** |
| Quality vs BF16 | Baseline | ~98% | **~95-96%** |
| KV cache headroom | ~20 GB | ~34 GB | **~41 GB** (at 0.55 util) |
| Generation throughput | N/A | ~4.5 tok/s | **~8-12 tok/s** |

### Why NVFP4 on DGX Spark

1. **Memory bandwidth is the bottleneck**: The GB10 is a mobile-class Blackwell chip. NVFP4 halves the bytes read per token, roughly doubling generation throughput
2. **CUDA graphs enabled**: With `--enforce-eager` removed, vLLM uses CUDA graphs for kernel launch batching (20-40% additional speedup)
3. **Reduced context window**: `--max-model-len=16384` instead of 32768 — the RAG pipeline uses ~4K tokens total, so 16K provides ample headroom with less memory overhead
4. **Acceptable quality tradeoff**: ~2-3% quality degradation on benchmarks vs FP8, negligible for RAG-grounded Q&A where answers come from retrieved documents

### FP8 Alternative

For higher quality at the cost of slower generation (~4.5 tok/s), switch back to FP8:

```yaml
args:
- "nvidia/Llama-3_3-Nemotron-Super-49B-v1_5-FP8"
- "--gpu-memory-utilization=0.70"
- "--max-model-len=32768"
- "--trust-remote-code"
```

### Performance Considerations

- **First startup** after switching models requires downloading ~25GB of NVFP4 weights. Set `HF_HUB_OFFLINE=0` for the first run, then `1` after cached.
- **CUDA graphs** are enabled (no `--enforce-eager`) for optimized kernel replay during generation.
- **`--enable-prefix-caching`** reduces redundant computation for requests that share common prompt prefixes (e.g., system prompts in RAG).

## Files

### Base (`base/`)

| File | Purpose |
|------|---------|
| `nemotron-super-49b-deployment.yaml` | vLLM inference deployment (pre-quantized FP8) |
| `nemotron-super-49b-service.yaml` | ClusterIP service exposing port 8000 |
| `model-cache-pvc.yaml` | 100Gi PersistentVolumeClaim for HuggingFace model cache |
| `qwen3-embedding-deployment.yaml` | Embedding service (CPU-based, separate from inference) |
| `qwen3-embedding-service.yaml` | ClusterIP service for embedding endpoint |
| `hf-external-secret.yaml` | HuggingFace token from Azure Key Vault |
| `kustomization.yaml` | Kustomize configuration |

### Overlays

- `overlays/dev/` - Development environment configuration

## Deployment

### Via Azure DevOps

Automatically deployed when changes are pushed to `kustomize/models/**`:

```yaml
# Trigger: azure-pipelines-models.yaml
```

### Manual Deployment

```bash
# Apply to cluster
kubectl apply -k kustomize/models/overlays/dev

# Check status
kubectl get pods -n rag-agent -l app=nemotron-super-49b
kubectl logs -n rag-agent -l app=nemotron-super-49b -f

# Test API endpoint
kubectl exec -it -n rag-agent deployment/rag-agent-backend -- \
  curl http://nemotron-super-49b:8000/v1/models
```

## Probe Configuration

The deployment uses a three-tier probe strategy to handle the slow model loading:

| Probe | Config | Purpose |
|-------|--------|---------|
| **Startup** | delay=120s, period=30s, threshold=120 (max 62 min) | Gates liveness/readiness during model loading |
| **Liveness** | period=30s, timeout=30s, threshold=5 | Restarts pod if inference hangs |
| **Readiness** | period=10s, timeout=10s, threshold=3 | Removes from service during issues |

The startup probe runs first; liveness and readiness probes don't begin until it succeeds.

## Backend Integration

Backend connects using the served model name as hostname:

```python
# assets/backend/agent.py
base_url=f"http://{self.current_model}:8000/v1"
# Resolves to: http://nemotron-super-49b.rag-agent.svc.cluster.local:8000
```

No backend changes needed when switching quantization or model variants.

## Security

The model endpoint is **internal-only** with no external exposure:

- **ClusterIP service** — only reachable from within the cluster, no HTTPRoute or ingress
- **Mesh-exempt** — `ambient.istio.io/redirection: disabled` opts the pod out of the Istio mesh (required to avoid probe timeouts from ztunnel interception on the GPU node)
- **No authentication** — the model trusts all in-cluster callers; security is enforced upstream

The full request chain:

```
Internet → Ingress Gateway (JWT validated via Istio RequestAuthentication)
         → Waypoint (L7 routing, CORS, retries)
         → Backend (JWT validated again in auth.py)
         → Model (internal ClusterIP, no auth)
```

Unauthenticated users are blocked at the ingress gateway before reaching the backend or model.

## Monitoring

```bash
# Pod status
kubectl get pods -n rag-agent -l app=nemotron-super-49b

# Logs
kubectl logs -n rag-agent -l app=nemotron-super-49b --tail=100

# Service endpoints
kubectl get endpoints nemotron-super-49b -n rag-agent

# vLLM metrics (Prometheus-compatible)
kubectl exec -it -n rag-agent deployment/rag-agent-backend -- \
  curl http://nemotron-super-49b:8000/metrics
```

## Switching Models

Update the model in `nemotron-super-49b-deployment.yaml`:

```yaml
args:
- "organization/model-name"
- "--served-model-name=nemotron-super-49b"   # Keep service name stable
- "--dtype=auto"
- "--max-model-len=CONTEXT_LENGTH"
```

After switching models, delete and recreate the PVC to clear the old cache:

```bash
kubectl scale deployment nemotron-super-49b -n rag-agent --replicas=0
kubectl delete pvc model-cache-pvc -n rag-agent
kubectl apply -f kustomize/models/base/model-cache-pvc.yaml
kubectl scale deployment nemotron-super-49b -n rag-agent --replicas=1
```

## GPU Configuration

### Prerequisites

- NVIDIA GPU Operator installed
- Nodes labeled: `nvidia.com/gpu.present=true`
- GPU toleration configured

### Resource Requests

```yaml
resources:
  requests:
    nvidia.com/gpu: 1
    cpu: "4"
    memory: "16Gi"
  limits:
    nvidia.com/gpu: 1
    cpu: "16"
    memory: "64Gi"
```

## Azure Key Vault Integration

HuggingFace token is managed via External Secrets Operator:

```
Azure Key Vault (hugging-face-read-only-token)
  → ExternalSecret (hf-external-secret.yaml)
    → K8s Secret (hf-credentials)
      → Pod env var (HF_TOKEN)
```

## References

- [Llama-3.3-Nemotron-Super-49B-v1.5-NVFP4](https://huggingface.co/nvidia/Llama-3_3-Nemotron-Super-49B-v1_5-NVFP4) (current)
- [Llama-3.3-Nemotron-Super-49B-v1.5-FP8](https://huggingface.co/nvidia/Llama-3_3-Nemotron-Super-49B-v1_5-FP8) (FP8 alternative)
- [Llama-3.3-Nemotron-Super-49B-v1.5](https://huggingface.co/nvidia/Llama-3_3-Nemotron-Super-49B-v1_5) (base unquantized)
- [vLLM Quantization](https://docs.vllm.ai/en/latest/features/quantization/supported_hardware.html)
- [vLLM Documentation](https://docs.vllm.ai/)
- [NVIDIA GPU Operator](https://docs.nvidia.com/datacenter/cloud-native/gpu-operator/)
