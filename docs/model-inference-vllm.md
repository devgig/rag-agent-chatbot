# Model Inference - vLLM on DGX Spark

Kubernetes manifests for GPU-accelerated LLM inference using vLLM on the NVIDIA DGX Spark (GB10 Blackwell GPU, 128GB unified memory). The model runs in a shared `llm` namespace so multiple projects can use it.

## Architecture

```
┌─────────────────────────┐   ┌─────────────────────────┐
│ rag-agent-chatbot       │   │ ai-agents               │
│ (rag-agent namespace)   │   │ (ai-agents namespace)   │
│ http://nemotron-nano:8000/v1   │   │                         │
│ (via ExternalName svc)  │   │                         │
└───────────┬─────────────┘   └───────────┬─────────────┘
            │                             │
            ▼                             ▼
┌─────────────────────────────────────────────────────────┐
│ Namespace: llm (shared)                                 │
│ Service: nemotron-nano (ClusterIP:8000)                        │
│ nemotron-nano.llm.svc.cluster.local:8000                       │
└────────────────┬────────────────────────────────────────┘
                 │
                 ▼
┌─────────────────────────────────────────────────────────┐
│ Deployment: nemotron-nano                                      │
│ - Image: nvcr.io/nvidia/vllm:26.02-py3                 │
│ - Model: Qwen/Qwen3-30B-A3B-FP8                      │
│ - Architecture: Mixture-of-Experts (30B total, 3B active)│
│ - Quantization: FP8                                      │
│ - GPU: 1x NVIDIA GB10 (spark-7eb5)                      │
│ - Context: 16,384 tokens                                 │
│ - PVC: model-cache-pvc (100Gi, Longhorn)                 │
└─────────────────────────────────────────────────────────┘
```

## Current Configuration

- **Model**: [`nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-NVFP4`](https://huggingface.co/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-NVFP4) (30B total params, 3B active per token, MoE)
- **Architecture**: Mixture-of-Experts transformer — only 3B parameters are active per token, enabling high throughput despite 30B total
- **Quantization**: NVFP4 (~15GB weights — native Blackwell format)
- **Context Length**: 16,384 tokens (model supports up to 128K)
- **GPU**: 1x NVIDIA GB10 Blackwell (128GB unified memory)
- **Namespace**: `llm` (shared across projects)
- **Serving**: OpenAI-compatible API (`/v1/chat/completions`, `/v1/models`)
- **Features**: Native tool calling (`hermes` parser), prefix caching, CUDA graphs enabled

## Why Qwen3-30B-A3B

The Mixture-of-Experts architecture delivers dramatically better throughput than dense models on the GB10:

| Model | Type | Active params | Weight size | Throughput | Quality |
|-------|------|---------------|-------------|------------|---------|
| Nemotron-49B FP8 | Dense | 49B | ~50 GB | ~4.5 tok/s | Highest |
| Nemotron-49B NVFP4 | Dense | 49B | ~25 GB | ~8-12 tok/s | High |
| Qwen3-30B-A3B FP8 | MoE | 3B | ~30 GB | ~38 tok/s | Good |
| **Nemotron 3 Nano 30B NVFP4** | **MoE** | **3B** | **~15 GB** | **~56 tok/s** | **Good** |

### Why MoE wins on DGX Spark

1. **3B active parameters per token**: Only a fraction of weights are read per token, drastically reducing memory bandwidth pressure — the GB10's primary bottleneck
2. **~56 tok/s generation (vLLM)**: [Benchmarked by community](https://developer.nvidia.com/blog/scaling-autonomous-ai-agents-and-workloads-with-nvidia-dgx-spark) on DGX Spark — 12x faster than Nemotron-49B FP8
3. **NVFP4 on Blackwell**: Native hardware-accelerated quantization format, NVIDIA-optimized
4. **Shared serving**: Runs in `llm` namespace, used by both RAG chatbot and AI agent workloads

### Performance Considerations

- **First startup** requires downloading ~15GB of NVFP4 weights. Set `HF_HUB_OFFLINE=0` for the first run, then `1` after cached.
- **CUDA graphs** are enabled (no `--enforce-eager`) for optimized kernel replay during generation.
- **`--enable-prefix-caching`** reduces redundant computation for repeated system prompts.
- **`--tool-call-parser=hermes`** enables native tool/function calling.

## Shared Namespace Strategy

The model runs in the `llm` namespace and is consumed by multiple projects:

| Consumer | Connection method |
|----------|------------------|
| **rag-agent-chatbot** | ExternalName service `nemotron-nano` in `rag-agent` namespace → `nemotron-nano.llm.svc.cluster.local` |
| **ai-agents** | Direct cross-namespace DNS: `nemotron-nano.llm.svc.cluster.local:8000/v1` |

This avoids running duplicate model instances on a single GPU.

## Files

### Base (`base/`)

| File | Purpose |
|------|---------|
| `llm-namespace.yaml` | Shared `llm` namespace |
| `nemotron-nano-deployment.yaml` | vLLM inference deployment (Qwen3-30B-A3B FP8) |
| `nemotron-nano-service.yaml` | ClusterIP service in `llm` namespace |
| `nemotron-nano-externalname-service.yaml` | ExternalName alias in `rag-agent` namespace |
| `model-cache-pvc.yaml` | 100Gi PersistentVolumeClaim in `llm` namespace |
| `qwen3-embedding-*` | Moved to `kustomize/embedding/` (separate pipeline) |
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
kubectl get pods -n llm -l app=nemotron-nano
kubectl logs -n llm -l app=nemotron-nano -f

# Test API endpoint
kubectl exec -it -n rag-agent deployment/rag-agent-backend -- \
  curl http://nemotron-nano:8000/v1/models
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
# Resolves to: http://nemotron-nano:8000 → nemotron-nano.llm.svc.cluster.local:8000 (via ExternalName)
```

The `rag-agent` namespace has an ExternalName service that aliases `nemotron-nano` to `nemotron-nano.llm.svc.cluster.local`, so the backend code works with just the short name.

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
kubectl get pods -n llm -l app=nemotron-nano

# Logs
kubectl logs -n llm -l app=nemotron-nano --tail=100

# Service endpoints
kubectl get endpoints nemotron-nano -n llm

# vLLM metrics (Prometheus-compatible)
kubectl exec -it -n rag-agent deployment/rag-agent-backend -- \
  curl http://nemotron-nano:8000/metrics
```

## Switching Models

Update the model in `nemotron-nano-deployment.yaml`:

```yaml
args:
- "organization/model-name"
- "--served-model-name=nemotron-nano"   # Keep service name stable
- "--dtype=auto"
- "--max-model-len=CONTEXT_LENGTH"
```

After switching models, delete and recreate the PVC to clear the old cache:

```bash
kubectl scale deployment nemotron-nano -n llm --replicas=0
kubectl delete pvc model-cache-pvc -n llm
kubectl apply -f kustomize/models/base/model-cache-pvc.yaml
kubectl scale deployment nemotron-nano -n llm --replicas=1
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

- [NVIDIA-Nemotron-3-Nano-30B-A3B-NVFP4](https://huggingface.co/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-NVFP4) (current)
- [NVIDIA-Nemotron-3-Nano-30B-A3B-BF16](https://huggingface.co/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16) (base unquantized)
- [NVIDIA DGX Spark AI Agent Scaling Blog](https://developer.nvidia.com/blog/scaling-autonomous-ai-agents-and-workloads-with-nvidia-dgx-spark) (benchmark source)
- [vLLM Quantization](https://docs.vllm.ai/en/latest/features/quantization/supported_hardware.html)
- [vLLM Documentation](https://docs.vllm.ai/)
- [NVIDIA GPU Operator](https://docs.nvidia.com/datacenter/cloud-native/gpu-operator/)
