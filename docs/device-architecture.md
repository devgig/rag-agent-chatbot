# Multi-Agent Chatbot — Device Architecture

> Nodes used by the multi-agent-chatbot in K8s (subset of the 17-node K3s v1.33.6 ARM64 cluster on `192.168.68.x`)

## Nodes Overview

The multi-agent-chatbot runs across **8 nodes** spanning all 4 hardware tiers:

| Node | Tier | Hardware | CPU | RAM | Chatbot Role |
|------|------|----------|-----|-----|--------------|
| control05 | Control Plane | Raspberry Pi 16GB | 4 | 16 GB | K3s API / CoreDNS |
| cube01 | Compute | Raspberry Pi 8GB | 4 | 8 GB | Frontend |
| cube02 | Compute | Raspberry Pi 8GB | 4 | 8 GB | PostgreSQL (chatbot DB) |
| cube04 | Compute | Raspberry Pi 8GB | 4 | 8 GB | KEDA Operator (autoscaling) |
| cube06 | Compute | Raspberry Pi 8GB | 4 | 8 GB | Milvus DataNode (RAG vectors) |
| spark-7eb5 | GPU | NVIDIA DGX Spark | 20 | 128 GB | GPT-OSS-120B (LLM inference) |
| storage01 | Storage | Rockchip SBC 16GB | 8 | 16 GB | Backend + Milvus etcd/Proxy |
| storage02 | Storage | Rockchip SBC 16GB | 8 | 16 GB | Qwen3 Embedding + Cloudflared ingress |

## Device Architecture

```mermaid
graph TB
    subgraph NET["<b>Network: 192.168.68.x</b><br/>K3s v1.33.6 | Cilium CNI | Istio Ambient Mesh"]

        subgraph CP["<b>CONTROL PLANE</b>"]
            subgraph control05["<b>control05</b><br/>Raspberry Pi | 4 CPU | 16 GB<br/>192.168.68.102"]
                cp_k3s["K3s Server + etcd"]
                cp_dns["CoreDNS"]
            end
        end

        subgraph GPU_TIER["<b>GPU TIER</b>"]
            subgraph spark["<b>spark-7eb5</b><br/>NVIDIA DGX Spark | 20 CPU | 128 GB | Blackwell GB10 GPU<br/>192.168.68.94 | CUDA 13.0 | Driver 580.95"]
                spark_gpt["GPT-OSS-120B<br/>(LLM — Qwen2.5-VL-7B via vLLM)"]
                spark_nvidia["NVIDIA Container Toolkit<br/>+ Device Plugin"]
            end
        end

        subgraph COMPUTE["<b>COMPUTE WORKERS</b>"]
            direction LR
            subgraph cube01["<b>cube01</b><br/>.88 | 4C 8G"]
                c01_1["Multi-Agent Frontend<br/>(React + Vite + nginx)"]
            end
            subgraph cube02["<b>cube02</b><br/>.89 | 4C 8G"]
                c02_1["PostgreSQL<br/>(chatbot DB)"]
            end
            subgraph cube04["<b>cube04</b><br/>.52 | 4C 8G"]
                c04_1["KEDA Operator<br/>(backend autoscaling)"]
            end
            subgraph cube06["<b>cube06</b><br/>.80 | 4C 8G"]
                c06_1["Milvus DataNode<br/>(RAG vector storage)"]
            end
        end

        subgraph STORAGE["<b>STORAGE WORKERS</b>"]
            direction LR
            subgraph storage01["<b>storage01</b><br/>.105 | 8C 16G"]
                s01_1["Multi-Agent Backend<br/>(FastAPI + LangGraph)"]
                s01_2["Milvus etcd + Proxy"]
            end
            subgraph storage02["<b>storage02</b><br/>.104 | 8C 16G"]
                s02_1["Qwen3 Embedding<br/>(all-MiniLM-L6-v2)"]
                s02_2["Cloudflared Tunnel<br/>(external ingress)"]
            end
        end
    end

    %% Data flow connections
    c01_1 -->|"HTTP / WebSocket"| s01_1
    s01_1 -->|"OpenAI API"| spark_gpt
    s01_1 -->|"SQL"| c02_1
    s01_1 -->|"gRPC :19530"| s01_2
    s01_2 --- c06_1
    s01_1 -->|"/v1/embeddings"| s02_1
    s02_2 -.->|"tunnel"| c01_1
    c04_1 -.->|"scale 1–5 replicas"| s01_1

    %% Styling
    classDef controlPlane fill:#4a90d9,stroke:#2c5aa0,color:#fff
    classDef gpuNode fill:#76b947,stroke:#4a8c1c,color:#fff
    classDef computeNode fill:#f5a623,stroke:#c77d0a,color:#fff
    classDef storageNode fill:#9b59b6,stroke:#7d3c98,color:#fff

    class control05 controlPlane
    class spark gpuNode
    class cube01,cube02,cube04,cube06 computeNode
    class storage01,storage02 storageNode
```

## Service Connectivity

```mermaid
graph LR
    USER["User"] -->|"HTTPS"| CF["Cloudflared<br/>(storage02)"]
    CF -->|":3000"| FE["Frontend<br/>(cube01)"]
    FE -->|"HTTP :8000<br/>WebSocket /ws"| BE["Backend<br/>(storage01)"]

    BE -->|"OpenAI API :8000"| LLM["GPT-OSS-120B<br/>(spark-7eb5)"]
    BE -->|"/v1/embeddings :8000"| EMB["Qwen3 Embedding<br/>(storage02)"]
    BE -->|":5432"| PG["PostgreSQL<br/>(cube02)"]
    BE -->|":19530"| MV["Milvus Proxy<br/>(storage01)"]

    MV --- ETCD["Milvus etcd<br/>(storage01)"]
    MV --- DN["Milvus DataNode<br/>(cube06)"]

    subgraph MESH["Istio Ambient Mesh"]
        FE
        BE
    end

    KEDA["KEDA<br/>(cube04)"] -.->|"scale 1–5"| BE

    classDef user fill:#e74c3c,stroke:#c0392b,color:#fff
    classDef app fill:#3498db,stroke:#2980b9,color:#fff
    classDef data fill:#9b59b6,stroke:#7d3c98,color:#fff
    classDef ai fill:#76b947,stroke:#4a8c1c,color:#fff
    classDef infra fill:#95a5a6,stroke:#7f8c8d,color:#fff

    class USER user
    class FE,BE app
    class PG,MV,ETCD,DN data
    class LLM,EMB ai
    class CF,KEDA infra
```

## Key Configuration

| Component | Namespace | Service DNS | Port |
|-----------|-----------|-------------|------|
| Frontend | multi-agent-dev | multi-agent-frontend.multi-agent-dev.svc.cluster.local | 3000 |
| Backend | multi-agent-dev | multi-agent-backend.multi-agent-dev.svc.cluster.local | 8000 |
| GPT-OSS-120B | multi-agent-dev | gpt-oss-120b.multi-agent-dev.svc.cluster.local | 8000 |
| Qwen3 Embedding | multi-agent-dev | qwen3-embedding.multi-agent-dev.svc.cluster.local | 8000 |
| PostgreSQL | postgres-system | postgresql.postgres-system.svc.cluster.local | 5432 |
| Milvus | milvus-system | milvus.milvus-system.svc.cluster.local | 19530 |

### Backend Autoscaling (KEDA)

- **Min/Max Replicas:** 1–5
- **Triggers:** CPU > 70%, Memory > 80%
- **Scale Up:** +2 pods per 30s
- **Scale Down:** -25% per 60s (120s stabilization)

### Istio Routing

- **WebSocket** (`/ws`): No timeout, 0 retries (client reconnects)
- **HTTP** (`/`): 300s timeout, 2 retries on 5xx/reset/connect-failure
- **Load Balancing:** Consistent hash on `chatId` query param (session affinity)
