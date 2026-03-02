# Model Inference - vLLM on DGX Spark

Kubernetes manifests for GPU-accelerated LLM inference using vLLM on the NVIDIA DGX Spark (GB10 Blackwell GPU, 128GB unified memory).

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│ Backend (rag-agent-backend)                             │
│ http://gpt-oss-120b:8000/v1                             │
└────────────────┬────────────────────────────────────────┘
                 │
                 ▼
┌─────────────────────────────────────────────────────────┐
│ Service: gpt-oss-120b (ClusterIP:8000)                  │
└────────────────┬────────────────────────────────────────┘
                 │
                 ▼
┌─────────────────────────────────────────────────────────┐
│ Deployment: gpt-oss-120b                                │
│ - Image: nvcr.io/nvidia/vllm:25.11-py3                  │
│ - Model: Qwen/Qwen2.5-VL-7B-Instruct-AWQ               │
│ - Quantization: AWQ-Marlin (4-bit)                       │
│ - GPU: 1x NVIDIA GB10 (spark-7eb5)                      │
│ - Context: 16,384 tokens                                 │
│ - PVC: model-cache-pvc (100Gi, Longhorn)                 │
└─────────────────────────────────────────────────────────┘
```

## Current Configuration

- **Model**: [`Qwen/Qwen2.5-VL-7B-Instruct-AWQ`](https://huggingface.co/Qwen/Qwen2.5-VL-7B-Instruct-AWQ) (7B params, 4-bit AWQ quantized)
- **Quantization**: AWQ-Marlin (vLLM's optimized Marlin kernel for AWQ)
- **Context Length**: 16,384 tokens
- **GPU**: 1x NVIDIA GB10 Blackwell (128GB unified memory)
- **Serving**: OpenAI-compatible API (`/v1/chat/completions`, `/v1/models`)
- **Features**: Tool calling (Hermes parser), prefix caching, vision support

## AWQ Quantization - Performance Comparison

The switch from the full-precision model to AWQ 4-bit quantization dramatically improved performance:

| Metric | Full Model (BF16) | AWQ 4-bit | Improvement |
|--------|-------------------|-----------|-------------|
| Weight loading into GPU | ~450s (7.5 min) | **18s** | **25x faster** |
| GPU memory (model weights) | ~14 GiB | **6.6 GiB** | **53% reduction** |
| Available KV cache memory | ~limited | **96 GiB** | Massive headroom |
| Max concurrent requests (16K ctx) | ~few | **109x** | Orders of magnitude more |
| Pod restarts (before fix) | 73 | **0** | Stable |
| Startup (cached, subsequent) | ~8 min | **~90s** | **5x faster** |

### Why AWQ is a Good Fit

1. **Minimal quality loss**: AWQ (Activation-aware Weight Quantization) preserves the most important weights at higher precision. For RAG workloads where the model synthesizes answers from retrieved context, the quality difference from full precision is negligible.

2. **Massive memory savings**: Reducing model weights from ~14 GiB to ~6.6 GiB frees up 96 GiB of GPU memory for KV cache. This means the model can handle 109 concurrent requests at full 16K context length instead of being memory-constrained.

3. **AWQ-Marlin kernel**: vLLM's Marlin kernel is specifically optimized for AWQ inference on NVIDIA GPUs, delivering near-full-precision throughput with 4-bit weights. This is faster than standard AWQ dequantization.

4. **Vision-language support**: The AWQ variant of Qwen2.5-VL retains full multimodal capabilities (text + image understanding), so no functionality is lost.

5. **Fast startup**: The 25x improvement in weight loading (18s vs 450s) means the pod recovers quickly from restarts, which is critical for a single-GPU deployment with no redundancy.

### Performance Considerations

- **First startup** after a fresh PVC takes ~10 minutes (model download from HuggingFace ~580s + loading ~18s + profiling ~40s). Subsequent restarts use the cached model and take ~60-90s.
- **`--enforce-eager`** is enabled to disable CUDA graph compilation, which ensures compatibility with the vision-language model's dynamic input shapes.
- **`--enable-prefix-caching`** reduces redundant computation for requests that share common prompt prefixes (e.g., system prompts in RAG).
- **`HF_HUB_OFFLINE`** can be set to `1` after the model is cached on the PVC to eliminate network dependency on startup.

## Files

### Base (`base/`)

| File | Purpose |
|------|---------|
| `gpt-oss-deployment.yaml` | vLLM inference deployment (AWQ-Marlin quantized) |
| `kaito-service.yaml` | ClusterIP service exposing port 8000 |
| `model-cache-pvc.yaml` | 100Gi PersistentVolumeClaim for HuggingFace model cache |
| `qwen-chat-template.yaml` | ConfigMap with Jinja chat template for Qwen |
| `qwen3-embedding-deployment.yaml` | Embedding service (CPU-based, separate from inference) |
| `qwen3-embedding-service.yaml` | ClusterIP service for embedding endpoint |
| `hf-external-secret.yaml` | HuggingFace token from Azure Key Vault |
| `kustomization.yaml` | Kustomize configuration |
| `kaito-workspace.yaml` | KAITO Workspace (reference only, not active) |

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
kubectl get pods -n rag-agent -l app=gpt-oss-120b
kubectl logs -n rag-agent -l app=gpt-oss-120b -f

# Test API endpoint
kubectl exec -it -n rag-agent deployment/rag-agent-backend -- \
  curl http://gpt-oss-120b:8000/v1/models
```

## Probe Configuration

The deployment uses a three-tier probe strategy to handle the slow model loading:

| Probe | Config | Purpose |
|-------|--------|---------|
| **Startup** | delay=60s, period=30s, threshold=30 (max 16 min) | Gates liveness/readiness during model loading |
| **Liveness** | period=30s, timeout=30s, threshold=5 | Restarts pod if inference hangs |
| **Readiness** | period=10s, timeout=10s, threshold=3 | Removes from service during issues |

The startup probe runs first; liveness and readiness probes don't begin until it succeeds.

## Backend Integration

Backend connects using the served model name as hostname:

```python
# assets/backend/agent.py
base_url=f"http://{self.current_model}:8000/v1"
# Resolves to: http://gpt-oss-120b.rag-agent.svc.cluster.local:8000
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
kubectl get pods -n rag-agent -l app=gpt-oss-120b

# Logs
kubectl logs -n rag-agent -l app=gpt-oss-120b --tail=100

# Service endpoints
kubectl get endpoints gpt-oss-120b -n rag-agent

# vLLM metrics (Prometheus-compatible)
kubectl exec -it -n rag-agent deployment/rag-agent-backend -- \
  curl http://gpt-oss-120b:8000/metrics
```

## Switching Models

Update the model in `gpt-oss-deployment.yaml`:

```yaml
args:
- "organization/model-name"
- "--served-model-name=gpt-oss-120b"   # Keep service name stable
- "--quantization=awq_marlin"           # If using AWQ variant
- "--dtype=half"                        # Required for AWQ
- "--max-model-len=CONTEXT_LENGTH"
```

After switching models, delete and recreate the PVC to clear the old cache:

```bash
kubectl scale deployment gpt-oss-120b -n rag-agent --replicas=0
kubectl delete pvc model-cache-pvc -n rag-agent
kubectl apply -f kustomize/models/base/model-cache-pvc.yaml
kubectl scale deployment gpt-oss-120b -n rag-agent --replicas=1
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
    cpu: "2"
    memory: "16Gi"
  limits:
    nvidia.com/gpu: 1
    cpu: "8"
    memory: "32Gi"
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

- [Qwen2.5-VL-7B-Instruct-AWQ](https://huggingface.co/Qwen/Qwen2.5-VL-7B-Instruct-AWQ)
- [vLLM AWQ Quantization](https://docs.vllm.ai/en/latest/features/quantization/supported_hardware.html)
- [vLLM Documentation](https://docs.vllm.ai/)
- [NVIDIA GPU Operator](https://docs.nvidia.com/datacenter/cloud-native/gpu-operator/)
