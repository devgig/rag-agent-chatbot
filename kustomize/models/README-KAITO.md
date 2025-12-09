# Deploying Models with KAITO

Since you already have KAITO installed in your cluster, **use this approach instead of the manual vLLM deployment**.

## Why KAITO is Better

✅ **Automated model deployment** - No manual Docker images or deployments
✅ **Built-in vLLM runtime** - Optimized for your GB10 GPU
✅ **HuggingFace integration** - Automatically downloads models at runtime
✅ **Simpler configuration** - Single YAML file vs multiple manifests
✅ **Production-ready** - Includes monitoring, health checks, and best practices

## Quick Start

### 1. Choose Your Model

First, determine what model you want to deploy. Common options for GB10 (128GB memory):

| Model | HuggingFace ID | Memory | Best For |
|-------|----------------|--------|----------|
| Llama 3.1 8B | `meta-llama/Meta-Llama-3.1-8B-Instruct` | ~16GB | Fast, general purpose |
| Llama 3.1 70B | `meta-llama/Meta-Llama-3.1-70B-Instruct` | ~70GB (FP8) | High quality, fits with quantization |
| Qwen 2.5 72B | `Qwen/Qwen2.5-72B-Instruct` | ~72GB (FP8) | Excellent reasoning |
| Mixtral 8x7B | `mistralai/Mixtral-8x7B-Instruct-v0.1` | ~90GB | Good quality/speed |
| DeepSeek V2 | `deepseek-ai/DeepSeek-V2` | ~120GB (quantized) | Very efficient MoE |

### 2. Update the Workspace Configuration

Edit `kaito-workspace.yaml` and change the `MODEL_ID`:

```bash
# Open the file
nano kustomize/models/kaito-workspace.yaml

# Change this line (around line 31):
MODEL_ID: "meta-llama/Meta-Llama-3.1-70B-Instruct"  # Your chosen model
```

**For gated models** (like Llama), uncomment the `HF_TOKEN` section and ensure you have the secret:

```bash
# Create HuggingFace token secret
kubectl create secret generic hf-credentials \
  --from-literal=token=hf_your_token_here \
  -n multi-agent-dev
```

### 3. Deploy the Workspace

```bash
# Deploy KAITO workspace
kubectl apply -f kustomize/models/kaito-workspace.yaml

# Deploy service
kubectl apply -f kustomize/models/kaito-service.yaml

# Watch deployment progress (can take 10-20 minutes for first model download)
kubectl get workspace gpt-oss-120b -n multi-agent-dev -w
```

### 4. Monitor Deployment

```bash
# Check workspace status
kubectl describe workspace gpt-oss-120b -n multi-agent-dev

# Check pods
kubectl get pods -n multi-agent-dev -l workspace=gpt-oss-120b

# View logs
kubectl logs -n multi-agent-dev -l workspace=gpt-oss-120b -f

# Check GPU usage
kubectl exec -n multi-agent-dev -l workspace=gpt-oss-120b -- nvidia-smi
```

### 5. Test the Model

```bash
# Port forward to local machine
kubectl port-forward -n multi-agent-dev svc/gpt-oss-120b 8000:8000

# In another terminal, test with curl
curl http://localhost:8000/v1/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "meta-llama/Meta-Llama-3.1-70B-Instruct",
    "prompt": "Hello, how are you?",
    "max_tokens": 50
  }'
```

## Configuration Options

### vLLM Runtime Parameters

In `kaito-workspace.yaml`, you can configure:

```yaml
env:
- name: TENSOR_PARALLEL_SIZE
  value: "1"                    # Number of GPUs (keep at 1 for single GPU)

- name: GPU_MEMORY_UTILIZATION
  value: "0.9"                  # Use 90% of GPU memory (0.0-1.0)

- name: MAX_MODEL_LEN
  value: "8192"                 # Maximum context length

- name: DTYPE
  value: "auto"                 # Data type: "auto", "float16", "bfloat16", "float32"

- name: QUANTIZATION
  value: "fp8"                  # Optional: "fp8", "awq", "gptq" for smaller memory
```

### Resource Limits

Adjust based on your model size:

```yaml
resources:
  requests:
    nvidia.com/gpu: 1
    cpu: "4"
    memory: "64Gi"      # Increase for larger models
  limits:
    nvidia.com/gpu: 1
    cpu: "16"
    memory: "120Gi"     # Increase for larger models
```

## Using Preset Models

KAITO has preset configurations for popular models. To use a preset instead of custom config:

```yaml
apiVersion: kaito.sh/v1beta1
kind: Workspace
metadata:
  name: gpt-oss-120b
  namespace: multi-agent-dev
spec:
  resource:
    count: 1
    labelSelector:
      matchLabels:
        nvidia.com/gpu.present: "true"
  inference:
    preset:
      name: "llama-3.1-70b-instruct"  # Use preset instead of custom template
```

[Check available presets](https://github.com/kaito-project/kaito/tree/main/presets/models)

## Comparison: KAITO vs Manual vLLM

| Feature | KAITO | Manual vLLM |
|---------|-------|-------------|
| **Setup Complexity** | Single YAML | Multiple manifests + Dockerfile |
| **Model Updates** | Edit YAML, reapply | Rebuild image, update deployment |
| **HuggingFace Integration** | Built-in | Manual setup |
| **Monitoring** | Prometheus metrics included | Must configure separately |
| **Image Management** | Pre-built reference images | Custom images in ACR |
| **vLLM Optimization** | Automatic for model type | Manual configuration |
| **Azure DevOps** | Apply YAML in pipeline | Full build/push/deploy cycle |

**Recommendation**: Use KAITO for production deployments.

## Troubleshooting

### Workspace Stuck in Pending

```bash
# Check workspace events
kubectl describe workspace gpt-oss-120b -n multi-agent-dev

# Check if GPU is available
kubectl get nodes -o custom-columns=NAME:.metadata.name,GPU:.status.allocatable.'nvidia\.com/gpu'
```

**Common causes**:
- GPU already allocated to another pod
- GPU not labeled correctly
- Tolerations don't match node taints

### Model Download Timeout

First model download can take 15-30 minutes for large models. Check logs:

```bash
kubectl logs -n multi-agent-dev -l workspace=gpt-oss-120b -f | grep -i download
```

**Solutions**:
- Increase `initialDelaySeconds` in readiness probe to 600
- Ensure internet connectivity from cluster
- Use persistent volume for model cache (add to workspace)

### Out of Memory

```bash
# Check GPU memory
kubectl exec -n multi-agent-dev -l workspace=gpt-oss-120b -- nvidia-smi
```

**Solutions**:
1. Reduce `GPU_MEMORY_UTILIZATION` to `0.85`
2. Add quantization: `QUANTIZATION: "fp8"`
3. Reduce `MAX_MODEL_LEN`
4. Use smaller model

### Service Not Reachable

```bash
# Check service
kubectl get svc gpt-oss-120b -n multi-agent-dev

# Test from within cluster
kubectl run -it --rm debug --image=curlimages/curl --restart=Never -n multi-agent-dev \
  -- curl http://gpt-oss-120b:8000/health
```

## Cleanup

```bash
# Delete workspace (this will delete pods)
kubectl delete workspace gpt-oss-120b -n multi-agent-dev

# Delete service
kubectl delete svc gpt-oss-120b -n multi-agent-dev

# Delete secrets (if needed)
kubectl delete secret hf-credentials -n multi-agent-dev
```

## Integration with Backend

Your backend is already configured to use `gpt-oss-120b:8000`. With KAITO:

1. The **service name remains the same**: `gpt-oss-120b`
2. The **API is OpenAI-compatible**: Works with existing backend code
3. **No backend changes needed**: Just deploy the workspace

The backend will automatically connect to the KAITO-managed inference service.

## Sources

- [KAITO Documentation](https://kaito-project.github.io/kaito/docs/)
- [KAITO GitHub](https://github.com/kaito-project/kaito)
- [Custom Model Integration](https://learn.microsoft.com/en-us/azure/aks/kaito-custom-inference-model)
- [vLLM on DGX Spark](https://build.nvidia.com/spark/vllm)
