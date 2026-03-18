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
│ - Image: nvcr.io/nvidia/vllm:25.11-py3                 │
│ - Model: nvidia/Llama-3_3-Nemotron-Super-49B-v1_5-FP8  │
│ - Quantization: Pre-quantized FP8                       │
│ - GPU: 1x NVIDIA GB10 (spark-7eb5)                     │
│ - Context: 32,768 tokens                                │
│ - PVC: model-cache-pvc (100Gi, Longhorn)                │
└─────────────────────────────────────────────────────────┘
```

## Current Configuration

- **Model**: [`nvidia/Llama-3_3-Nemotron-Super-49B-v1_5-FP8`](https://huggingface.co/nvidia/Llama-3_3-Nemotron-Super-49B-v1_5-FP8) (49B params, pre-quantized FP8)
- **Architecture**: Dense transformer, NAS-pruned from Llama-3.3-70B
- **Quantization**: Pre-quantized FP8 (no dynamic quantization needed)
- **Context Length**: 32,768 tokens (model supports up to 128K)
- **GPU**: 1x NVIDIA GB10 Blackwell (128GB unified memory)
- **Serving**: OpenAI-compatible API (`/v1/chat/completions`, `/v1/models`)
- **Features**: Native tool calling (`llama_nemotron_json` parser), prefix caching

## FP8 Quantization

The Nemotron-Super-49B-v1.5-FP8 checkpoint ships with pre-quantized FP8 weights, optimized for Blackwell GPUs:

| Metric | BF16 (unquantized) | FP8 (pre-quantized) |
|--------|--------------------|--------------------|
| Model weight size | ~98 GB | **~50 GB** |
| Fits in 128GB unified memory | Tight | **Comfortable** |
| Quality vs BF16 | Baseline | Near-lossless |
| KV cache headroom | ~20 GB | **~34 GB** (at 0.70 utilization) |

### Why FP8 on Blackwell

1. **Native hardware support**: Blackwell GB10 has FP8 tensor cores — no software emulation overhead
2. **Near-lossless quality**: FP8 preserves model quality better than INT4/INT8 for instruction-following and reasoning tasks
3. **Optimal for 49B on 128GB**: At ~50GB for weights with 0.70 gpu-memory-utilization, leaves ~34GB for KV cache — enough for concurrent requests at 32K context while preserving system headroom
4. **Pre-quantized checkpoint**: No dynamic quantization overhead at load time — faster startup than runtime FP8 conversion

### Performance Considerations

- **First startup** after a fresh PVC requires downloading ~50GB of model weights. Subsequent restarts use the cached model.
- **`--enforce-eager`** is enabled to disable CUDA graph compilation, ensuring compatibility with dynamic input shapes.
- **`--enable-prefix-caching`** reduces redundant computation for requests that share common prompt prefixes (e.g., system prompts in RAG).
- **`HF_HUB_OFFLINE`** can be set to `1` after the model is cached on the PVC to eliminate network dependency on startup.

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
| **Startup** | delay=120s, period=30s, threshold=40 (max 22 min) | Gates liveness/readiness during model loading |
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

- [Llama-3.3-Nemotron-Super-49B-v1.5-FP8](https://huggingface.co/nvidia/Llama-3_3-Nemotron-Super-49B-v1_5-FP8)
- [Llama-3.3-Nemotron-Super-49B-v1.5](https://huggingface.co/nvidia/Llama-3_3-Nemotron-Super-49B-v1_5)
- [Llama-3.3-Nemotron-Super-49B-v1.5-NVFP4](https://huggingface.co/nvidia/Llama-3_3-Nemotron-Super-49B-v1_5-NVFP4)
- [vLLM FP8 Quantization](https://docs.vllm.ai/en/latest/features/quantization/supported_hardware.html)
- [vLLM Documentation](https://docs.vllm.ai/)
- [NVIDIA GPU Operator](https://docs.nvidia.com/datacenter/cloud-native/gpu-operator/)
