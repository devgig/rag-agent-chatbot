# LLM Model Inference Service Deployment

This directory contains Kubernetes manifests for deploying LLM inference services using vLLM on NVIDIA GB10 (Blackwell architecture) GPUs.

## Architecture

The model inference service runs separately from the backend application:

```
Backend (FastAPI) → gpt-oss-120b Service (vLLM) → GPU
```

- **Backend**: Orchestrates conversations, manages tools, handles chat history
- **Model Service**: Runs LLM inference on GPU with OpenAI-compatible API
- **GPU**: NVIDIA GB10 with 128GB unified memory (supports up to 200B parameter models)

## Components

### Model Service (gpt-oss-120b)
- **Inference Server**: vLLM optimized for Blackwell (sm_121) architecture
- **Base Image**: `nvcr.io/nvidia/pytorch:25.10-py3`
- **API**: OpenAI-compatible REST API on port 8000
- **GPU Requirements**: 1x NVIDIA GB10 GPU
- **Memory**: 64-120 GB RAM, 100GB cache volume

## Deployment

### Prerequisites

1. **Kubernetes cluster** with NVIDIA GPU operator installed
2. **GPU node** labeled with `nvidia.com/gpu.present: "true"`
3. **Azure Container Registry** credentials in Key Vault
4. **HuggingFace token** (optional, for gated models) in Key Vault as `huggingface-token`
5. **ExternalSecrets operator** installed for secret management

### Deploy to Cluster

```bash
# Build and push image via Azure DevOps (automatic on push to main)
# Or manually:
cd assets/models
docker build -t bytecourier.azurecr.io/multi-agent-chatbot-models:latest .
docker push bytecourier.azurecr.io/multi-agent-chatbot-models:latest

# Deploy using kustomize
kubectl apply -k kustomize/models/base -n multi-agent-dev

# Check deployment status
kubectl get pods -n multi-agent-dev -l app=gpt-oss-120b
kubectl logs -n multi-agent-dev -l app=gpt-oss-120b -f
```

### Verify Deployment

```bash
# Check if model service is healthy
kubectl exec -n multi-agent-dev deployment/gpt-oss-120b -- curl http://localhost:8000/health

# Test inference
kubectl port-forward -n multi-agent-dev svc/gpt-oss-120b 8000:8000

# In another terminal
curl http://localhost:8000/v1/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-oss-120b",
    "prompt": "Hello, how are you?",
    "max_tokens": 50
  }'
```

## Configuration

### Model Configuration

Edit `kustomize/models/base/deployment.yaml` to customize:

```yaml
env:
- name: MODEL_NAME
  value: "your-model-name"  # HuggingFace model ID or path

command:
- --model
- "$(MODEL_NAME)"
- --tensor-parallel-size
- "1"                        # Number of GPUs for tensor parallelism
- --gpu-memory-utilization
- "0.9"                      # GPU memory utilization (0.0-1.0)
- --max-model-len
- "8192"                     # Maximum sequence length
- --dtype
- "auto"                     # Data type (auto, float16, bfloat16, float32)
```

### Resource Limits

Current configuration for 120B parameter model:

```yaml
resources:
  requests:
    cpu: "4"
    memory: "64Gi"
    nvidia.com/gpu: "1"
  limits:
    cpu: "16"
    memory: "120Gi"
    nvidia.com/gpu: "1"
```

Adjust based on your model size:
- **7B models**: 16-32GB memory
- **13B models**: 32-64GB memory
- **70B models**: 64-128GB memory
- **120B+ models**: 120GB+ memory

## vLLM Configuration Options

Common vLLM server arguments:

| Argument | Description | Default |
|----------|-------------|---------|
| `--model` | HuggingFace model ID or local path | Required |
| `--tensor-parallel-size` | Number of GPUs for tensor parallelism | 1 |
| `--gpu-memory-utilization` | Fraction of GPU memory to use (0.0-1.0) | 0.9 |
| `--max-model-len` | Maximum sequence length | Model default |
| `--dtype` | Data type (auto, float16, bfloat16) | auto |
| `--quantization` | Quantization method (awq, gptq, fp8) | None |
| `--served-model-name` | Model name exposed in API | Same as --model |

For full options, see [vLLM documentation](https://docs.vllm.ai/).

## Model Selection

### Recommended Models for GB10 (128GB memory)

| Model | Parameters | Memory Required | Notes |
|-------|------------|-----------------|-------|
| Llama 3.1 8B | 8B | ~16GB | Fast, general purpose |
| Llama 3.1 70B | 70B | ~140GB FP16, ~70GB INT8 | Use quantization |
| Qwen 2.5 72B | 72B | ~144GB FP16, ~72GB INT8 | Use quantization |
| Mixtral 8x7B | 47B (sparse) | ~90GB | MoE architecture |
| DeepSeek V2 | 236B (sparse) | ~120GB with quantization | Very efficient MoE |

### Using Quantization

For models that don't fit in memory, use quantization:

```yaml
command:
- --model
- "meta-llama/Meta-Llama-3.1-70B"
- --quantization
- "fp8"                      # or "awq", "gptq"
- --gpu-memory-utilization
- "0.95"
```

## Troubleshooting

### Pod Stuck in Pending

```bash
# Check node GPU availability
kubectl get nodes -o custom-columns=NAME:.metadata.name,GPU:.status.allocatable.'nvidia\.com/gpu'

# Check pod events
kubectl describe pod -n multi-agent-dev -l app=gpt-oss-120b
```

**Solution**: Ensure no other pods are using the GPU, or add more GPU nodes.

### Out of Memory Errors

```bash
# Check GPU memory usage
kubectl exec -n multi-agent-dev deployment/gpt-oss-120b -- nvidia-smi
```

**Solutions**:
1. Reduce `--gpu-memory-utilization` to 0.85 or lower
2. Reduce `--max-model-len` to decrease KV cache size
3. Enable quantization (`--quantization fp8`)
4. Use a smaller model

### Model Download Timeout

```bash
# Check pod logs
kubectl logs -n multi-agent-dev -l app=gpt-oss-120b -f
```

**Solutions**:
1. Increase `initialDelaySeconds` in readiness probe
2. Pre-download model weights to persistent volume
3. Use model cache volume mount

### Connection Refused from Backend

```bash
# Test service connectivity
kubectl run -it --rm debug --image=curlimages/curl --restart=Never -- curl http://gpt-oss-120b:8000/health
```

**Solution**: Ensure service name matches backend configuration in `MODELS` env var.

## Azure DevOps Pipeline

The pipeline automatically:
1. Validates Dockerfile syntax
2. Builds Docker image with vLLM
3. Pushes to Azure Container Registry
4. Generates Kubernetes manifests
5. Deploys to K3s cluster

Triggered by changes to:
- `assets/models/**`
- `kustomize/models/**`

## Performance Optimization

### For GB10 Blackwell Architecture

1. **Use FP8 Quantization**: GB10 has hardware FP4/FP8 acceleration
   ```yaml
   - --quantization
   - "fp8"
   ```

2. **Optimize Batch Size**: Adjust based on workload
   ```yaml
   - --max-num-batched-tokens
   - "8192"
   ```

3. **Enable Prefix Caching**: For repeated prompts
   ```yaml
   - --enable-prefix-caching
   ```

4. **Adjust KV Cache**: For longer contexts
   ```yaml
   - --max-model-len
   - "16384"  # Increase as needed
   ```

## Security

- **Secrets**: Managed via ExternalSecrets operator from Azure Key Vault
- **Image Pull**: Uses ACR credentials from Key Vault
- **HuggingFace Token**: Optional, stored in Key Vault for gated models
- **Network**: ClusterIP service (internal only)

## References

- [vLLM Documentation](https://docs.vllm.ai/)
- [NVIDIA DGX Spark User Guide](https://docs.nvidia.com/dgx/dgx-spark/)
- [NVIDIA GB10 Specifications](https://www.techpowerup.com/gpu-specs/gb10.c4342)
- [vLLM GB10 Setup Guide](https://github.com/eelbaz/dgx-spark-vllm-setup)
