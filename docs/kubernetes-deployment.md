# Kubernetes Deployment with Kustomize

This directory contains Kubernetes manifests for deploying the RAG Agent Chatbot system using Kustomize.

## Structure

```
kustomize/
├── backend/                        # Backend API (rag-agent namespace)
│   ├── base/
│   │   ├── deployment.yaml         # FastAPI backend
│   │   ├── service.yaml
│   │   ├── nemotron-nano-externalname-service.yaml  # → nemotron-nano.llm.svc
│   │   └── kustomization.yaml
│   └── overlays/dev/
├── embedding/                      # Embedding service (rag-agent namespace)
│   ├── base/
│   │   ├── qwen3-embedding-deployment.yaml   # all-MiniLM-L6-v2, CPU
│   │   ├── qwen3-embedding-service.yaml
│   │   └── kustomization.yaml
│   └── overlays/dev/
├── models/                         # LLM inference (llm namespace)
│   ├── base/
│   │   ├── nemotron-nano-deployment.yaml  # Nemotron 3 Nano 30B, GPU
│   │   ├── nemotron-nano-service.yaml
│   │   ├── llm-namespace.yaml
│   │   └── kustomization.yaml
│   └── overlays/dev/
├── frontend/                       # React frontend (rag-agent namespace)
│   ├── base/
│   └── overlays/dev/
└── gateway/                        # Istio Gateway + auth policies
    ├── base/
    └── overlays/dev/
```

Each directory has its own Azure DevOps pipeline so changes deploy independently:

| Pipeline | Trigger paths | Namespace |
|----------|--------------|-----------|
| `azure-pipelines-backend.yaml` | `assets/backend/**`, `kustomize/backend/**` | `rag-agent` |
| `azure-pipelines-embedding.yaml` | `assets/embedding/**`, `kustomize/embedding/**` | `rag-agent` |
| `azure-pipelines-models.yaml` | `kustomize/models/**` | `llm` |
| `azure-pipelines-frontend.yaml` | `assets/frontend/**`, `kustomize/frontend/**` | `rag-agent` |
| `azure-pipelines-gateway.yaml` | `kustomize/gateway/**` | `istio-ingress` |

## Components

The deployment includes the following components:

### Application Services
- **Frontend**: React + Vite application (port 3000)
- **Backend**: FastAPI application (port 8000)

### Infrastructure Services
- **PostgreSQL**: Relational database (port 5432)
- **Milvus**: Vector database for embeddings (ports 19530, 9091)
- **etcd**: Distributed key-value store for Milvus (ports 2379, 2380)
- **MinIO**: Object storage for Milvus (port 9000)

## Usage

### Prerequisites
- `kubectl` installed and configured
- Access to a Kubernetes cluster
- Container registry configured (update the registry in image patches)

### Deploy to Development

```bash
# Preview the manifests
kubectl kustomize ./kustomize/overlays/dev

# Apply to cluster
kubectl apply -k ./kustomize/overlays/dev
```

### Deploy to Production

```bash
# Preview the manifests
kubectl kustomize ./kustomize/overlays/prod

# Apply to cluster
kubectl apply -k ./kustomize/overlays/prod
```

### Update Image Tags

The image tags are managed through the `image_patch.yaml` files in each component's `app/` directory. These are automatically updated by the CI/CD pipeline, but can be manually updated if needed:

```bash
cd kustomize/base/frontend/app
cat <<EOF >image_patch.yaml
- op: replace
  path: /spec/template/spec/containers/0/image
  value: your-registry.azurecr.io/rag-agent-chatbot/frontend:v1.2.3
EOF
```

## Environment Differences

### Development (dev)
- Single replica for frontend and backend
- Lower resource limits
- Debug logging enabled
- Namespace: `rag-agent`
- Name prefix: `dev-`

### Production (prod)
- 2 replicas for frontend and backend
- Higher resource limits
- Info-level logging
- Namespace: `rag-agent-prod`
- Name prefix: `prod-`
- **Note**: Update the PostgreSQL password in `kustomize/overlays/prod/kustomization.yaml` before deploying!

## Secrets Management

The base manifests reference secrets that need to be created:

```bash
# Create postgres credentials secret
kubectl create secret generic postgres-credentials \
  --from-literal=username=chatbot_user \
  --from-literal=password=YOUR_SECURE_PASSWORD \
  -n rag-agent
```

Or use the secretGenerator in the overlay's `kustomization.yaml` (already configured).

## Persistent Storage

- PostgreSQL uses a StatefulSet with PersistentVolumeClaims requesting 10Gi of storage
- Other database components use emptyDir volumes (data will be lost on pod restart)
- For production, consider adding PVCs for Milvus, etcd, and MinIO

## Monitoring and Health Checks

All deployments include:
- **Liveness probes**: Ensure containers are running and restart if unhealthy
- **Readiness probes**: Ensure containers are ready to accept traffic
- **Resource limits**: Prevent resource exhaustion

## Cleanup

```bash
# Delete dev deployment
kubectl delete -k ./kustomize/overlays/dev

# Delete prod deployment
kubectl delete -k ./kustomize/overlays/prod
```

## Troubleshooting

### Check pod status
```bash
kubectl get pods -n rag-agent
kubectl describe pod <pod-name> -n rag-agent
```

### View logs
```bash
kubectl logs <pod-name> -n rag-agent
kubectl logs <pod-name> -n rag-agent --previous  # Previous container instance
```

### Check services
```bash
kubectl get svc -n rag-agent
```

### Port forwarding for local testing
```bash
# Frontend
kubectl port-forward svc/dev-rag-agent-frontend 3000:3000 -n rag-agent

# Backend
kubectl port-forward svc/dev-rag-agent-backend 8000:8000 -n rag-agent
```
