# CNCF Projects Used in This Repository

This document catalogs all [Cloud Native Computing Foundation (CNCF)](https://www.cncf.io/) projects used in the Multi-Agent Chatbot platform. The system runs on a 17-node ARM64 K3s cluster (see [device-architecture.md](device-architecture.md) for full topology).

---

## Graduated Projects

| Project | Usage | Where |
|---------|-------|-------|
| [Kubernetes](https://kubernetes.io/) | Container orchestration via K3s v1.33.6 across 17 ARM64 nodes | `kustomize/`, `docs/device-architecture.md` |
| [Helm](https://helm.sh/) | Package management for PostgreSQL (Bitnami v13.2.24) and Milvus (v4.2.8) chart deployments | `docs/POSTGRESQL_SETUP.md`, `docs/MILVUS_SETUP.md` |
| [Istio](https://istio.io/) | Service mesh (ambient mode) with waypoint proxies, L7 routing, WebSocket affinity, timeout/retry policies | `kustomize/backend/base/istio-*.yaml`, Istiod on cube11 |
| [Cilium](https://cilium.io/) | Container Networking Interface (CNI), network policy enforcement, Hubble observability (UI + Relay) | DaemonSet on all 17 nodes, Hubble UI on cube01 |
| [Prometheus](https://prometheus.io/) | Metrics collection via Node Exporter (DaemonSet) and Pushgateway; optional KEDA scaling trigger | `kustomize/backend/base/keda-scaledobject.yaml`, DaemonSet |
| [CoreDNS](https://coredns.io/) | Cluster DNS resolution | control05 (control plane) |
| [containerd](https://containerd.io/) | Container runtime (bundled with K3s) | All 17 nodes |
| [etcd](https://etcd.io/) | Distributed key-value store for K3s control plane and Milvus metadata storage | control05, `assets/docker-compose.yml`, storage01 |
| [KEDA](https://keda.sh/) | Event-driven autoscaling for backend pods (1-5 replicas) based on CPU, memory, and Prometheus metrics | `kustomize/backend/base/keda-scaledobject.yaml`, cube04 (operator), cube07 (metrics API) |

## Incubating Projects

| Project | Usage | Where |
|---------|-------|-------|
| [Backstage](https://backstage.io/) | Developer portal and service catalog for system/component/API metadata | `catalog-info.yaml`, storage02 |
| [cert-manager](https://cert-manager.io/) | TLS/SSL certificate management for Kubernetes | storage01 |
| [Longhorn](https://longhorn.io/) | Distributed block storage with CSI driver | DaemonSet (Manager + CSI Plugin) on all nodes, UI on cube05 |
| [gRPC](https://grpc.io/) | High-performance RPC framework used by Milvus (port 19530) and inter-service communication | Milvus data/proxy nodes |

## Sandbox Projects

| Project | Usage | Where |
|---------|-------|-------|
| [External Secrets Operator](https://external-secrets.io/) | Syncs secrets from Azure Key Vault to Kubernetes via ClusterSecretStore | `kustomize/*/base/*-external-secret.yaml`, storage03 |
| [Strimzi](https://strimzi.io/) | Kafka operator for managing Redpanda/Kafka brokers on Kubernetes | cube08 (operator) |
| [KAITO](https://github.com/kaito-project/kaito) | Kubernetes AI Toolchain Operator for AI model workload management | cube07 (workspace), DaemonSet (CSI local node) |

## Kubernetes Sub-Projects (under CNCF umbrella)

| Project | Usage | Where |
|---------|-------|-------|
| [Kustomize](https://kustomize.io/) | Kubernetes manifest templating with base/overlay pattern for dev and prod environments | `kustomize/` directory (30+ YAML files) |
| [Metrics Server](https://github.com/kubernetes-sigs/metrics-server) | Cluster-wide resource usage metrics for HPA and KEDA | cube02 |
| [Node Feature Discovery](https://github.com/kubernetes-sigs/node-feature-discovery) | Hardware feature detection for GPU and node capability labeling | cube08 (NFD Master), DaemonSet (NFD Worker) |

---

## Summary

| Maturity Level | Count | Projects |
|----------------|-------|----------|
| Graduated | 9 | Kubernetes, Helm, Istio, Cilium, Prometheus, CoreDNS, containerd, etcd, KEDA |
| Incubating | 4 | Backstage, cert-manager, Longhorn, gRPC |
| Sandbox | 3 | External Secrets Operator, Strimzi, KAITO |
| K8s Sub-Projects | 3 | Kustomize, Metrics Server, Node Feature Discovery |
| **Total** | **19** | |

## Non-CNCF Notable Dependencies

The following key infrastructure components are used alongside CNCF projects but are **not** CNCF projects themselves:

| Project | Foundation / Company | Usage |
|---------|---------------------|-------|
| Grafana, Loki, Mimir, Tempo, Alloy | Grafana Labs | Observability stack (metrics, logs, traces, telemetry) |
| Milvus | LF AI & Data Foundation | Vector database for RAG embeddings |
| MinIO | MinIO Inc. | S3-compatible object storage for Milvus |
| Redpanda | Redpanda Data | Kafka-compatible event streaming (3-broker cluster) |
| PostgreSQL | PostgreSQL Global Dev Group | Relational database for conversation history |
| Apache Flink | Apache Software Foundation | Stream processing operators |
