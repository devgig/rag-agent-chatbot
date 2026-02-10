# Device Architecture Diagram

> Auto-generated from live kubectl inspection on 2026-02-09

## Cluster Overview

**K3s v1.33.6** cluster running on **17 ARM64 nodes** across 4 hardware tiers on the `192.168.68.x` network.

| Tier | Nodes | Hardware | CPU | RAM | Kernel |
|------|-------|----------|-----|-----|--------|
| Control Plane | 1 | Raspberry Pi (16GB) | 4 | 16 GB | `raspi` |
| Compute Workers | 12 | Raspberry Pi (8GB) | 4 | 8 GB | `raspi` |
| GPU | 1 | NVIDIA DGX Spark (Blackwell GB10) | 20 | 128 GB | `nvidia` |
| Storage Workers | 3 | Rockchip SBC | 8 | 16 GB | `rockchip` |

**Totals:** 17 nodes, 96 CPU cores, 336 GB RAM, 1x NVIDIA Blackwell GB10 GPU

## Device Architecture

```mermaid
graph TB
    subgraph NET["<b>Network: 192.168.68.x</b><br/>K3s v1.33.6 | Cilium CNI | Istio Ambient Mesh"]

        subgraph CP["<b>CONTROL PLANE</b>"]
            subgraph control05["<b>control05</b><br/>Raspberry Pi | 4 CPU | 16 GB<br/>192.168.68.102"]
                cp_k3s["K3s Server + etcd"]
                cp_dns["CoreDNS"]
                cp_cilium["Cilium"]
                cp_istio["Istio CNI + ztunnel"]
            end
        end

        subgraph GPU_TIER["<b>GPU TIER</b>"]
            subgraph spark["<b>spark-7eb5</b><br/>NVIDIA DGX Spark | 20 CPU | 128 GB | Blackwell GB10 GPU<br/>192.168.68.94 | CUDA 13.0 | Driver 580.95"]
                spark_gpt["GPT-OSS-120B<br/>(AI Model Serving)"]
                spark_nvidia["NVIDIA Container Toolkit<br/>+ Device Plugin"]
                spark_cilium_op["Cilium Operator"]
            end
        end

        subgraph COMPUTE["<b>COMPUTE WORKERS &mdash; Raspberry Pi 8GB Cluster</b>"]
            subgraph ROW1[" "]
                direction LR
                subgraph cube01["<b>cube01</b><br/>.88 | 4C 8G"]
                    c01_1["Multi-Agent Frontend"]
                    c01_2["Flink Operator"]
                    c01_3["KEDA Webhooks"]
                    c01_4["Hubble UI"]
                    c01_5["ByteCourier UI (dev)"]
                end
                subgraph cube02["<b>cube02</b><br/>.89 | 4C 8G"]
                    c02_1["PostgreSQL"]
                    c02_2["Flink Operator"]
                    c02_3["Metrics Server"]
                    c02_4["Loki Gateway"]
                    c02_5["Mimir Nginx"]
                    c02_6["Unleash"]
                end
                subgraph cube03["<b>cube03</b><br/>.91 | 4C 8G"]
                    c03_1["Hubble Relay"]
                    c03_2["Redpanda Console"]
                    c03_3["Loki Distributor + Querier"]
                    c03_4["Mimir Querier"]
                    c03_5["Tempo Ingester"]
                end
                subgraph cube04["<b>cube04</b><br/>.52 | 4C 8G"]
                    c04_1["Portainer"]
                    c04_2["KEDA Operator"]
                    c04_3["Redpanda Broker"]
                    c04_4["Loki Query Frontend"]
                    c04_5["Tempo Distributor"]
                end
            end

            subgraph ROW2[" "]
                direction LR
                subgraph cube05["<b>cube05</b><br/>.73 | 4C 8G"]
                    c05_1["Mimir Alertmanager"]
                    c05_2["Mimir Compactor"]
                    c05_3["Mimir Distributor"]
                    c05_4["Loki Ingester Zone C"]
                    c05_5["Longhorn UI"]
                end
                subgraph cube06["<b>cube06</b><br/>.80 | 4C 8G"]
                    c06_1["ByteCourier Gateway"]
                    c06_2["Milvus DataNode"]
                    c06_3["Mimir Ruler"]
                    c06_4["Loki Index Gateway"]
                    c06_5["Today Mechanic UI (dev)"]
                end
                subgraph cube07["<b>cube07</b><br/>.96 | 4C 8G"]
                    c07_1["KAITO Workspace"]
                    c07_2["DevLake UI"]
                    c07_3["KEDA Metrics API"]
                    c07_4["Loki Ingester Zone A"]
                    c07_5["Today Mechanic UI"]
                end
                subgraph cube08["<b>cube08</b><br/>.92 | 4C 8G"]
                    c08_1["ByteCourier UI + Waypoint"]
                    c08_2["MinIO Broker"]
                    c08_3["Strimzi Operator"]
                    c08_4["NFD Master"]
                    c08_5["Tempo Compactor"]
                end
            end

            subgraph ROW3[" "]
                direction LR
                subgraph cube09["<b>cube09</b><br/>.99 | 4C 8G"]
                    c09_1["Mimir Compactor"]
                    c09_2["Mimir Querier"]
                    c09_3["Loki Results Cache"]
                end
                subgraph cube10["<b>cube10</b><br/>.70 | 4C 8G"]
                    c10_1["Loki Compactor"]
                    c10_2["Loki Ingester Zone B"]
                    c10_3["Mimir Store Gateway"]
                end
                subgraph cube11["<b>cube11</b><br/>.77 | 4C 8G"]
                    c11_1["Istiod"]
                    c11_2["Grafana"]
                    c11_3["Mimir Query Frontend"]
                    c11_4["ByteCourier UI"]
                    c11_5["Tempo Querier"]
                end
                subgraph cube12["<b>cube12</b><br/>.90 | 4C 8G"]
                    c12_1["Mimir Ingester Zone C"]
                    c12_2["Mimir Distributor"]
                    c12_3["Longhorn Driver"]
                    c12_4["Tempo Memcached"]
                end
            end
        end

        subgraph STORAGE["<b>STORAGE WORKERS &mdash; Rockchip SBC 16GB</b>"]
            direction LR
            subgraph storage01["<b>storage01</b><br/>.105 | 8C 16G"]
                s01_1["MySQL"]
                s01_2["Milvus etcd + Proxy"]
                s01_3["Multi-Agent Backend"]
                s01_4["Cert Manager"]
                s01_5["NFS Provisioner"]
                s01_6["GPU Operator"]
                s01_7["Redpanda Connectors"]
                s01_8["Mimir Store Gateway A"]
            end
            subgraph storage02["<b>storage02</b><br/>.104 | 8C 16G"]
                s02_1["Backstage"]
                s02_2["Qwen3 Embedding"]
                s02_3["Cloudflared Tunnel"]
                s02_4["NFS Provisioner"]
                s02_5["Unleash"]
                s02_6["Mimir Ingester Zone A"]
                s02_7["Mimir Alertmanager"]
                s02_8["Redpanda Broker"]
            end
            subgraph storage03["<b>storage03</b><br/>.79 | 8C 16G"]
                s03_1["Azure DevOps Agent"]
                s03_2["Cloudflared Tunnel"]
                s03_3["External Secrets"]
                s03_4["MinIO Broker"]
                s03_5["Redpanda Broker"]
                s03_6["Loki Chunks Cache"]
                s03_7["Mimir Ingester Zone B"]
            end
        end
    end

    %% Styling
    classDef controlPlane fill:#4a90d9,stroke:#2c5aa0,color:#fff
    classDef gpuNode fill:#76b947,stroke:#4a8c1c,color:#fff
    classDef computeNode fill:#f5a623,stroke:#c77d0a,color:#fff
    classDef storageNode fill:#9b59b6,stroke:#7d3c98,color:#fff
    classDef workload fill:#ecf0f1,stroke:#bdc3c7,color:#2c3e50

    class control05 controlPlane
    class spark gpuNode
    class cube01,cube02,cube03,cube04,cube05,cube06,cube07,cube08,cube09,cube10,cube11,cube12 computeNode
    class storage01,storage02,storage03 storageNode
```

## Platform Services Distributed Across Nodes

```mermaid
graph LR
    subgraph DAEMONSETS["<b>DaemonSets (all 17 nodes)</b>"]
        direction TB
        DS1["Cilium CNI"]
        DS2["Istio CNI + ztunnel"]
        DS3["Longhorn Manager + CSI Plugin"]
        DS4["Grafana Alloy (telemetry)"]
        DS5["Loki Canary"]
        DS6["Prometheus Node Exporter"]
        DS7["KAITO CSI Local Node"]
        DS8["GPU Operator NFD Worker"]
    end

    subgraph NETWORKING["<b>Service Mesh & Ingress</b>"]
        direction TB
        N1["Istiod (cube11)"]
        N2["Istio Gateways<br/>ByteCourier | Today | Multi-Agent"]
        N3["Cloudflared Tunnels<br/>(storage02, storage03)"]
    end

    subgraph DATA["<b>Data Platform</b>"]
        direction TB
        D1["Redpanda (3-broker cluster)<br/>storage03, storage02, cube04"]
        D2["PostgreSQL (cube02)"]
        D3["MySQL (storage01)"]
        D4["MinIO (4-node)<br/>storage03, storage01, storage02, cube08"]
        D5["Milvus Vector DB<br/>cube02, cube06, cube07, storage01"]
    end

    subgraph OBSERVABILITY["<b>Observability Stack</b>"]
        direction TB
        O1["Grafana (cube11)"]
        O2["Mimir (metrics)<br/>distributed across compute"]
        O3["Loki (logs)<br/>distributed across compute"]
        O4["Tempo (traces)<br/>distributed across compute"]
        O5["Prometheus + Pushgateway"]
    end

    subgraph AI_ML["<b>AI / ML</b>"]
        direction TB
        AI1["GPT-OSS-120B (spark-7eb5)<br/>NVIDIA Blackwell GB10"]
        AI2["Qwen3 Embedding (storage02)"]
        AI3["KAITO Workspace (cube07)"]
        AI4["Flink Operators<br/>(cube01, cube02)"]
    end

    subgraph APPS["<b>Applications</b>"]
        direction TB
        A1["Multi-Agent Chatbot<br/>Frontend (cube01) + Backend (storage01)"]
        A2["ByteCourier UI<br/>prod (cube08, cube11) + dev (cube01)"]
        A3["Today Mechanic UI<br/>prod (cube02, cube07) + dev (cube06)"]
        A4["Backstage (storage02)"]
        A5["Portainer (cube04)"]
        A6["Unleash Feature Flags<br/>(storage02, cube02)"]
    end
```

## Physical Topology

```mermaid
graph TB
    ROUTER["Network Router / Switch<br/>192.168.68.x"]

    ROUTER --- CP_GROUP
    ROUTER --- PI_GROUP
    ROUTER --- GPU_GROUP
    ROUTER --- STORAGE_GROUP

    subgraph CP_GROUP["Control Plane"]
        CP["control05<br/>Raspberry Pi 16GB<br/>.102"]
    end

    subgraph PI_GROUP["Raspberry Pi Compute Cluster (12x Pi 8GB)"]
        direction LR
        PI1["cube01<br/>.88"]
        PI2["cube02<br/>.89"]
        PI3["cube03<br/>.91"]
        PI4["cube04<br/>.52"]
        PI5["cube05<br/>.73"]
        PI6["cube06<br/>.80"]
        PI7["cube07<br/>.96"]
        PI8["cube08<br/>.92"]
        PI9["cube09<br/>.99"]
        PI10["cube10<br/>.70"]
        PI11["cube11<br/>.77"]
        PI12["cube12<br/>.90"]
    end

    subgraph GPU_GROUP["GPU Compute"]
        GPU["spark-7eb5<br/>NVIDIA DGX Spark<br/>Blackwell GB10 GPU<br/>20 CPU | 128 GB RAM<br/>.94"]
    end

    subgraph STORAGE_GROUP["Storage Tier (3x Rockchip SBC 16GB)"]
        direction LR
        ST1["storage01<br/>.105"]
        ST2["storage02<br/>.104"]
        ST3["storage03<br/>.79"]
    end

    classDef router fill:#e74c3c,stroke:#c0392b,color:#fff
    classDef control fill:#4a90d9,stroke:#2c5aa0,color:#fff
    classDef compute fill:#f5a623,stroke:#c77d0a,color:#fff
    classDef gpu fill:#76b947,stroke:#4a8c1c,color:#fff
    classDef storage fill:#9b59b6,stroke:#7d3c98,color:#fff

    class ROUTER router
    class CP control
    class PI1,PI2,PI3,PI4,PI5,PI6,PI7,PI8,PI9,PI10,PI11,PI12 compute
    class GPU gpu
    class ST1,ST2,ST3 storage
```
