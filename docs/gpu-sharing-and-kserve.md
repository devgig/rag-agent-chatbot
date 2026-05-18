# GPU Sharing Patterns for DGX Spark on K3s

Strategies for sharing a single NVIDIA DGX Spark (Blackwell GB10, 128GB unified memory) across multiple GPU workloads in a K3s cluster — with a focus on agentic and Agent-to-Agent (A2A) architectures.

## Current State

| Resource | Value |
|----------|-------|
| GPU Node | `spark-7eb5` — NVIDIA DGX Spark, Blackwell GB10 |
| GPU Memory | 128GB unified (shared CPU/GPU address space) |
| CUDA | 13.0 (Driver 580.95) |
| Current Workload | Nemotron 3 Nano 30B NVFP4 via vLLM (55% GPU mem utilization) |
| GPU Resource | `nvidia.com/gpu: 1` — exclusive allocation to one pod |

**The problem:** Kubernetes allocates the GPU as a single indivisible resource. The current `nemotron-nano` deployment claims the entire GPU, blocking any other pod from scheduling GPU workloads.

---

## GPU Sharing Strategies (Ranked by Fit)

### 1. NVIDIA GPU Time-Slicing (Recommended Starting Point)

Time-slicing lets the GPU Operator advertise a single physical GPU as multiple virtual GPUs. Pods take turns on the GPU via context switching — no code changes required.

**Why it fits:**
- Simplest to set up on K3s with the existing GPU Operator
- No hardware partitioning required (MIG is not supported on GB10)
- Works with any CUDA workload (vLLM, llama.cpp, custom models)
- 128GB unified memory means generous headroom for multiple models

**Setup:**

Create a ConfigMap for the GPU Operator to define time-slicing:

```yaml
# kustomize/gpu-sharing/time-slicing-config.yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: gpu-sharing-config
  namespace: gpu-operator  # or wherever your GPU Operator lives
data:
  any: |-
    version: v1
    sharing:
      timeSlicing:
        renameByDefault: false
        failRequestsGreaterThanOne: false
        resources:
          - name: nvidia.com/gpu
            replicas: 4          # Expose 1 physical GPU as 4 virtual GPUs
```

Then patch the ClusterPolicy:

```bash
kubectl patch clusterpolicy/cluster-policy \
  -n gpu-operator --type merge \
  -p '{"spec": {"devicePlugin": {"config": {"name": "gpu-sharing-config", "default": "any"}}}}'
```

After rollout, `kubectl describe node spark-7eb5` will show:

```
Allocatable:
  nvidia.com/gpu: 4    # was 1
```

**Update your deployments** to request a fraction:

```yaml
# nemotron-nano-deployment.yaml — reduce from 1 to 1 virtual GPU slice
resources:
  limits:
    nvidia.com/gpu: 1   # now 1 of 4 slices, not 1 of 1
```

**Trade-offs:**
- No memory isolation — a misbehaving pod can OOM the GPU for everyone
- Context switching adds ~5-15% overhead per concurrent workload
- You must manually manage total GPU memory across pods (128GB / N workloads)

**Memory budget example with 4 slices:**

| Workload | GPU Memory | Purpose |
|----------|-----------|---------|
| vLLM (Nemotron Nano 30B) | ~15GB (`gpu-memory-utilization` at 0.65) | Primary LLM inference |
| Agent Tooling Model | ~30GB | Smaller model for tool-use/routing |
| Embedding (GPU-accel) | ~8GB | Optional: move embedding to GPU |
| Reserved/Headroom | ~40GB | Burst capacity or additional agents |

---

### 2. NVIDIA MPS (Multi-Process Service)

MPS enables true concurrent GPU kernel execution from multiple processes, rather than time-multiplexing. Better throughput than time-slicing when multiple workloads are active simultaneously.

**Why consider it:**
- Higher GPU utilization than time-slicing (overlapping compute + memory transfers)
- Lower latency for concurrent inference requests
- Ideal when multiple agent processes hit the GPU simultaneously

**Setup with GPU Operator:**

```yaml
# ClusterPolicy patch for MPS
apiVersion: nvidia.com/v1
kind: ClusterPolicy
metadata:
  name: cluster-policy
spec:
  mps:
    enabled: true
  devicePlugin:
    config:
      name: mps-config
      default: any
```

```yaml
# mps-config ConfigMap
apiVersion: v1
kind: ConfigMap
metadata:
  name: mps-config
  namespace: gpu-operator
data:
  any: |-
    version: v1
    sharing:
      mps:
        renameByDefault: false
        failRequestsGreaterThanOne: false
        resources:
          - name: nvidia.com/gpu
            replicas: 4
```

**Trade-offs:**
- All MPS clients share a single CUDA context — one crash can take down all GPU workloads
- Requires GPU Operator v23.9+ with MPS support
- Slightly more complex debugging (shared fault domain)

---

### 3. KServe for Multi-Model GPU Serving

KServe provides a Kubernetes-native model serving layer with autoscaling, canary rollouts, and — critically — **ModelMesh** for multiplexing models on shared GPU infrastructure.

**Why KServe matters for your setup:**
- **ModelMesh** intelligently loads/unloads models from GPU memory on demand
- Scale-to-zero for infrequently used models (free up GPU for active workloads)
- Standard InferenceService CRD — agents call models via a uniform API
- Built-in request batching and model versioning

#### Option A: KServe Serverless (Recommended)

Uses Knative for scale-to-zero. Models load into GPU memory when called, unload when idle.

**Install KServe on K3s:**

```bash
# Install Knative Serving (required for serverless mode)
kubectl apply -f https://github.com/knative/serving/releases/download/knative-v1.17.0/serving-crds.yaml
kubectl apply -f https://github.com/knative/serving/releases/download/knative-v1.17.0/serving-core.yaml

# Install KServe
kubectl apply -f https://github.com/kserve/kserve/releases/download/v0.14.1/kserve.yaml
kubectl apply -f https://github.com/kserve/kserve/releases/download/v0.14.1/kserve-cluster-resources.yaml
```

**Define InferenceServices:**

```yaml
# Primary LLM — Nemotron 70B via vLLM runtime
apiVersion: serving.kserve.io/v1beta1
kind: InferenceService
metadata:
  name: qwen-7b-llm
  namespace: rag-agent
spec:
  predictor:
    minReplicas: 1          # Always warm for primary chat
    maxReplicas: 1
    model:
      modelFormat:
        name: vllm
      runtime: kserve-vllm
      storageUri: "hf://nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-NVFP4"
      resources:
        limits:
          nvidia.com/gpu: 1
        requests:
          cpu: "2"
          memory: "16Gi"
    nodeSelector:
      nvidia.com/gpu.present: "true"
    tolerations:
      - key: nvidia.com/gpu
        operator: Equal
        value: present
        effect: NoSchedule
```

```yaml
# Tool-use agent model — scale to zero when idle
apiVersion: serving.kserve.io/v1beta1
kind: InferenceService
metadata:
  name: tool-agent-model
  namespace: rag-agent
spec:
  predictor:
    minReplicas: 0          # Scale to zero!
    maxReplicas: 1
    scaleTarget: 1
    scaleMetric: concurrency
    model:
      modelFormat:
        name: vllm
      runtime: kserve-vllm
      storageUri: "hf://nvidia/Nemotron-3-8B-Instruct"
      resources:
        limits:
          nvidia.com/gpu: 1
        requests:
          cpu: "2"
          memory: "8Gi"
    nodeSelector:
      nvidia.com/gpu.present: "true"
```

**Requires time-slicing or MPS** to allow both InferenceServices to schedule on the same GPU.

#### Option B: KServe ModelMesh (Multi-Model on Single GPU)

ModelMesh packs multiple models into a single serving runtime, dynamically loading/unloading from GPU memory based on request traffic.

```yaml
apiVersion: serving.kserve.io/v1alpha1
kind: ServingRuntime
metadata:
  name: vllm-modelmesh
  namespace: rag-agent
spec:
  supportedModelFormats:
    - name: vllm
      version: "1"
      autoSelect: true
  multiModel: true
  grpcDataEndpoint: port:8001
  grpcEndpoint: port:8085
  containers:
    - name: vllm
      image: nvcr.io/nvidia/vllm:26.02-py3
      resources:
        limits:
          nvidia.com/gpu: 1
        requests:
          cpu: "4"
          memory: "32Gi"
```

**Best for:** Many small-to-medium models sharing one GPU, where not all are active at once.

---

### 4. vLLM Native Multi-Model via LoRA

If your agent models are fine-tuned variants of the same base, vLLM can serve multiple LoRA adapters from a single base model — zero additional GPU memory per adapter.

```yaml
# Single vLLM deployment serving multiple "models"
args:
  - "--model=nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-NVFP4"
  - "--enable-lora"
  - "--lora-modules"
  - "chat-agent=/models/lora/chat"
  - "tool-agent=/models/lora/tools"
  - "routing-agent=/models/lora/router"
  - "--max-loras=4"
  - "--max-lora-rank=16"
```

Agents call different model names via the OpenAI-compatible API:

```python
# Chat agent
client.chat.completions.create(model="chat-agent", ...)

# Tool-use agent
client.chat.completions.create(model="tool-agent", ...)
```

**Best for:** Multiple specialized agents that share the same base model architecture.

---

## Agentic A2A Architecture on K3s

### Design Pattern: Shared Inference Backbone

Rather than each agent owning a GPU, deploy a central inference layer that all agents share:

```
┌─────────────────────────────────────────────────────────┐
│                    K3s Cluster                           │
│                                                         │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐              │
│  │ Agent A  │  │ Agent B  │  │ Agent C  │  (CPU pods)  │
│  │ Planner  │  │ Executor │  │ Reviewer │  on Pi/      │
│  │          │  │          │  │          │  storage      │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘  nodes       │
│       │              │              │                    │
│       └──────────────┼──────────────┘                   │
│                      │                                  │
│            ┌─────────▼──────────┐                       │
│            │  Istio Service Mesh │                       │
│            │  (L7 routing, mTLS)│                       │
│            └─────────┬──────────┘                       │
│                      │                                  │
│  ┌───────────────────▼───────────────────┐              │
│  │       GPU Inference Layer             │              │
│  │       (spark-7eb5 node)               │              │
│  │                                       │              │
│  │  ┌─────────┐  ┌─────────┐            │              │
│  │  │ vLLM    │  │ vLLM    │  (time-    │              │
│  │  │ Primary │  │ Agent   │   sliced)  │              │
│  │  │ 7B      │  │ Small   │            │              │
│  │  └─────────┘  └─────────┘            │              │
│  └───────────────────────────────────────┘              │
│                                                         │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐              │
│  │ Milvus   │  │ Postgres │  │ Embedding│  (CPU)       │
│  │ VectorDB │  │          │  │ Service  │              │
│  └──────────┘  └──────────┘  └──────────┘              │
└─────────────────────────────────────────────────────────┘
```

### Agent-to-Agent Communication Patterns

#### Pattern 1: A2A Protocol over Istio (Google A2A)

Google's [Agent2Agent protocol](https://github.com/google/A2A) defines a standard JSON-RPC interface for agent interop. Deploy each agent as a K8s Service and route via Istio:

```yaml
# Agent A — Planner
apiVersion: apps/v1
kind: Deployment
metadata:
  name: agent-planner
  namespace: rag-agent
  labels:
    app: agent-planner
    agent-role: planner
spec:
  replicas: 1
  template:
    metadata:
      labels:
        app: agent-planner
    spec:
      nodeSelector:
        node-type: storage      # CPU-only, runs on storage/compute nodes
      containers:
        - name: agent
          image: bytecourier.azurecr.io/agent-planner:latest
          ports:
            - containerPort: 8000
          env:
            - name: LLM_ENDPOINT
              value: "http://nemotron-nano.llm.svc.cluster.local:8000"
            - name: PEER_AGENTS
              value: "http://agent-executor.rag-agent.svc.cluster.local:8000,http://agent-reviewer.rag-agent.svc.cluster.local:8000"
          resources:
            requests:
              cpu: 500m
              memory: 512Mi
            limits:
              cpu: 2
              memory: 2Gi
```

Each agent exposes the A2A protocol endpoints:
- `POST /.well-known/agent.json` — Agent Card (capabilities, skills)
- `POST /a2a` — JSON-RPC task submission
- Supports streaming via SSE for long-running agent tasks

#### Pattern 2: LangGraph Multi-Agent with Shared State

Since the backend already uses LangGraph, extend it with a multi-agent supervisor pattern where sub-agents are separate deployments:

```python
# backend agent orchestrator
from langgraph.graph import StateGraph
from langchain_openai import ChatOpenAI

# All agents share the same GPU-backed LLM endpoint
llm = ChatOpenAI(
    base_url="http://nemotron-nano.llm.svc.cluster.local:8000/v1",
    model="nemotron-nano",
)

# Sub-agents as remote services (CPU pods)
planner = RemoteAgent("http://agent-planner:8000/a2a")
executor = RemoteAgent("http://agent-executor:8000/a2a")
reviewer = RemoteAgent("http://agent-reviewer:8000/a2a")
```

#### Pattern 3: Tool Servers as Independent Pods

Deploy specialized tool servers as independent pods that the central agent orchestrator invokes:

```
Backend (LangGraph Agent)
  ├── Tool: code-execution-server (CPU pod)
  ├── Tool: web-search-server (CPU pod)
  ├── Tool: database-query-server (CPU pod)
  └── LLM: vLLM on GPU (inference calls)
```

Each tool server is a lightweight CPU pod. Only the LLM inference touches the GPU.

---

## Recommended Implementation Path

### Phase 1: Enable GPU Time-Slicing (Day 1)

1. Create the time-slicing ConfigMap with `replicas: 4`
2. Patch the GPU Operator ClusterPolicy
3. Adjust vLLM `gpu-memory-utilization` as needed (currently 0.65)
4. Verify `nvidia.com/gpu: 4` on the node
5. Existing workload continues unchanged (now uses 1 of 4 slices)

### Phase 2: Deploy a Second Model (Week 1)

1. Deploy a smaller model for agent routing/tool-use (e.g., Qwen2.5-3B-Instruct)
2. Give it 1 GPU slice with ~25GB memory budget
3. Test concurrent inference on both models
4. Profile GPU utilization with `nvidia-smi dmon`

### Phase 3: Build Agent Services (Week 2-3)

1. Create agent pods as CPU-only deployments on compute/storage nodes
2. Each agent calls the shared GPU inference layer via cluster-internal DNS
3. Implement A2A protocol or extend LangGraph with remote agent calls
4. Use Istio for mTLS, routing, and observability between agents

### Phase 4: KServe for Model Lifecycle (Week 4+)

1. Install KServe with Knative for scale-to-zero
2. Migrate vLLM deployments to InferenceService CRDs
3. Enable scale-to-zero for infrequently used agent models
4. Add ModelMesh if you need 3+ models sharing the GPU

---

## Key Constraints and Notes

| Constraint | Detail |
|-----------|--------|
| **No MIG on GB10** | Multi-Instance GPU requires A100/H100/B200. The DGX Spark GB10 does not support MIG. Use time-slicing or MPS instead. |
| **Unified Memory** | The 128GB is shared between CPU and GPU. `nvidia-smi` memory reporting may differ from discrete GPU cards. Monitor with `tegrastats` or `/proc/driver/nvidia/gpus/*/information`. |
| **ARM64 Considerations** | KServe and Knative have ARM64 images available but may require manual image overrides for some components. Test in a staging namespace first. |
| **Istio Ambient + KServe** | KServe's webhook and data plane components may need Istio sidecar exemptions (similar to your existing `ambient.istio.io/redirection: disabled` annotations on GPU pods). |
| **vLLM Prefix Caching** | Already enabled — this helps multi-agent scenarios where agents share system prompts. Repeated prefixes are cached on GPU, reducing latency for subsequent agent calls. |

---

## Quick Reference: GPU Memory Budget Planning

With 128GB unified memory and time-slicing (4 replicas):

```
Total GPU Memory:           128 GB
├── OS/Driver Overhead:      ~4 GB
├── Available:              ~124 GB
│
├── Slot 1: Primary LLM     ~83 GB  (Nemotron Nano 30B at 0.65 utilization)
├── Slot 2: Agent Model      ~30 GB  (Qwen 3B or similar)
├── Slot 3: Embedding/Other  ~14 GB  (GPU-accelerated embedding or specialty model)
└── Slot 4: Headroom         ~30 GB  (burst, experiments, new agents)
```

Adjust `--gpu-memory-utilization` on each vLLM instance to stay within budget. Time-slicing does not enforce memory limits — you must manage this yourself.

---

## Further Reading

- [NVIDIA GPU Operator: Time-Slicing](https://docs.nvidia.com/datacenter/cloud-native/gpu-operator/latest/gpu-sharing.html)
- [NVIDIA MPS Documentation](https://docs.nvidia.com/deploy/mps/index.html)
- [KServe Documentation](https://kserve.github.io/website/)
- [KServe ModelMesh](https://kserve.github.io/website/latest/modelserving/mms/modelmesh/overview/)
- [Google Agent2Agent Protocol](https://github.com/google/A2A)
- [vLLM Multi-LoRA Serving](https://docs.vllm.ai/en/latest/serving/lora.html)
- [LangGraph Multi-Agent Patterns](https://langchain-ai.github.io/langgraph/concepts/multi_agent/)
