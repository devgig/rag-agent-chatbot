# Kustomize Configuration

This directory contains Kustomize overlays for environment-specific configuration.

## Structure

```
k8s/
├── base/                          # Base configuration
│   ├── kustomization.yaml
│   └── frontend-env-configmap.yaml
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

## Notes

- The frontend connects **directly** to the backend via external DNS names
- No Next.js API proxy is used
- Both HTTP and WebSocket traffic go directly from the browser to the backend
- Ensure your backend has CORS configured to allow requests from the frontend domain
