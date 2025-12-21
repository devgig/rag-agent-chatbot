# Kustomize Configuration

This directory contains Kustomize overlays for environment-specific configuration.

## Structure

```
k8s/
├── base/                          # Base configuration
│   ├── kustomization.yaml
│   ├── frontend-env-configmap.yaml
│   └── istio-httproute.yaml       # HTTPRoute for frontend ingress
├── istio-ingress/                 # Istio Gateway (deployed to istio-ingress-dev)
│   ├── kustomization.yaml
│   └── gateway.yaml
└── overlays/
    └── dev/                       # Development environment
        └── kustomization.yaml
```

## Usage

### Development Environment

Apply the dev overlay to your cluster:

```bash
kubectl apply -k k8s/overlays/dev
```

This will:
- Set `NEXT_PUBLIC_API_URL=http://backend.bytecourier.local:8000`
- Set `NEXT_PUBLIC_WS_URL=ws://backend.bytecourier.local:8000`
- Inject these environment variables into the frontend deployment

### Preview Changes

Preview the generated manifests without applying:

```bash
kubectl kustomize k8s/overlays/dev
```

### Creating New Environments

To create a new environment (e.g., production):

1. Create a new overlay directory:
   ```bash
   mkdir -p k8s/overlays/prod
   ```

2. Create `k8s/overlays/prod/kustomization.yaml`:
   ```yaml
   apiVersion: kustomize.config.k8s.io/v1beta1
   kind: Kustomization

   namespace: multi-agent-prod

   resources:
     - ../../base

   configMapGenerator:
     - name: frontend-env
       behavior: replace
       literals:
         - NEXT_PUBLIC_API_URL=https://backend.yourdomain.com
         - NEXT_PUBLIC_WS_URL=wss://backend.yourdomain.com
   ```

3. Apply:
   ```bash
   kubectl apply -k k8s/overlays/prod
   ```

## Environment Variables

### NEXT_PUBLIC_API_URL
- **Description**: Backend API URL for HTTP requests
- **Format**: `http://backend.example.com:8000` or `https://backend.example.com`
- **Required**: Yes
- **Used by**: Browser API calls (fetch)

### NEXT_PUBLIC_WS_URL
- **Description**: Backend WebSocket URL
- **Format**: `ws://backend.example.com:8000` or `wss://backend.example.com`
- **Required**: No (auto-derived from NEXT_PUBLIC_API_URL if not set)
- **Used by**: Browser WebSocket connections

## Istio Ambient Mesh Setup

The multi-agent chatbot uses Istio Ambient Mesh for ingress with WebSocket support.

### Prerequisites

1. Istio Ambient Mesh installed in the cluster
2. Gateway API CRDs installed
3. `istio-ingress-dev` namespace exists

### Deployment Order

1. **Deploy the Istio Gateway** (to `istio-ingress-dev` namespace):
   ```bash
   kubectl apply -k k8s/istio-ingress
   ```

2. **Enable Ambient Mesh on the namespace**:
   ```bash
   kubectl label namespace multi-agent-dev istio.io/dataplane-mode=ambient
   ```

3. **Deploy the application overlay** (includes HTTPRoute):
   ```bash
   kubectl apply -k k8s/overlays/dev
   ```

4. **Get the Gateway IP**:
   ```bash
   kubectl get gateway multi-agent-gateway -n istio-ingress-dev
   ```

5. **Update DNS** to point `frontend.bytecourier.local` to the Gateway IP

### Why Istio?

Cilium LoadBalancer has aggressive idle timeouts (~100ms) that terminate WebSocket
connections before the backend handshake completes. Istio's Envoy proxy has better
WebSocket support with configurable timeouts.

## Notes

- Traffic flows: Browser -> Istio Gateway -> Frontend Service -> Backend Service
- WebSocket connections are proxied through the Next.js server to the backend
- The namespace must be labeled for Istio Ambient Mesh
- Ensure your backend has CORS configured to allow requests from the frontend domain
