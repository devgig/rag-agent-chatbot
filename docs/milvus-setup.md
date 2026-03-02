# Milvus Setup Guide

This project uses an existing Milvus installation deployed via Helm chart in the `milvus-system` namespace.

## Helm Chart Configuration

The Milvus instance is deployed using the official Milvus Helm chart:

```yaml
helmCharts:
  - name: milvus
    repo: https://zilliztech.github.io/milvus-helm/
    version: "4.2.8"
    releaseName: milvus
    namespace: milvus-system
    valuesFile: values.yaml
```

## Connection Details

The backend application connects to Milvus using:

- **Host**: `milvus.milvus-system.svc.cluster.local`
- **Port**: 19530 (gRPC)
- **Protocol**: HTTP/gRPC
- **Collection**: `context` (automatically created by the application)

The connection is configured via the `MILVUS_ADDRESS` environment variable in the backend deployment:

```yaml
- name: MILVUS_ADDRESS
  value: "milvus.milvus-system.svc.cluster.local:19530"
```

## Authentication (Optional)

By default, the application connects to Milvus **without authentication**. The current code implementation does not use credentials.

### Enabling Authentication

If you want to enable authentication in Milvus, you'll need to:

1. **Configure Milvus with authentication enabled** (in your Helm values):

```yaml
# values.yaml for Milvus Helm chart
extraConfigFiles:
  user.yaml: |+
    common:
      security:
        authorizationEnabled: true
```

2. **Create a Milvus user and password** after deployment:

```bash
# Connect to Milvus
kubectl exec -it -n milvus-system milvus-standalone-0 -- bash

# Use Milvus CLI to create a user (if available)
# Or use the Python client
```

3. **Store credentials in Azure Key Vault**:

```bash
az keyvault secret set \
  --vault-name <your-keyvault-name> \
  --name rag-agent-milvus-username \
  --value "your_milvus_user"

az keyvault secret set \
  --vault-name <your-keyvault-name> \
  --name rag-agent-milvus-password \
  --value "your_secure_password"
```

4. **Deploy the Milvus ExternalSecret** (see `milvus-external-secret.yaml`):

```bash
kubectl apply -f kustomize/backend/base/milvus-external-secret.yaml
```

5. **Update the backend code** to use credentials (see "Code Updates" section below)

## Collections

The application automatically creates and manages the following Milvus collection:

- **Collection Name**: `context`
- **Purpose**: Stores document embeddings for RAG (Retrieval-Augmented Generation)
- **Auto-creation**: The collection is created automatically by the `langchain-milvus` library on first use

### Collection Schema

The collection stores:
- Document embeddings (vector data)
- Metadata:
  - `source`: Name of the source document/directory
  - `file_path`: Original file path
  - `filename`: Name of the file
  - Additional metadata from document processing

## Verification

### Check Milvus Service

Verify the Milvus service is accessible:

```bash
kubectl get svc -n milvus-system milvus
```

### Test Connection from Backend Pod

```bash
# Get a backend pod
BACKEND_POD=$(kubectl get pods -n rag-agent -l app=rag-agent-backend -o jsonpath='{.items[0].metadata.name}')

# Test DNS resolution
kubectl exec -n rag-agent $BACKEND_POD -- nslookup milvus.milvus-system.svc.cluster.local

# Test connectivity (if nc/netcat is available)
kubectl exec -n rag-agent $BACKEND_POD -- nc -zv milvus.milvus-system.svc.cluster.local 19530
```

### Check Collection Status

If you have Python and pymilvus installed locally:

```python
from pymilvus import connections, utility

# Connect to Milvus
connections.connect(
    host="<milvus-endpoint>",
    port="19530"
)

# List collections
print(utility.list_collections())

# Check collection stats
from pymilvus import Collection
collection = Collection("context")
print(f"Number of entities: {collection.num_entities}")
```

## Code Updates Required

⚠️ **Important**: The current backend code has a configuration issue that needs to be fixed.

### Issue

The `MILVUS_ADDRESS` environment variable is set in the deployment but **not used** by the application code. The code currently uses a hardcoded default value.

**Current code** (assets/backend/main.py line 61):
```python
vector_store = create_vector_store_with_config(config_manager)
```

**Current default** (assets/backend/vector_store.py line 70):
```python
def __init__(self, uri: str = "http://milvus:19530", ...):
```

### Fix Required

Update `assets/backend/main.py` to read and pass the `MILVUS_ADDRESS` environment variable:

```python
# Add near the other environment variable reads (after line 49)
MILVUS_ADDRESS = os.getenv("MILVUS_ADDRESS", "http://milvus:19530")

# Update line 61 to pass the URI
vector_store = create_vector_store_with_config(
    config_manager,
    uri=f"http://{MILVUS_ADDRESS}"  # or just MILVUS_ADDRESS if it includes protocol
)
```

### Authentication Support (Optional)

If you enable authentication in Milvus, you'll also need to update the code to pass credentials:

1. **Update `vector_store.py`** to accept credentials:

```python
def __init__(
    self,
    embeddings=None,
    uri: str = "http://milvus:19530",
    user: str = None,
    password: str = None,
    token: str = None,
    on_source_deleted: Optional[Callable[[str], None]] = None
):
    # ... existing code ...
    self.user = user
    self.password = password
    self.token = token
```

2. **Update connection arguments**:

```python
def _initialize_store(self):
    connection_args = {"uri": self.uri}

    # Add authentication if provided
    if self.token:
        connection_args["token"] = self.token
    elif self.user and self.password:
        connection_args["user"] = self.user
        connection_args["password"] = self.password

    self._store = Milvus(
        embedding_function=self.embeddings,
        collection_name="context",
        connection_args=connection_args,
        auto_id=True
    )
```

3. **Update `main.py`** to read credentials from environment variables:

```python
MILVUS_ADDRESS = os.getenv("MILVUS_ADDRESS", "http://milvus:19530")
MILVUS_USER = os.getenv("MILVUS_USER")
MILVUS_PASSWORD = os.getenv("MILVUS_PASSWORD")
MILVUS_TOKEN = os.getenv("MILVUS_TOKEN")

vector_store = create_vector_store_with_config(
    config_manager,
    uri=f"http://{MILVUS_ADDRESS}",
    user=MILVUS_USER,
    password=MILVUS_PASSWORD,
    token=MILVUS_TOKEN
)
```

## Troubleshooting

### Connection Refused

If you see "connection refused" errors:

1. Check Milvus is running:
   ```bash
   kubectl get pods -n milvus-system
   ```

2. Check DNS resolution works from the backend pod
3. Verify the service endpoint:
   ```bash
   kubectl get endpoints -n milvus-system milvus
   ```

### Collection Not Found

If the collection isn't being created:

1. Check backend logs for errors:
   ```bash
   kubectl logs -n rag-agent -l app=rag-agent-backend --tail=100
   ```

2. Verify Milvus has sufficient resources and is healthy

3. Check if there are permission issues (if authentication is enabled)

### Authentication Errors

If you enabled authentication and see auth errors:

1. Verify credentials are correctly stored in Azure Key Vault
2. Check ExternalSecret is syncing:
   ```bash
   kubectl get externalsecret -n rag-agent milvus-external-secret
   kubectl describe externalsecret -n rag-agent milvus-external-secret
   ```
3. Verify the secret was created:
   ```bash
   kubectl get secret -n rag-agent milvus-credentials
   ```

## Security Notes

- Milvus authentication is **optional** but recommended for production
- If authentication is disabled, ensure Milvus is not exposed outside the cluster
- Use network policies to restrict access to Milvus to only necessary services
- Consider using Istio/service mesh for mTLS between services
- Rotate Milvus credentials regularly if authentication is enabled
- Never commit credentials to version control
