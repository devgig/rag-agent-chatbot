# Milvus Setup Guide

Milvus runs as a **standalone** instance in the `milvus-system` namespace. It stores document embeddings for RAG retrieval.

## Architecture

```
┌─────────────────────────────────────────────────────┐
│ Backend (rag-agent-backend)                         │
│ MILVUS_ADDRESS=tcp://milvus....:19530               │
└────────────────┬────────────────────────────────────┘
                 │
                 ▼
┌─────────────────────────────────────────────────────┐
│ Service: milvus (ClusterIP:19530)                   │
│ Selector: component=standalone                      │
└────────────────┬────────────────────────────────────┘
                 │
                 ▼
┌─────────────────────────────────────────────────────┐
│ Deployment: milvus-standalone                       │
│ - Image: milvusdb/milvus:v2.6.7                    │
│ - Mode: standalone (embedded RocksMQ)               │
│ - Metadata: etcd (milvus-etcd-0)                   │
│ - Storage: MinIO (minio-system)                     │
│ - Collection: context (auto-created)                │
└─────────────────────────────────────────────────────┘
```

## Why Standalone Mode

The cluster mode with Woodpecker message queue caused persistent **streamingnode crash-loops** on ARM64/K3s due to gRPC channel-assignment resolver failures in Milvus v2.6.7. Standalone mode:

- Uses embedded RocksMQ — no streamingnode, no WAL coordination issues
- Runs as a single pod (~2-4 GB) vs 6-7 pods (~6-8 GB) in cluster mode
- Sufficient for dev RAG workloads (moderate query volume, occasional ingestion)
- Eliminates the Woodpecker dependency entirely

If a future Milvus release fixes the streamingnode issue, cluster mode can be re-enabled by setting `cluster.enabled: true` and `woodpecker.enabled: true` in the Helm values.

## Helm Chart Configuration

Deployed via Helm chart with Kustomize in the `K8sInfrastructure` repo:

```yaml
# K8sInfrastructure/kustomize/milvus-system/overlays/dev/kustomization.yaml
helmCharts:
  - name: milvus
    repo: https://zilliztech.github.io/milvus-helm/
    version: "5.0.10"
    releaseName: milvus
    namespace: milvus-system
    valuesFile: values.yaml
```

Key values:

```yaml
# Standalone mode with embedded RocksMQ
cluster:
  enabled: false
standalone:
  enabled: true
  persistence:
    enabled: true
    persistentVolumeClaim:
      size: 50Gi
      accessModes: ReadWriteOnce
      # Uses default storageClass (longhorn)
woodpecker:
  enabled: false

# External MinIO for object storage
externalS3:
  enabled: true
  host: "minio.minio-system.svc.cluster.local"
  port: "9000"
  bucketName: "milvus"
  rootPath: "milvus-dev"

# etcd for metadata
etcd:
  enabled: true
  replicaCount: 1
  persistence:
    size: 10Gi

# Server-side default index for new collections
extraConfigFiles:
  user.yaml: |+
    autoIndex:
      params:
        build: '{"index_type": "HNSW", "metric_type": "COSINE", "M": 16, "efConstruction": 256}'
        search: '{"ef": 64}'
```

## Connection Details

- **Host**: `milvus.milvus-system.svc.cluster.local`
- **Port**: 19530
- **Protocol**: TCP (pymilvus gRPC)
- **Collection**: `context` (auto-created by langchain-milvus on first document ingest)

Backend environment variable:

```yaml
- name: MILVUS_ADDRESS
  value: "tcp://milvus.milvus-system.svc.cluster.local:19530"
```

## Collection Schema

The `context` collection is auto-created by `langchain-milvus` with HNSW/COSINE indexing:

| Field | Type | Description |
|-------|------|-------------|
| `pk` | INT64 | Auto-generated primary key |
| `vector` | FLOAT_VECTOR (384-dim) | Document chunk embedding (HNSW index, COSINE metric) |
| `text` | VARCHAR | Text chunk content |
| `source` | VARCHAR | Document filename (used for filtering/deletion) |
| `file_path` | VARCHAR | Original file path |
| `filename` | VARCHAR | Original filename |

The vector index is configured both client-side (`vector_store.py`) and server-side (`autoIndex` in Milvus values.yaml):
- **Index type**: HNSW (M=16, efConstruction=256)
- **Metric**: COSINE (scores normalized to [0, 1])
- **Search params**: ef=64

## Verification

```bash
# Check pods
kubectl get pods -n milvus-system

# List collections via REST API
kubectl exec -n milvus-system -l component=standalone -- \
  curl -s -X POST http://localhost:19530/v2/vectordb/collections/list \
  -H "Content-Type: application/json" -d '{}'

# Check service endpoints
kubectl get endpoints milvus -n milvus-system

# Test from backend pod
BACKEND_POD=$(kubectl get pods -n rag-agent -l app=rag-agent-backend -o jsonpath='{.items[0].metadata.name}')
kubectl exec -n rag-agent $BACKEND_POD -c backend -- \
  /app/.venv/bin/python -c "from pymilvus import connections, utility; connections.connect(uri='tcp://milvus.milvus-system.svc.cluster.local:19530'); print(utility.list_collections())"
```

## Troubleshooting

### Document Ingestion Hangs

If document uploads don't complete:

1. Check Milvus standalone pod is running and ready
2. Check embedding service is healthy: `kubectl get pods -n rag-agent -l app=qwen3-embedding`
3. Check backend logs: `kubectl logs -n rag-agent -l app=rag-agent-backend -c backend --tail=50`

### Resetting Milvus

To clear all collections and start fresh:

```bash
# Clear etcd metadata (removes all collections)
kubectl exec -n milvus-system milvus-etcd-0 -- etcdctl del --prefix "by-dev/"

# Restart standalone
kubectl rollout restart deployment milvus-standalone -n milvus-system
```

### Connection Refused

1. Verify standalone pod is running: `kubectl get pods -n milvus-system`
2. Verify service selector matches: `kubectl get svc milvus -n milvus-system -o jsonpath='{.spec.selector}'`
3. Check endpoints exist: `kubectl get endpoints milvus -n milvus-system`
