# Model Inference - vLLM on DGX Spark

Kubernetes manifests for GPU-accelerated LLM inference using vLLM on the NVIDIA DGX Spark (GB10 Blackwell GPU, 128GB unified memory). The model runs in a dedicated `llm` namespace, separate from application workloads, and is shared with the ai-agents platform (LiteLLM routes Claude Code CLI fallback traffic to the same endpoint).

## Architecture

```
┌─────────────────────────┐       ┌─────────────────────────┐
│ rag-agent-chatbot       │       │ ai-agents (LiteLLM)     │
│ (rag-agent namespace)   │       │ (ai-agents namespace)   │
│ http://qwen3-coder-next │       │ http://qwen3-coder-next │
│   :8000/v1              │       │   .llm.svc.cluster.local│
│ (via ExternalName svc)  │       │   :8000/v1              │
└───────────┬─────────────┘       └───────────┬─────────────┘
            │                                 │
            └────────────────┬────────────────┘
                             ▼
┌─────────────────────────────────────────────────────────┐
│ Namespace: llm                                          │
│ Service: qwen3-coder-next (ClusterIP:8000)              │
│ qwen3-coder-next.llm.svc.cluster.local:8000             │
└────────────────┬────────────────────────────────────────┘
                 │
                 ▼
┌─────────────────────────────────────────────────────────┐
│ Deployment: qwen3-coder-next                            │
│ - Image: nvcr.io/nvidia/vllm:26.03.post1-py3            │
│ - Model: Qwen/Qwen3-Coder-Next                          │
│ - Architecture: MoE + hybrid attention (80B / 3B active)│
│ - Quantization: FP8 (runtime, via --quantization)       │
│ - GPU: 1x NVIDIA GB10 (spark-7eb5)                      │
│ - Context: 131,072 tokens                               │
│ - PVC: model-cache-pvc (100Gi, Longhorn)                │
└─────────────────────────────────────────────────────────┘
```

## Current Configuration

- **Model**: [`Qwen/Qwen3-Coder-Next`](https://huggingface.co/Qwen/Qwen3-Coder-Next) (80B total params, 3B active per token, MoE + hybrid attention)
- **Architecture**: Mixture-of-Experts with hybrid (linear + standard) attention — only 3B parameters active per token, enabling high throughput despite 80B total
- **Quantization**: FP8 at runtime (`--quantization=fp8`) — ~80GB weights, leaves ~20-25GB headroom for KV cache at 128K context
- **Context Length**: 131,072 tokens (model natively supports 256K; single-GPU memory budget caps us at 128K)
- **GPU**: 1x NVIDIA GB10 Blackwell (128GB unified memory)
- **Namespace**: `llm` (dedicated, shared with ai-agents platform)
- **Serving**: OpenAI-compatible API (`/v1/chat/completions`, `/v1/models`)
- **Features**: Native tool calling (`qwen3_coder` parser), prefix caching, CUDA graphs enabled

## Why Qwen3-Coder-Next

Built specifically for coding agents — "long-horizon reasoning, complex tool usage, and recovery from execution failures" in Qwen's own framing. For an agentic workflow where the LLM drives a coding task across multiple turns, tool calls, and file edits, this matters more than single-shot HumanEval scores.

| Benchmark | Qwen3-Coder-Next | Nemotron 3 Nano 30B |
|-----------|------------------|---------------------|
| SWE-Bench Verified (with SWE-Agent scaffold) | **>70%** | Not primary benchmark |
| SWE-Bench Pro | 44.3% (matches 10-20× larger active params) | — |
| LiveCodeBench v6 | — | 68.3% |
| HumanEval | — | 78.05% |
| Agent/tool-use training | Purpose-built | Strong but general |
| Context | **256K native** (128K configured) | 128K native (16K configured) |

### Why this beats the Nemotron Nano deployment on the same hardware

1. **Purpose-built for agentic coding**. SWE-Bench Verified >70% means 7 of 10 real GitHub issues get resolved with a proper scaffold — translates directly to "will the agent actually finish the PR."
2. **Claude Sonnet 4.5-level on coding benchmarks** at 3B active params. Same throughput profile as Nemotron Nano (3B active) with substantially better code quality.
3. **256K native context** for large codebases, currently configured at 128K to leave KV-cache headroom on a single GB10.
4. **Shared model serving**. Both rag-agent-chatbot and ai-agents consume the same endpoint — no duplicate deployments, no model-cache duplication.

### Performance Considerations

- **First boot** requires downloading ~80Gi of FP8 weights from HuggingFace. Expect 10-30 min before the pod reports Ready. Leave `HF_HUB_OFFLINE` unset for the first run; add `HF_HUB_OFFLINE=1` once the PVC is populated to skip network checks on subsequent restarts.
- **CUDA graphs** are enabled (no `--enforce-eager`) for optimized kernel replay during generation.
- **`--enable-prefix-caching`** reduces redundant computation for repeated system prompts.
- **`--tool-call-parser=qwen3_coder`** enables Qwen3-Coder-Next's native structured tool calling. Requires vLLM ≥ 0.15, which is included in `nvcr.io/nvidia/vllm:26.03.post1-py3`.
- **`--quantization=fp8`** activates runtime FP8 quantization. The base `Qwen/Qwen3-Coder-Next` repo ships BF16 weights; FP8 reduces memory from ~160GB to ~80GB.

## Namespace Strategy

The model runs in the `llm` namespace, separate from application workloads and shared across platforms:

| Consumer | Connection method |
|----------|------------------|
| **rag-agent-chatbot** | ExternalName service `qwen3-coder-next` in `rag-agent` namespace → `qwen3-coder-next.llm.svc.cluster.local` |
| **ai-agents (LiteLLM)** | Direct `qwen3-coder-next.llm.svc.cluster.local:8000/v1` (cross-namespace) |

Separating model serving into its own namespace keeps GPU resources isolated from application deployment lifecycle.

## Files

### Base (`base/`)

| File | Purpose |
|------|---------|
| `llm-namespace.yaml` | Shared `llm` namespace |
| `qwen3-coder-next-deployment.yaml` | vLLM inference deployment |
| `qwen3-coder-next-service.yaml` | ClusterIP service in `llm` namespace |
| `qwen3-coder-next-externalname-service.yaml` | ExternalName alias in `rag-agent` namespace (lives under `kustomize/backend/base/`) |
| `model-cache-pvc.yaml` | 100Gi PersistentVolumeClaim in `llm` namespace |
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
kubectl get pods -n llm -l app=qwen3-coder-next
kubectl logs -n llm -l app=qwen3-coder-next -f

# Test API endpoint
kubectl exec -it -n rag-agent deployment/rag-agent-backend -- \
  curl http://qwen3-coder-next:8000/v1/models
```

### Migrating from the previous Nemotron Nano deployment

```bash
# Free the GPU
kubectl delete deployment -n llm nemotron-nano
kubectl delete svc -n llm nemotron-nano
kubectl delete svc -n rag-agent nemotron-nano

# The PVC (model-cache-pvc) stays -- but the cached Nemotron weights won't be
# used by Qwen. Reclaim disk either by deleting + recreating, or just let the
# new model download alongside (100Gi PVC has room for both for a while).
kubectl scale deployment qwen3-coder-next -n llm --replicas=0
kubectl delete pvc model-cache-pvc -n llm
kubectl apply -f kustomize/models/base/model-cache-pvc.yaml

# Deploy the new stack
kubectl apply -k kustomize/models/overlays/dev
```

## Probe Configuration

The deployment uses a three-tier probe strategy to handle the slow model loading:

| Probe | Config | Purpose |
|-------|--------|---------|
| **Startup** | delay=180s, period=30s, threshold=180 (max 93 min) | Gates liveness/readiness during model loading; weight download can take 10-30 min on first boot |
| **Liveness** | period=30s, timeout=30s, threshold=5 | Restarts pod if inference hangs |
| **Readiness** | period=10s, timeout=10s, threshold=3 | Removes from service during issues |

The startup probe runs first; liveness and readiness probes don't begin until it succeeds.

## Backend Integration

Backend connects using the served model name as hostname:

```python
# assets/backend/agent.py
base_url=f"http://{self.current_model}:8000/v1"
# Resolves to: http://qwen3-coder-next:8000 → qwen3-coder-next.llm.svc.cluster.local:8000 (via ExternalName)
```

The `rag-agent` namespace has an ExternalName service that aliases `qwen3-coder-next` to `qwen3-coder-next.llm.svc.cluster.local`, so the backend code works with just the short name.

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
kubectl get pods -n llm -l app=qwen3-coder-next

# Logs
kubectl logs -n llm -l app=qwen3-coder-next --tail=100

# Service endpoints
kubectl get endpoints qwen3-coder-next -n llm

# vLLM metrics (Prometheus-compatible)
kubectl exec -it -n rag-agent deployment/rag-agent-backend -- \
  curl http://qwen3-coder-next:8000/metrics
```

## Switching Models

Update the model in `qwen3-coder-next-deployment.yaml`:

```yaml
args:
- "organization/model-name"
- "--served-model-name=qwen3-coder-next"   # Keep service name stable
- "--dtype=auto"
- "--max-model-len=CONTEXT_LENGTH"
```

After switching models, delete and recreate the PVC to clear the old cache:

```bash
kubectl scale deployment qwen3-coder-next -n llm --replicas=0
kubectl delete pvc model-cache-pvc -n llm
kubectl apply -f kustomize/models/base/model-cache-pvc.yaml
kubectl scale deployment qwen3-coder-next -n llm --replicas=1
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
    memory: "32Gi"
  limits:
    nvidia.com/gpu: 1
    cpu: "16"
    memory: "96Gi"
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

- [Qwen/Qwen3-Coder-Next on HuggingFace](https://huggingface.co/Qwen/Qwen3-Coder-Next)
- [Qwen3-Coder-Next blog (Qwen team)](https://qwen.ai/blog?id=qwen3-coder-next)
- [vLLM qwen3_coder tool parser docs](https://docs.vllm.ai/en/latest/api/vllm/tool_parsers/qwen3coder_tool_parser/)
- [NVIDIA vLLM Container Release Notes](https://docs.nvidia.com/deeplearning/frameworks/vllm-release-notes/index.html)
- [vLLM Quantization hardware matrix](https://docs.vllm.ai/en/latest/features/quantization/supported_hardware.html)
- [NVIDIA GPU Operator](https://docs.nvidia.com/datacenter/cloud-native/gpu-operator/)
