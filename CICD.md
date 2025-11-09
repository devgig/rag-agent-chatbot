# CI/CD Pipeline Documentation

This document describes the Azure Pipelines configuration for the Multi-Agent Chatbot project.

## Pipeline Files

Three Azure Pipeline configurations are provided:

1. **azure-pipelines.yaml** - Main pipeline that builds and tests all components
2. **azure-pipelines-frontend.yaml** - Frontend-specific pipeline
3. **azure-pipelines-backend.yaml** - Backend-specific pipeline

## Main Pipeline (azure-pipelines.yaml)

This is a comprehensive pipeline that handles all components in a single workflow.

### Stages

#### 1. BuildAndTestAll
Runs tests and builds for both frontend and backend in parallel.

**Frontend Job:**
- Install Node.js 20.x
- Install dependencies with `npm ci`
- Run linting
- Run tests
- Build the application

**Backend Job:**
- Install Python 3.12
- Install uv package manager
- Install dependencies with `uv sync`
- Run Ruff linting
- Run pytest tests
- Publish test results

#### 2. BuildImages
Builds and pushes Docker images to Azure Container Registry.

**Prerequisites:**
- Only runs on main branch
- Only runs for CI/Manual builds
- Requires BuildAndTestAll stage to succeed

**Jobs:**
- Build and push frontend Docker image
- Build and push backend Docker image

#### 3. PublishManifests
Generates Kubernetes manifests using Kustomize.

**Outputs:**
- `manifests/dev-workloads.yaml` - Development environment manifests
- `manifests/prod-workloads.yaml` - Production environment manifests

## Component-Specific Pipelines

### Frontend Pipeline (azure-pipelines-frontend.yaml)

**Triggers:**
- Changes to `assets/frontend/*`
- Changes to `kustomize/base/frontend/*`

**Stages:**
1. Build and test frontend
2. Build and push Docker image
3. Generate and publish Kustomize manifests

### Backend Pipeline (azure-pipelines-backend.yaml)

**Triggers:**
- Changes to `assets/backend/*`
- Changes to `kustomize/base/backend/*`

**Stages:**
1. Build and test backend
2. Build and push Docker image
3. Generate and publish Kustomize manifests

## Configuration

### Parameters

All pipelines accept the following parameters:

| Parameter | Default | Description |
|-----------|---------|-------------|
| poolName | 'Default' | Azure DevOps agent pool name |
| containerRegistry | 'your-registry.azurecr.io' | ACR registry URL |
| tag | '$(Build.BuildId)' | Docker image tag |

Component-specific pipelines also have:
- `imageRepository` - Docker image repository name
- `dockerfilePath` - Path to Dockerfile
- `manifestPath` - Path to Kustomize manifests

### Required Setup

Before using these pipelines, configure the following in Azure DevOps:

1. **Service Connection**
   - Create a Docker Registry service connection named matching your `containerRegistry` parameter
   - Grant it push permissions to your ACR

2. **Agent Pool**
   - Ensure the specified pool exists and has agents available
   - Agents must have Docker installed
   - Agents must have kubectl installed (for manifest generation)

3. **Variable Groups** (Optional)
   - Create a variable group with registry settings
   - Link it to the pipeline for centralized configuration

4. **Update Parameters**
   ```yaml
   # Update in each pipeline file
   parameters:
     - name: containerRegistry
       type: string
       default: 'YOUR-REGISTRY.azurecr.io'  # Change this!
   ```

## Usage

### Using the Main Pipeline

Best for:
- Full system deployments
- Initial setup
- Testing all components together

```bash
# Trigger manually or push to main branch
git push origin main
```

### Using Component Pipelines

Best for:
- Rapid iteration on a single component
- Separate deployment cycles
- Microservice-style development

```bash
# Only triggers if frontend files changed
git add assets/frontend/
git commit -m "Update frontend"
git push
```

## Pipeline Workflow

```
┌─────────────────────────────────────────────────────────────┐
│ 1. Code Push to main branch                                 │
└─────────────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────────┐
│ 2. BuildAndTestAll Stage                                    │
│    ├─ Frontend: npm ci → lint → test → build                │
│    └─ Backend: uv sync → ruff → pytest                      │
└─────────────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────────┐
│ 3. BuildImages Stage (only if tests pass)                   │
│    ├─ Build frontend Docker image                           │
│    └─ Build backend Docker image                            │
│    Push both to ACR with tags: <BuildId> and latest         │
└─────────────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────────┐
│ 4. PublishManifests Stage                                   │
│    ├─ Update image_patch.yaml with new image tags           │
│    ├─ Generate dev manifests: kubectl kustomize overlays/dev│
│    ├─ Generate prod manifests: kubectl kustomize overlays/prod
│    └─ Publish as pipeline artifacts                         │
└─────────────────────────────────────────────────────────────┘
```

## Artifacts

Each pipeline run produces:

- **Docker Images**: Pushed to ACR with tags `<BuildId>` and `latest`
- **Kubernetes Manifests**: Available as build artifacts
  - Download from Azure DevOps UI
  - Use for ArgoCD or manual deployment

## Deployment Options

### Option 1: Manual Deployment (kubectl)

```bash
# Download manifests artifact from pipeline run
# Extract and apply
kubectl apply -f dev-workloads.yaml
```

### Option 2: ArgoCD Integration

```yaml
# argocd-application.yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: multi-agent-chatbot-dev
spec:
  source:
    repoURL: https://your-repo.git
    targetRevision: main
    path: kustomize/overlays/dev
  destination:
    server: https://kubernetes.default.svc
    namespace: multi-agent-dev
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
```

### Option 3: Pipeline-Based Deployment

Add a deployment stage to the pipeline:

```yaml
- stage: Deploy
  displayName: Deploy to Dev
  dependsOn: PublishManifests
  jobs:
  - deployment: DeployToDev
    environment: 'development'
    strategy:
      runOnce:
        deploy:
          steps:
          - task: KubernetesManifest@0
            inputs:
              action: 'deploy'
              manifests: '$(Pipeline.Workspace)/k8s-manifests/dev-workloads.yaml'
```

## Testing the Pipeline

### 1. Test Locally

```bash
# Test frontend build
cd assets/frontend
npm ci
npm run build

# Test backend build
cd assets/backend
uv sync
uv run pytest

# Test manifest generation
kubectl kustomize kustomize/overlays/dev
```

### 2. Test in Pipeline

Create a feature branch and open a PR to trigger PR validation:

```bash
git checkout -b test-pipeline
git push origin test-pipeline
# Open PR in Azure DevOps
```

## Troubleshooting

### Pipeline Fails at Docker Push

**Problem**: `unauthorized: authentication required`

**Solution**:
- Verify service connection is configured
- Check ACR permissions
- Ensure containerRegistry parameter matches the service connection name

### Pipeline Fails at Manifest Generation

**Problem**: `kubectl: command not found`

**Solution**:
- Install kubectl on the build agent
- Or use a Microsoft-hosted agent (ubuntu-latest includes kubectl)

### Tests Pass Locally but Fail in Pipeline

**Problem**: Environment differences

**Solution**:
- Check Node.js/Python versions match
- Verify environment variables are set
- Review pipeline logs for specific errors

## Best Practices

1. **Use Separate Pipelines for Rapid Development**
   - Use component-specific pipelines during active development
   - Use main pipeline for releases

2. **Tag Management**
   - The pipeline uses BuildId for versioning
   - Consider semantic versioning for production releases
   - Update image tags in overlays after successful builds

3. **Security**
   - Never commit secrets to the repository
   - Use Azure Key Vault for sensitive values
   - Regularly rotate ACR credentials

4. **Testing**
   - Keep test stage fast (< 5 minutes)
   - Add integration tests for critical paths
   - Use test coverage reports

5. **Monitoring**
   - Set up pipeline failure notifications
   - Monitor build times for performance degradation
   - Track artifact sizes
