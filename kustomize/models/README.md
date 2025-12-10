# KAITO Model Inference - BYO GPU Mode

This directory contains Kubernetes manifests for deploying GPU-accelerated LLM inference using KAITO (Kubernetes AI Toolchain Operator) in BYO (Bring Your Own) GPU mode with NVIDIA GPU Operator.

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│ Backend (multi-agent-backend)                           │
│ http://gpt-oss-120b:8000/v1                            │
└────────────────┬────────────────────────────────────────┘
                 │
                 ▼
┌─────────────────────────────────────────────────────────┐
│ Service: gpt-oss-120b (ClusterIP)                       │
│ Port: 8000                                              │
└────────────────┬────────────────────────────────────────┘
                 │
                 ▼
┌─────────────────────────────────────────────────────────┐
│ Deployment: gpt-oss-120b                                │
│ - Container: vllm/vllm-openai:latest                    │
│ - Model: microsoft/Phi-3.5-mini-instruct                │
│ - GPU: nvidia.com/gpu=1                                 │
│ - Node: spark-7eb5 (GPU node)                           │
└─────────────────────────────────────────────────────────┘
```

## Files

### Base (`base/`)

| File | Purpose |
|------|---------|
| `kustomization.yaml` | Main kustomize configuration with architecture docs |
| `kaito-workspace.yaml` | KAITO Workspace (reference only, blocked by NodeClaim) |
| `gpt-oss-deployment.yaml` | **Actual deployment** (workaround for KAITO limitation) |
| `kaito-service.yaml` | ClusterIP service exposing port 8000 |
| `hf-external-secret.yaml` | HuggingFace token from Azure Key Vault |

### Overlays

- `overlays/dev/` - Development environment configuration

## Current Configuration

- **Model**: `microsoft/Phi-3.5-mini-instruct` (3.8B parameters, open-source)
- **Context Length**: 4096 tokens
- **GPU**: 1x NVIDIA GB10 (via NVIDIA GPU Operator)
- **Node**: `spark-7eb5` (update in `kaito-workspace.yaml` line 24)
- **API**: OpenAI-compatible via vLLM (`/v1/chat/completions`, `/v1/models`)

## KAITO BYO GPU Mode Limitation

**Issue**: KAITO with `disableNodeAutoProvisioning=true` still expects NodeClaims to exist, blocking deployment creation.

**Workaround**:
- `kaito-workspace.yaml` - Kept for reference and documentation
- `gpt-oss-deployment.yaml` - **Manually created deployment** with identical spec

Both files maintain the same configuration to ensure consistency.

## Deployment

### Via Azure DevOps

Automatically deployed when changes are pushed to `kustomize/models/**`:

```yaml
# Trigger: azure-pipelines-models.yaml
# Pipeline validates with kustomize and publishes artifacts
```

### Manual Deployment

```bash
# Build and preview manifests
kubectl kustomize kustomize/models/overlays/dev

# Apply to cluster
kubectl apply -k kustomize/models/overlays/dev

# Check status
kubectl get pods -n multi-agent-dev -l workspace=gpt-oss-120b
kubectl logs -n multi-agent-dev -l workspace=gpt-oss-120b -f

# Test API endpoint
kubectl run -it --rm debug --image=curlimages/curl --restart=Never \
  -- curl http://gpt-oss-120b.multi-agent-dev.svc.cluster.local:8000/v1/models
```

## Switching Models

### Using Meta-Llama Models

Meta-Llama models require HuggingFace access approval:

1. **Request Access**: Visit [Meta-Llama-3.1-8B-Instruct](https://huggingface.co/meta-llama/Meta-Llama-3.1-8B-Instruct)
2. **Update Configuration**: Edit both files:
   - `kaito-workspace.yaml` line 40
   - `gpt-oss-deployment.yaml` line 34

   Change model to: `meta-llama/Meta-Llama-3.1-8B-Instruct`

3. **Ensure Token**: HF_TOKEN is automatically injected from Azure Key Vault (`hugging-face-read-only-token`)

4. **Deploy**: `kubectl apply -k kustomize/models/overlays/dev`

### Using Other Models

Compatible with any HuggingFace model supported by vLLM:

```yaml
# In both workspace and deployment files
args:
  - "--model=organization/model-name"
  - "--max-model-len=CONTEXT_LENGTH"  # Adjust based on model
```

## GPU Configuration

### Prerequisites

- NVIDIA GPU Operator installed
- Nodes labeled: `nvidia.com/gpu.present=true`
- GPU resources exposed: `nvidia.com/gpu`

### Node Selection

Update target GPU node in `kaito-workspace.yaml`:

```yaml
resource:
  preferredNodes:
  - YOUR-GPU-NODE-NAME  # Default: spark-7eb5
```

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

## Backend Integration

Backend automatically connects using model name as hostname:

```python
# assets/backend/agent.py:162
base_url=f"http://{self.current_model}:8000/v1"

# Resolves to: http://gpt-oss-120b:8000/v1
# Via service: gpt-oss-120b.multi-agent-dev.svc.cluster.local:8000
```

**No backend code changes required!**

## Monitoring

### Health Checks

```bash
# Check pod status
kubectl get pods -n multi-agent-dev -l workspace=gpt-oss-120b

# View logs
kubectl logs -n multi-agent-dev -l workspace=gpt-oss-120b --tail=100

# Check service endpoints
kubectl get endpoints gpt-oss-120b -n multi-agent-dev
```

### Startup Time

- **Image Pull**: ~10-15 minutes (first time, 8GB image)
- **Model Download**: ~2-5 minutes (Phi-3.5: ~8GB)
- **Model Loading**: ~30-60 seconds
- **Total**: ~15-20 minutes first deployment

Subsequent restarts: ~2-3 minutes (cached image and model)

### API Testing

```bash
# Test models endpoint
kubectl exec -it -n multi-agent-dev deployment/multi-agent-backend -- \
  curl http://gpt-oss-120b:8000/v1/models

# Test chat completion
kubectl exec -it -n multi-agent-dev deployment/multi-agent-backend -- \
  curl -X POST http://gpt-oss-120b:8000/v1/chat/completions \
    -H "Content-Type: application/json" \
    -d '{"model":"microsoft/Phi-3.5-mini-instruct","messages":[{"role":"user","content":"Hello!"}],"max_tokens":50}'
```

## Troubleshooting

### Pod Not Starting

```bash
# Check events
kubectl describe pod -n multi-agent-dev -l workspace=gpt-oss-120b

# Common issues:
# - Insufficient GPU: Check node has nvidia.com/gpu available
# - Image pull: Verify internet connectivity
# - Node selector: Ensure node has nvidia.com/gpu.present=true label
```

### Model Access Denied (403/401)

```bash
# For gated models (Meta-Llama):
# 1. Request access on HuggingFace
# 2. Verify token in Azure Key Vault: hugging-face-read-only-token
# 3. Check secret exists: kubectl get secret hf-credentials -n multi-agent-dev
```

### Health Check Failing

```bash
# Check logs for model loading progress
kubectl logs -n multi-agent-dev -l workspace=gpt-oss-120b --tail=200

# Health checks start after 300s (initialDelaySeconds)
# Model must be fully loaded before health check passes
```

## Azure Key Vault Integration

HuggingFace token is managed via External Secrets Operator:

```yaml
# External Secret pulls from Azure Key Vault
source: azure-keyvault-secret-store
key: hugging-face-read-only-token

# Creates Kubernetes Secret
target: hf-credentials (key: token)

# Injected into pod as HF_TOKEN environment variable
```

## Production Considerations

1. **Resource Limits**: Adjust based on model size and GPU memory
2. **Model Caching**: Use persistent volume for `/root/.cache/huggingface`
3. **Autoscaling**: KAITO workspace `count` can scale replicas
4. **Model Versioning**: Pin specific model revisions in production
5. **Monitoring**: Add Prometheus metrics from vLLM `/metrics` endpoint

## References

- [KAITO Documentation](https://github.com/Azure/kaito)
- [vLLM Documentation](https://docs.vllm.ai/)
- [NVIDIA GPU Operator](https://docs.nvidia.com/datacenter/cloud-native/gpu-operator/)
- [External Secrets Operator](https://external-secrets.io/)
