# Kubernetes Deployment with Kustomize

This directory contains Kubernetes manifests for deploying the Multi-Agent Chatbot system using Kustomize.

## Structure

```
kustomize/
├── base/                           # Base manifests (environment-agnostic)
│   ├── frontend/                   # Frontend deployment and service
│   │   ├── app/
│   │   │   ├── deployment.yaml
│   │   │   ├── image_patch.yaml
│   │   │   └── kustomization.yaml
│   │   ├── network/
│   │   │   ├── service.yaml
│   │   │   └── kustomization.yaml
│   │   └── kustomization.yaml
│   ├── backend/                    # Backend deployment and service
│   │   ├── app/
│   │   ├── network/
│   │   └── kustomization.yaml
│   ├── database/                   # Database and infrastructure components
│   │   ├── app/
│   │   │   ├── postgres-deployment.yaml
│   │   │   ├── milvus-deployment.yaml
│   │   │   ├── etcd-deployment.yaml
│   │   │   ├── minio-deployment.yaml
│   │   │   └── kustomization.yaml
│   │   ├── network/
│   │   │   ├── postgres-service.yaml
│   │   │   ├── milvus-service.yaml
│   │   │   ├── etcd-service.yaml
│   │   │   ├── minio-service.yaml
│   │   │   └── kustomization.yaml
│   │   └── kustomization.yaml
│   └── kustomization.yaml
└── overlays/                       # Environment-specific configurations
    ├── dev/                        # Development environment
    │   ├── kustomization.yaml
    │   └── dev-patches.yaml
    └── prod/                       # Production environment
        ├── kustomization.yaml
        └── prod-patches.yaml
```

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
  value: your-registry.azurecr.io/multi-agent-chatbot/frontend:v1.2.3
EOF
```

## Environment Differences

### Development (dev)
- Single replica for frontend and backend
- Lower resource limits
- Debug logging enabled
- Namespace: `multi-agent-dev`
- Name prefix: `dev-`

### Production (prod)
- 2 replicas for frontend and backend
- Higher resource limits
- Info-level logging
- Namespace: `multi-agent-prod`
- Name prefix: `prod-`
- **Note**: Update the PostgreSQL password in `kustomize/overlays/prod/kustomization.yaml` before deploying!

## Secrets Management

The base manifests reference secrets that need to be created:

```bash
# Create postgres credentials secret
kubectl create secret generic postgres-credentials \
  --from-literal=username=chatbot_user \
  --from-literal=password=YOUR_SECURE_PASSWORD \
  -n multi-agent-dev
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
kubectl get pods -n multi-agent-dev
kubectl describe pod <pod-name> -n multi-agent-dev
```

### View logs
```bash
kubectl logs <pod-name> -n multi-agent-dev
kubectl logs <pod-name> -n multi-agent-dev --previous  # Previous container instance
```

### Check services
```bash
kubectl get svc -n multi-agent-dev
```

### Port forwarding for local testing
```bash
# Frontend
kubectl port-forward svc/dev-multi-agent-frontend 3000:3000 -n multi-agent-dev

# Backend
kubectl port-forward svc/dev-multi-agent-backend 8000:8000 -n multi-agent-dev
```
