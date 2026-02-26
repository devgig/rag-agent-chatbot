# CNCF Projects Used in This Repository

This document catalogs all [Cloud Native Computing Foundation (CNCF)](https://www.cncf.io/) projects that have configuration, manifests, or service definitions in this repository.

---

## Graduated Projects

| Project | Usage | Where |
|---------|-------|-------|
| [Kubernetes](https://kubernetes.io/) | Target deployment platform; all manifests define K8s resources | `kustomize/` |
| [Istio](https://istio.io/) | Service mesh (ambient mode) with waypoint proxies for L7 routing, WebSocket affinity, timeout/retry policies, and JWT enforcement at the ingress gateway via RequestAuthentication + AuthorizationPolicy | `kustomize/gateway/base/istio-*.yaml`, `kustomize/backend/base/istio-waypoint.yaml`, `istio-destination-rule.yaml`, `istio-virtual-service.yaml` |
| [etcd](https://etcd.io/) | Distributed key-value store for Milvus metadata | Milvus dependency in K8s cluster |
| [KEDA](https://keda.sh/) | Event-driven autoscaling for backend pods (1-5 replicas) based on CPU, memory, and optional Prometheus metrics | `kustomize/backend/base/keda-scaledobject.yaml` |

## Incubating Projects

| Project | Usage | Where |
|---------|-------|-------|
| [Backstage](https://backstage.io/) | Developer portal service catalog with system, component, and API metadata | `catalog-info.yaml` |

## Sandbox Projects

| Project | Usage | Where |
|---------|-------|-------|
| [External Secrets Operator](https://external-secrets.io/) | Syncs secrets from Azure Key Vault to Kubernetes via ClusterSecretStore | `kustomize/frontend/base/acr-external-secret.yaml`, `kustomize/backend/base/acr-external-secret.yaml`, `kustomize/backend/base/postgres-external-secret.yaml`, `kustomize/models/base/hf-external-secret.yaml` |
| [KAITO](https://github.com/kaito-project/kaito) | Kubernetes AI Toolchain Operator for GPU model inference workloads | `kustomize/models/base/kaito-workspace.yaml`, `kustomize/models/base/kaito-service.yaml` |

## Kubernetes Sub-Projects (under CNCF umbrella)

| Project | Usage | Where |
|---------|-------|-------|
| [Kustomize](https://kustomize.io/) | Kubernetes manifest templating with base/overlay pattern for dev and prod environments | `kustomize/` directory (30+ YAML files) |

---

## Summary

| Maturity Level | Count | Projects |
|----------------|-------|----------|
| Graduated | 4 | Kubernetes, Istio, etcd, KEDA |
| Incubating | 1 | Backstage |
| Sandbox | 2 | External Secrets Operator, KAITO |
| K8s Sub-Projects | 1 | Kustomize |
| **Total** | **8** | |
